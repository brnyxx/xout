"""규칙 감사 - 사람이 이미 쓴 지시 줄을 한 줄씩 재본다.

컴파일된 8줄은 xout이 만든 것이라 어떤 장면에서 갈리는지도 xout이 안다.
반대로 사람이 직접 쓴 규칙 파일은 줄마다 무엇을 요구하는지 xout이 모른다.
그래서 감사는 두 단계다: 사용자가 이름을 댄 외부 러너에게 줄마다 장면(할
일, 지키는 행동, 어기는 행동)을 짓게 하고, 그 장면을 줄 없이 한 번, 그 줄만
앞에 붙여 한 번 물어 답이 움직였는지 본다.

probe와 같은 규약이다: 세션 밖, 옵트인, 원장 무접촉, 규칙 파일 무접촉.
남는 것은 소유 디렉토리(audits/) 안의 영수증뿐이다.

판정은 네 가지. 줄이 없어도 지키면 default(그 줄이 없어도 하는 행동),
없을 땐 어기다 붙이면 지키면 effective(그 줄이 일한다), 줄을 붙여도 어기면
ignored(가장 중요한 칸), 답을 읽어낼 수 없거나 표가 갈리면 unclear.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from xout.judge import Candidate
from xout.probe import PROMPT as ASK_PROMPT, Runner, majority, parse_choice

AUDIT_DIR = "audits"
BATCH_LINES = 20
DEFAULT_LIMIT = 60
MIN_LINE_CHARS = 12

FOLLOW = "follow"
VIOLATE = "violate"

VERDICT_DEFAULT = "default"
VERDICT_EFFECTIVE = "effective"
VERDICT_IGNORED = "ignored"
VERDICT_UNCLEAR = "unclear"
VERDICT_SKIPPED = "not_an_instruction"

#: 보고 순서 - 무시된 줄이 제일 중요하지만, 읽는 사람은 위에서부터 좁혀 온다.
VERDICTS: tuple[str, ...] = (
    VERDICT_DEFAULT,
    VERDICT_EFFECTIVE,
    VERDICT_IGNORED,
    VERDICT_UNCLEAR,
)

#: 장면 생성 프롬프트 - 지시가 아닌 줄은 빼고, JSON 배열만.
GENERATE_PROMPT: dict[str, str] = {
    "ko": (
        "아래는 어떤 개발자가 코딩 에이전트에게 주는 규칙 파일의 줄들이다. "
        "각 줄에는 번호가 붙어 있다.\n\n"
        "줄마다 그것이 코딩 에이전트의 행동을 지시하는 줄인지 판단하고, 맞으면 "
        "그 줄에 걸리는 짧은 장면을 써라.\n"
        '  - "task": 그 줄이 걸릴 만한 상황에서 사용자가 할 법한 한 문장.\n'
        '  - "follow": 그 줄을 지키는 구체적인 다음 행동 하나.\n'
        '  - "violate": 그 줄을 어기는 구체적인 다음 행동 하나.\n\n'
        "두 행동은 둘 다 그럴듯해야 하고 길이와 말투가 비슷해야 한다 - 문장만 "
        "보고 어느 쪽이 맞는지 뻔하면 안 된다. 에이전트에게 아무 행동도 시키지 "
        "않는 줄(제목, 설명, 명령어, 메모)은 빼고 넘어간다.\n\n"
        "줄:\n{lines}\n\n"
        "JSON 배열만 출력하라. 설명 없이. 형식: "
        '[{{"n": 3, "task": "...", "follow": "...", "violate": "..."}}]. '
        "해당하는 줄이 없으면 []."
    ),
    "en": (
        "Below are numbered lines from a rules file a developer gives to a coding agent.\n\n"
        "For each line, decide whether it tells a coding agent how to behave. If it does, "
        "write a short scene for it.\n"
        '  - "task": one sentence a user might say where that line would apply.\n'
        '  - "follow": one concrete next action that obeys the line.\n'
        '  - "violate": one concrete next action that breaks the line.\n\n'
        "Both actions must be plausible and of the same length and tone - the wording alone "
        "must not give away which one is right. Skip lines that ask the agent to do nothing "
        "(headings, explanations, commands, notes) and leave them out.\n\n"
        "Lines:\n{lines}\n\n"
        "Output a JSON array only, no prose. Format: "
        '[{{"n": 3, "task": "...", "follow": "...", "violate": "..."}}]. '
        "If no line qualifies, output []."
    ),
    "ja": (
        "以下は、ある開発者がコーディングエージェントに渡すルールファイルの行で、"
        "番号が付いています。\n\n"
        "各行がコーディングエージェントの振る舞いを指示しているか判断し、指示して"
        "いれば、その行に効いてくる短い場面を書いてください。\n"
        '  - "task": その行が効く状況で利用者が言いそうな一文。\n'
        '  - "follow": その行を守る具体的な次の行動を一つ。\n'
        '  - "violate": その行を破る具体的な次の行動を一つ。\n\n'
        "二つの行動はどちらも自然で、長さも口調も揃えてください - 文面だけで"
        "どちらが正しいか分かってしまってはいけません。エージェントに何の行動も"
        "求めていない行(見出し、説明、コマンド、メモ)は飛ばします。\n\n"
        "行:\n{lines}\n\n"
        "JSON 配列だけを出力してください。説明は不要。形式: "
        '[{{"n": 3, "task": "...", "follow": "...", "violate": "..."}}]。'
        "該当する行がなければ []。"
    ),
    "zh": (
        "下面是某位开发者写给编码智能体的规则文件里的若干行，带有编号。\n\n"
        "逐行判断它是不是在指示编码智能体怎么做事; 如果是，就为这一行写一个简短的"
        "场景。\n"
        '  - "task": 在这一行会起作用的情况下，用户可能说的一句话。\n'
        '  - "follow": 遵守这一行的一个具体的下一步动作。\n'
        '  - "violate": 违反这一行的一个具体的下一步动作。\n\n'
        "两个动作都要合理，长度和语气也要接近 - 不能只看措辞就知道哪个对。对智能体"
        "没有任何行为要求的行(标题、说明、命令、备注)就跳过。\n\n"
        "行:\n{lines}\n\n"
        "只输出 JSON 数组，不要解释。格式: "
        '[{{"n": 3, "task": "...", "follow": "...", "violate": "..."}}]。'
        "没有符合的行就输出 []。"
    ),
}

#: 한 파일 안에서 서로 어긋나는 줄 짝 - 보고만 한다, 고치지 않는다.
CLASH_PROMPT: dict[str, str] = {
    "ko": (
        "아래는 한 규칙 파일에서 코딩 에이전트에게 행동을 지시하는 줄들이다. "
        "각 줄에는 번호가 붙어 있다.\n\n"
        "서로 반대되는 것을 시켜서 한 에이전트가 둘 다 지킬 수 없는 짝을 찾아라. "
        "정말로 부딪히는 짝만 적는다 - 주제가 다른 줄이나, 같은 방향에서 한쪽이 "
        "더 엄격하기만 한 줄은 부딪히는 것이 아니다.\n\n"
        "줄:\n{lines}\n\n"
        "JSON 배열만 출력하라. 설명 없이. 형식: "
        '[{{"a": 3, "b": 11, "why": "..."}}]. 부딪히는 짝이 없으면 [].'
    ),
    "en": (
        "Below are numbered lines from one rules file that tell a coding agent how to act.\n\n"
        "Find pairs of lines that ask for opposite things, so one agent cannot obey both. "
        "List only pairs that really clash - lines on different topics, or one line simply "
        "being stricter than another in the same direction, do not clash.\n\n"
        "Lines:\n{lines}\n\n"
        "Output a JSON array only, no prose. Format: "
        '[{{"a": 3, "b": 11, "why": "..."}}]. If nothing clashes, output [].'
    ),
    "ja": (
        "以下は、一つのルールファイルの中でコーディングエージェントに行動を指示して"
        "いる行で、番号が付いています。\n\n"
        "正反対のことを求めていて、一つのエージェントが両方は守れない組を探して"
        "ください。本当にぶつかる組だけを挙げます - 話題が違う行や、同じ方向で片方が"
        "厳しいだけの行はぶつかっていません。\n\n"
        "行:\n{lines}\n\n"
        "JSON 配列だけを出力してください。説明は不要。形式: "
        '[{{"a": 3, "b": 11, "why": "..."}}]。ぶつかる組がなければ []。'
    ),
    "zh": (
        "下面是同一个规则文件里指示编码智能体怎么做事的行，带有编号。\n\n"
        "找出要求相反、一个智能体没法同时遵守的成对的行。只列真正冲突的组 - 话题不同"
        "的行，或者同一个方向上一条更严格的行，都不算冲突。\n\n"
        "行:\n{lines}\n\n"
        "只输出 JSON 数组，不要解释。格式: "
        '[{{"a": 3, "b": 11, "why": "..."}}]。没有冲突就输出 []。'
    ),
}

_FENCE: tuple[str, ...] = ("```", "~~~")
_MARKER = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+|>\s*|\[[ xX]\]\s*)+")


def instruction_shaped(line: str) -> bool:
    """러너를 부르기 전에 버릴 것 - 제목, 표, 목록 기호뿐인 줄, 너무 짧은 줄."""
    if line.startswith("#") or line.startswith("|"):
        return False
    return len(_MARKER.sub("", line).strip()) >= MIN_LINE_CHARS


@dataclass(frozen=True, slots=True)
class Selection:
    """러너에 보낼 줄과, 보내지 않기로 한 줄의 수."""

    items: tuple[Candidate, ...]
    scanned: int
    skipped: int
    over_limit: int

    @property
    def files(self) -> int:
        return len({item.abs_path for item in self.items})


def select(items: Sequence[Candidate], limit: int = DEFAULT_LIMIT) -> Selection:
    """코드 블록 안, 제목, 표, 짧은 줄을 먼저 버리고 limit로 자른다 - 첫 실행이 싸도록."""
    fenced: set[str] = set()
    kept: list[Candidate] = []
    for item in items:
        if item.line.startswith(_FENCE):
            fenced.symmetric_difference_update((item.abs_path,))
            continue
        if item.abs_path in fenced:
            continue
        if not instruction_shaped(item.line):
            continue
        kept.append(item)
    cut = max(0, int(limit))
    return Selection(
        items=tuple(kept[:cut]),
        scanned=len(items),
        skipped=len(items) - len(kept),
        over_limit=max(0, len(kept) - cut),
    )


def _first(path: str, line_no: int) -> str:
    """A 자리에 무엇을 놓을지 - 줄마다 고정된 해시로 섞어 자리 편향을 흩는다."""
    digest = hashlib.sha256(f"{path}:{line_no}".encode("utf-8")).digest()
    return FOLLOW if digest[0] % 2 == 0 else VIOLATE


@dataclass(frozen=True, slots=True)
class Scene:
    """한 줄에 대해 러너가 지은 장면 - 할 일 하나, 행동 둘."""

    candidate: Candidate
    task: str
    follow: str
    violate: str
    first: str

    @property
    def a_text(self) -> str:
        return self.follow if self.first == FOLLOW else self.violate

    @property
    def b_text(self) -> str:
        return self.violate if self.first == FOLLOW else self.follow

    def kind_of(self, letter: str | None) -> str | None:
        if letter == "A":
            return self.first
        if letter == "B":
            return VIOLATE if self.first == FOLLOW else FOLLOW
        return None


def _numbered(lines: Sequence[tuple[int, str]]) -> str:
    return "\n".join(f"{n}. {text}" for n, text in lines)


def build_generate_prompt(lines: Sequence[tuple[int, str]], lang: str) -> str:
    table = GENERATE_PROMPT.get(lang) or GENERATE_PROMPT["ko"]
    return table.format(lines=_numbered(lines))


def build_clash_prompt(lines: Sequence[tuple[int, str]], lang: str) -> str:
    table = CLASH_PROMPT.get(lang) or CLASH_PROMPT["ko"]
    return table.format(lines=_numbered(lines))


def build_ask_prompt(scene: Scene, lang: str, rule_line: str | None) -> str:
    """probe와 같은 골격 - 한 글자 답만 받는다. 규칙 자리에는 그 한 줄만 들어간다."""
    texts = ASK_PROMPT.get(lang) or ASK_PROMPT["ko"]
    body = texts["frame"].format(task=scene.task, a=scene.a_text, b=scene.b_text)
    if rule_line is None:
        return body
    return texts["ruled"].format(rules=rule_line + "\n") + body


_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def _payload(raw: str) -> list[object]:
    """응답의 첫 JSON 배열 - 없거나 깨졌으면 아무것도 받지 않는다."""
    match = _ARRAY.search(raw or "")
    if match is None:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _line_number(value: object, allowed: set[int]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        return None
    return value


def parse_scenes(raw: str, allowed: Sequence[int]) -> dict[int, tuple[str, str, str]]:
    """세 칸이 다 채워진 항목만 받는다. 모르는 번호, 빈 칸, 중복은 버린다."""
    allowed_set = set(allowed)
    scenes: dict[int, tuple[str, str, str]] = {}
    for item in _payload(raw):
        if not isinstance(item, dict):
            continue
        n = _line_number(item.get("n"), allowed_set)
        if n is None or n in scenes:
            continue
        task, follow, violate = item.get("task"), item.get(FOLLOW), item.get(VIOLATE)
        if not all(isinstance(v, str) and v.strip() for v in (task, follow, violate)):
            continue
        scenes[n] = (task.strip(), follow.strip(), violate.strip())
    return scenes


def parse_clashes(raw: str, allowed: Sequence[int]) -> list[tuple[int, int, str]]:
    """서로 다른 두 번호를 가리키는 항목만 받는다. 같은 짝은 한 번만."""
    allowed_set = set(allowed)
    pairs: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for item in _payload(raw):
        if not isinstance(item, dict):
            continue
        a = _line_number(item.get("a"), allowed_set)
        b = _line_number(item.get("b"), allowed_set)
        why = item.get("why", "")
        if a is None or b is None or a == b or not isinstance(why, str):
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((a, b, why.strip()))
    return pairs


@dataclass(frozen=True, slots=True)
class LineOutcome:
    """한 줄의 시행 결과 - 원문은 전부 남기고, 판정은 다수결로 낸다."""

    scene: Scene
    bare_raws: tuple[str, ...]
    ruled_raws: tuple[str, ...]

    @property
    def bare_kinds(self) -> tuple[str | None, ...]:
        return tuple(self.scene.kind_of(parse_choice(raw)) for raw in self.bare_raws)

    @property
    def ruled_kinds(self) -> tuple[str | None, ...]:
        return tuple(self.scene.kind_of(parse_choice(raw)) for raw in self.ruled_raws)

    @property
    def bare_kind(self) -> str | None:
        return majority(self.bare_kinds)

    @property
    def ruled_kind(self) -> str | None:
        return majority(self.ruled_kinds)

    @property
    def trials(self) -> int:
        return len(self.ruled_raws)

    @property
    def verdict(self) -> str:
        bare, ruled = self.bare_kind, self.ruled_kind
        if bare is None or ruled is None:
            return VERDICT_UNCLEAR
        if ruled == VIOLATE:
            return VERDICT_IGNORED
        return VERDICT_DEFAULT if bare == FOLLOW else VERDICT_EFFECTIVE

    def to_dict(self) -> dict[str, object]:
        cand = self.scene.candidate
        return {
            "path": cand.path,
            "line_no": cand.line_no,
            "line": cand.line,
            "task": self.scene.task,
            "follow": self.scene.follow,
            "violate": self.scene.violate,
            "shown_as_a": self.scene.first,
            "bare": {"raws": list(self.bare_raws), "kinds": list(self.bare_kinds),
                     "kind": self.bare_kind},
            "ruled": {"raws": list(self.ruled_raws), "kinds": list(self.ruled_kinds),
                      "kind": self.ruled_kind},
            "trials": self.trials,
            "verdict": self.verdict,
        }


@dataclass(frozen=True, slots=True)
class Contradiction:
    """같은 파일 안에서 서로 어긋난다고 러너가 지목한 두 줄 - 보고만 한다."""

    path: str
    a_line: int
    b_line: int
    a_text: str
    b_text: str
    why: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "a": {"line_no": self.a_line, "line": self.a_text},
            "b": {"line_no": self.b_line, "line": self.b_text},
            "why": self.why,
        }


@dataclass(frozen=True, slots=True)
class AuditCall:
    """러너 호출 한 번 - 장면 생성과 충돌 조회의 원문을 그대로 남긴다."""

    kind: str
    path: str
    line_numbers: tuple[int, ...]
    raw: str
    parsed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "lines": list(self.line_numbers),
            "raw": self.raw,
            "parsed": self.parsed,
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    lang: str
    runner: tuple[str, ...]
    started_at: str
    repeat: int
    scanned: int
    skipped: int
    over_limit: int
    sent: int
    outcomes: tuple[LineOutcome, ...]
    not_instructions: tuple[Candidate, ...]
    contradictions: tuple[Contradiction, ...]
    calls: tuple[AuditCall, ...]

    def by_verdict(self, verdict: str) -> tuple[LineOutcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict == verdict)

    @property
    def summary(self) -> dict[str, int]:
        counts = {verdict: len(self.by_verdict(verdict)) for verdict in VERDICTS}
        counts.update(
            {
                "sent": self.sent,
                "scanned": self.scanned,
                "skipped": self.skipped,
                "over_limit": self.over_limit,
                "scenes": len(self.outcomes),
                VERDICT_SKIPPED: len(self.not_instructions),
                "contradictions": len(self.contradictions),
            }
        )
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "xout_audit_receipt",
            "lang": self.lang,
            "runner": list(self.runner),
            "started_at": self.started_at,
            "repeat": self.repeat,
            "summary": self.summary,
            "lines": [o.to_dict() for o in self.outcomes],
            "not_instructions": [
                {"path": c.path, "line_no": c.line_no, "line": c.line}
                for c in self.not_instructions
            ],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "calls": [c.to_dict() for c in self.calls],
        }


def audit(
    selection: Selection,
    runner: Runner,
    lang: str,
    runner_command: Sequence[str] = (),
    repeat: int = 1,
    batch: int = BATCH_LINES,
    now: str | None = None,
) -> AuditReport:
    """파일별로 장면을 짓게 하고, 줄마다 두 번씩 물어보고, 끝에 충돌을 묻는다."""
    repeat = max(1, int(repeat))
    batch = max(1, int(batch))
    by_path: dict[str, list[Candidate]] = {}
    for item in selection.items:
        by_path.setdefault(item.abs_path, []).append(item)
    outcomes: list[LineOutcome] = []
    not_instructions: list[Candidate] = []
    contradictions: list[Contradiction] = []
    calls: list[AuditCall] = []
    for group in by_path.values():
        scenes: list[Scene] = []
        for start in range(0, len(group), batch):
            chunk = group[start : start + batch]
            numbers = tuple(c.line_no for c in chunk)
            raw = runner(build_generate_prompt([(c.line_no, c.line) for c in chunk], lang))
            parsed = parse_scenes(raw, numbers)
            calls.append(AuditCall("generate", chunk[0].path, numbers, raw, len(parsed)))
            for cand in chunk:
                found = parsed.get(cand.line_no)
                if found is None:
                    not_instructions.append(cand)
                    continue
                task, follow, violate = found
                scenes.append(
                    Scene(cand, task, follow, violate, _first(cand.path, cand.line_no))
                )
        for scene in scenes:
            bare: list[str] = []
            ruled: list[str] = []
            for _ in range(repeat):
                bare.append(runner(build_ask_prompt(scene, lang, None)))
                ruled.append(runner(build_ask_prompt(scene, lang, scene.candidate.line)))
            outcomes.append(LineOutcome(scene, tuple(bare), tuple(ruled)))
        if len(scenes) > 1:
            numbers = tuple(s.candidate.line_no for s in scenes)
            raw = runner(
                build_clash_prompt([(s.candidate.line_no, s.candidate.line) for s in scenes], lang)
            )
            pairs = parse_clashes(raw, numbers)
            path = scenes[0].candidate.path
            calls.append(AuditCall("clash", path, numbers, raw, len(pairs)))
            texts = {s.candidate.line_no: s.candidate.line for s in scenes}
            for a, b, why in pairs:
                contradictions.append(Contradiction(path, a, b, texts[a], texts[b], why))
    return AuditReport(
        lang=lang,
        runner=tuple(runner_command),
        started_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        repeat=repeat,
        scanned=selection.scanned,
        skipped=selection.skipped,
        over_limit=selection.over_limit,
        sent=len(selection.items),
        outcomes=tuple(outcomes),
        not_instructions=tuple(not_instructions),
        contradictions=tuple(contradictions),
        calls=tuple(calls),
    )


def write_receipt(base_dir: Path, report: AuditReport) -> Path:
    """영수증은 소유 디렉토리 안(~/.claude/xout/audits/)에만 쓴다 - 규칙 파일은 읽기만 했다."""
    directory = base_dir / AUDIT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9T]", "", report.started_at.split("+")[0])
    path = directory / f"audit-{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"audit-{stamp}-{counter}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
