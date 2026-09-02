"""외부 판정자 - 규칙 파일 줄의 축/값 귀속을 사용자의 에이전트에게 맡긴다.

`xout mine`의 기본 계층은 정규식이다: 의존성 없이 오프라인에서 돌고,
어떤 줄이 왜 잡혔는지 패턴으로 설명된다. 대신 표현이 조금만 달라도 놓친다.
이 모듈은 옵트인 두 번째 계층이다. 세션 밖에서, 사용자가 이름을 댄 외부
러너(`claude -p`, `codex exec`, ...)에게 같은 줄들을 묶어 보내고, 축/값을
JSON으로 받아 관측으로 바꾼다. probe와 같은 규약이다: 원장은 건드리지
않고, 러너의 원문 답을 영수증으로 남긴다.

두 계층은 서로를 검사한다. 정규식이 잡은 줄을 에이전트가 버리면 "dropped",
에이전트만 잡으면 "added", 둘이 같으면 "agreed"로 센다 - 정규식 계층이 어디서
약한지가 숫자로 남는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from xout.compiler import RULE_LANG_TABLES
from xout.counter import DEFAULT_CATALOG
from xout.mine import (
    Observation,
    _iter_rule_files,
    _without_owned_text,
    user_rule_files,
)
from xout.state import axis_label

JUDGE_DIR = "judgments"
BATCH_LINES = 40
_MAX_LINE_CHARS = 240

#: 판정 프롬프트 골격 - 한 줄에 하나의 (축, 값)만, JSON 배열만.
PROMPT: dict[str, dict[str, str]] = {
    "ko": {
        "frame": (
            "아래는 어떤 개발자가 코딩 에이전트에게 주는 규칙 파일의 줄들이다. "
            "각 줄에는 번호가 붙어 있다.\n\n"
            "다음 축 중 하나에 대한 선호를 분명히 말하는 줄만 골라, 그 줄의 번호와 "
            "축·값을 적어라. 선호를 말하지 않는 줄(제목, 설명, 명령어, 다른 주제)은 "
            "건너뛴다. 부정 표현을 조심해라: \"행동 전에 절대 묻지 마라\"는 "
            "act_then_report이지 ask_first가 아니다. 한 줄이 두 축을 말하면 둘 다 적는다.\n\n"
            "축과 값 (값 옆 문장이 그 값의 뜻이다):\n{glossary}\n\n"
            "줄:\n{lines}\n\n"
            "JSON 배열만 출력하라. 설명 없이. 형식: "
            '[{{"n": 3, "axis": "autonomy", "value": "ask_first"}}]. '
            "해당하는 줄이 없으면 []."
        ),
    },
    "en": {
        "frame": (
            "Below are numbered lines from a rules file a developer gives to a coding agent.\n\n"
            "Pick only the lines that clearly state a preference on one of the axes below, "
            "and give the line number with its axis and value. Skip lines that state no such "
            "preference (headings, explanations, commands, other topics). Read negations "
            "carefully: \"never ask before acting\" is act_then_report, not ask_first. "
            "If one line speaks to two axes, list both.\n\n"
            "Axes and values (the sentence next to a value is what that value means):\n"
            "{glossary}\n\n"
            "Lines:\n{lines}\n\n"
            "Output a JSON array only, no prose. Format: "
            '[{{"n": 3, "axis": "autonomy", "value": "ask_first"}}]. '
            "If no line qualifies, output []."
        ),
    },
    "ja": {
        "frame": (
            "以下は、ある開発者がコーディングエージェントに渡すルールファイルの行で、"
            "番号が付いています。\n\n"
            "下の軸のどれかについて好みをはっきり述べている行だけを選び、行番号と軸・値を"
            "書いてください。好みを述べていない行(見出し、説明、コマンド、別の話題)は"
            "飛ばします。否定表現に注意: 「行動の前に決して聞くな」は act_then_report であって "
            "ask_first ではありません。1 行が 2 つの軸に触れていれば両方書きます。\n\n"
            "軸と値 (値の横の文がその値の意味です):\n{glossary}\n\n"
            "行:\n{lines}\n\n"
            "JSON 配列だけを出力してください。説明は不要。形式: "
            '[{{"n": 3, "axis": "autonomy", "value": "ask_first"}}]。'
            "該当する行がなければ []。"
        ),
    },
    "zh": {
        "frame": (
            "下面是某位开发者写给编码智能体的规则文件里的若干行，带有编号。\n\n"
            "只挑出对下列某个轴明确表达偏好的行，写出行号和轴、值。没有表达这类偏好的行"
            "(标题、说明、命令、其他话题)跳过。注意否定: \"行动前绝不要问\" 是 "
            "act_then_report，不是 ask_first。一行涉及两个轴就都写。\n\n"
            "轴和值 (值旁边的句子就是这个值的含义):\n{glossary}\n\n"
            "行:\n{lines}\n\n"
            "只输出 JSON 数组，不要解释。格式: "
            '[{{"n": 3, "axis": "autonomy", "value": "ask_first"}}]。'
            "没有符合的行就输出 []。"
        ),
    },
}


class JudgeError(RuntimeError):
    """러너 부재처럼 판정을 시작할 수 없는 상태."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """판정에 보낼 규칙 파일 한 줄."""

    path: str
    abs_path: str
    line_no: int
    line: str


@dataclass(frozen=True, slots=True)
class JudgeCall:
    """러너 호출 한 번 - 보낸 줄들과 받은 원문."""

    path: str
    line_numbers: tuple[int, ...]
    raw: str
    parsed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "lines": list(self.line_numbers),
            "raw": self.raw,
            "parsed": self.parsed,
        }


@dataclass(frozen=True, slots=True)
class JudgeReport:
    lang: str
    runner: tuple[str, ...]
    started_at: str
    observations: tuple[Observation, ...]
    calls: tuple[JudgeCall, ...]
    candidates: int
    agreement: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "xout_judge_receipt",
            "lang": self.lang,
            "runner": list(self.runner),
            "started_at": self.started_at,
            "candidates": self.candidates,
            "agreement": dict(self.agreement),
            "observations": [o.to_dict() for o in self.observations],
            "calls": [c.to_dict() for c in self.calls],
        }


def glossary(lang: str) -> str:
    """축과 값의 뜻 - 규칙 문장 테이블에서 그대로 가져온다 (별도 설명 유지 안 함)."""
    rule_text = RULE_LANG_TABLES.get(lang, RULE_LANG_TABLES["ko"])[0]
    rows: list[str] = []
    for axis, values in DEFAULT_CATALOG.items():
        rows.append(f"{axis} ({axis_label(axis, lang)}):")
        for value in values:
            rows.append(f"  - {value}: {rule_text[(axis, value)]}")
    return "\n".join(rows)


def build_prompt(lines: Sequence[tuple[int, str]], lang: str) -> str:
    texts = PROMPT.get(lang) or PROMPT["ko"]
    numbered = "\n".join(f"{n}. {text}" for n, text in lines)
    return texts["frame"].format(glossary=glossary(lang), lines=numbered)


_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def parse_verdicts(raw: str, allowed: Sequence[int]) -> dict[int, list[tuple[str, str]]]:
    """응답의 첫 JSON 배열에서 카탈로그에 있는 (축, 값)만 받는다. 나머지는 버린다."""
    match = _ARRAY.search(raw or "")
    if match is None:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, list):
        return {}
    allowed_set = set(allowed)
    verdicts: dict[int, list[tuple[str, str]]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        n, axis, value = item.get("n"), item.get("axis"), item.get("value")
        if not isinstance(n, int) or n not in allowed_set:
            continue
        if not isinstance(axis, str) or not isinstance(value, str):
            continue
        if value not in DEFAULT_CATALOG.get(axis, ()):
            continue
        bucket = verdicts.setdefault(n, [])
        if all(existing[0] != axis for existing in bucket):
            bucket.append((axis, value))
    return verdicts


def candidates(
    roots: Sequence[Path],
    include_user: bool = False,
    home: Path | None = None,
) -> list[Candidate]:
    """mine과 같은 파일, 같은 줄 필터 - xout 소유 텍스트는 뺀다."""
    found: list[Candidate] = []
    seen: set[str] = set()

    def collect(path: Path, display: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line_no, raw in enumerate(_without_owned_text(text).splitlines(), start=1):
            line = raw.strip()
            if not line or len(line) > _MAX_LINE_CHARS:
                continue
            found.append(Candidate(display, str(path.resolve()), line_no, line))

    for root in roots:
        root = root.expanduser()
        if root.is_file():
            seen.add(str(root.resolve()))
            collect(root, str(root))
            continue
        if not root.is_dir():
            continue
        for path in _iter_rule_files(root):
            seen.add(str(path.resolve()))
            collect(path, str(path.relative_to(root)))
    if include_user:
        home_dir = (home or Path.home()).expanduser()
        for path in user_rule_files(home_dir):
            if str(path.resolve()) in seen:
                continue
            collect(path, "~/" + path.relative_to(home_dir).as_posix())
    return found


Runner = Callable[[str], str]


def judge(
    items: Sequence[Candidate],
    runner: Runner,
    lang: str,
    runner_command: Sequence[str] = (),
    batch: int = BATCH_LINES,
    on_call: Callable[[JudgeCall], None] | None = None,
    now: str | None = None,
) -> JudgeReport:
    """파일별로 줄을 batch개씩 묶어 러너에 보내고 관측으로 바꾼다."""
    batch = max(1, int(batch))
    observations: list[Observation] = []
    calls: list[JudgeCall] = []
    by_path: dict[str, list[Candidate]] = {}
    for item in items:
        by_path.setdefault(item.abs_path, []).append(item)
    for group in by_path.values():
        for start in range(0, len(group), batch):
            chunk = group[start : start + batch]
            numbered = [(c.line_no, c.line) for c in chunk]
            raw = runner(build_prompt(numbered, lang))
            verdicts = parse_verdicts(raw, [c.line_no for c in chunk])
            by_line = {c.line_no: c for c in chunk}
            parsed = 0
            for line_no in sorted(verdicts):
                cand = by_line[line_no]
                for axis, value in verdicts[line_no]:
                    parsed += 1
                    observations.append(
                        Observation(
                            axis=axis,
                            value=value,
                            path=cand.path,
                            line_no=cand.line_no,
                            line=cand.line,
                            abs_path=cand.abs_path,
                        )
                    )
            call = JudgeCall(chunk[0].path, tuple(c.line_no for c in chunk), raw, parsed)
            calls.append(call)
            if on_call is not None:
                on_call(call)
    return JudgeReport(
        lang=lang,
        runner=tuple(runner_command),
        started_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        observations=tuple(observations),
        calls=tuple(calls),
        candidates=len(items),
    )


def _key(obs: Observation) -> tuple[str, int, str]:
    return (obs.abs_path or obs.path, obs.line_no, obs.axis)


def merge(
    pattern: Sequence[Observation], agent: Sequence[Observation]
) -> tuple[list[Observation], dict[str, int], dict[tuple[str, int, str], str]]:
    """에이전트 판정이 이기되, 두 계층의 합의/추가/탈락을 센다.

    반환: (병합 관측, 집계, 관측 키 -> 출처 "agreed"|"agent"|"pattern").
    정규식만 잡은 줄은 에이전트가 그 줄을 봤다면 탈락(dropped)이고 병합에서
    빠진다. 에이전트가 같은 줄·축에 다른 값을 냈으면 에이전트 값이 남는다.
    """
    agent_by_key = {_key(o): o for o in agent}
    pattern_by_key = {_key(o): o for o in pattern}
    merged: list[Observation] = []
    source: dict[tuple[str, int, str], str] = {}
    agreed = added = dropped = disagreed = 0
    for key, obs in agent_by_key.items():
        twin = pattern_by_key.get(key)
        if twin is None:
            added += 1
            source[key] = "agent"
        elif twin.value == obs.value:
            agreed += 1
            source[key] = "agreed"
        else:
            disagreed += 1
            source[key] = "agent"
        merged.append(obs)
    for key in pattern_by_key:
        if key not in agent_by_key:
            dropped += 1
    merged.sort(key=lambda o: (o.path, o.line_no, o.axis))
    return (
        merged,
        {"agreed": agreed, "added": added, "dropped": dropped, "disagreed": disagreed},
        source,
    )


def write_receipt(base_dir: Path, report: JudgeReport) -> Path:
    """영수증은 소유 디렉토리 안(~/.claude/xout/judgments/)에만 쓴다."""
    directory = base_dir / JUDGE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9T]", "", report.started_at.split("+")[0])
    path = directory / f"judge-{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"judge-{stamp}-{counter}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
