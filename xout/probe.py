"""효과 탐침 - 컴파일된 규칙이 실제 에이전트의 선택을 움직이는지 잰다.

세션은 LLM을 호출하지 않는다는 불변식은 그대로다. 탐침은 세션 밖에서
사용자가 명시적으로 켜는 별도 명령이고, 러너는 외부 CLI(기본 `claude -p`)다.
같은 장면 프롬프트를 규칙 없이 한 번, 착지된 XOUT.md 본문을 앞세워 한 번
던져 A/B 선택이 생존값 쪽으로 움직였는지 영수증(JSON)으로 남긴다.

측정 단위는 페어와 같다: 한 장면, 한 축, 생존값 대 대안. 두 응답의 A/B
순서는 장면·축 해시로 섞어 자리 편향을 흩는다.

세 가지 강화 모드가 있다. `repeat`는 같은 질문을 여러 번 던져 다수결로
판정하고 시행별 원문을 전부 남긴다. `context_text`는 실제 프로젝트 규칙
문서를 앞에 깔아 규칙이 다른 지시들 사이에 묻혔을 때도 버티는지 본다.
`rules_in_prompt=False`는 규칙을 프롬프트에 넣지 않는다 - 호출자가
phase_hook으로 도구의 실제 규칙 파일에 블록을 넣었다 뺐다 하며, 도구가
자기 파일을 통해 규칙을 읽는지를 잰다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from typing import Callable, Mapping, Sequence

from xout.counter import DEFAULT_CATALOG
from xout.fixtures import (
    CONTEXT_IRREVERSIBLE,
    KIND_STATIC,
    FixturePack,
    RepoSkin,
    _substitute,
)

PROBE_DIR = "probes"
DEFAULT_RUNNER: tuple[str, ...] = ("claude", "-p", "--output-format", "text")
DEFAULT_TIMEOUT = 180.0

#: 탐침 프롬프트 골격 - 인식론 어휘 없이, 한 글자 답만 요구한다.
PROMPT: dict[str, dict[str, str]] = {
    "ko": {
        "frame": (
            "당신은 아래 대화의 코딩 에이전트다.\n\n{task}\n\n"
            "다음에 할 수 있는 응답 두 가지:\n\nA) {a}\n\nB) {b}\n\n"
            "지금 실제로 따르는 지시대로라면 당신은 어느 쪽으로 행동하는가? "
            "A 또는 B 한 글자로만 답하라."
        ),
        "ruled": "당신과 함께 일하는 사람의 고정 지시:\n\n{rules}\n---\n\n",
        "context": "이 프로젝트에 이미 있는 지시 문서:\n\n{context}\n---\n\n",
    },
    "en": {
        "frame": (
            "You are the coding agent in the exchange below.\n\n{task}\n\n"
            "Two ways you could respond next:\n\nA) {a}\n\nB) {b}\n\n"
            "Under the instructions you are actually operating with, which one do you do? "
            "Answer with exactly one letter, A or B, and nothing else."
        ),
        "ruled": "Standing instructions from the person you work for:\n\n{rules}\n---\n\n",
        "context": "Instructions already in place in this project:\n\n{context}\n---\n\n",
    },
    "ja": {
        "frame": (
            "あなたは以下のやり取りに登場するコーディングエージェントです。\n\n{task}\n\n"
            "次の応答として考えられるのは二つ:\n\nA) {a}\n\nB) {b}\n\n"
            "いま実際に従っている指示のもとでは、どちらを選びますか? "
            "A か B の一文字だけで答えてください。"
        ),
        "ruled": "一緒に働く相手から常に受けている指示:\n\n{rules}\n---\n\n",
        "context": "このプロジェクトに既にある指示文書:\n\n{context}\n---\n\n",
    },
    "zh": {
        "frame": (
            "你是下面这段对话里的编码智能体。\n\n{task}\n\n"
            "接下来有两种回应可选:\n\nA) {a}\n\nB) {b}\n\n"
            "按照你现在实际遵循的指示，你会选哪一个? 只回答一个字母，A 或 B。"
        ),
        "ruled": "和你一起工作的人给的固定指示:\n\n{rules}\n---\n\n",
        "context": "这个项目里已有的指示文档:\n\n{context}\n---\n\n",
    },
}


class ProbeError(RuntimeError):
    """러너 부재, 규칙 부재처럼 탐침을 시작할 수 없는 상태."""


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """탐침이 아는 규칙의 전부 - 두 맥락의 생존값과 사용자가 지운 값."""

    value: str
    irreversible_value: str | None = None
    eliminated: tuple[str, ...] = ()

    def survivor_for(self, context: str) -> str:
        if context == CONTEXT_IRREVERSIBLE and self.irreversible_value:
            return self.irreversible_value
        return self.value


@dataclass(frozen=True, slots=True)
class ProbeCase:
    scene_id: str
    context: str
    axis: str
    survivor: str
    alternative: str
    first: str
    second: str
    task: str
    a_text: str
    b_text: str

    def letter_of(self, value: str) -> str:
        return "A" if value == self.first else "B"

    def value_of(self, letter: str | None) -> str | None:
        if letter == "A":
            return self.first
        if letter == "B":
            return self.second
        return None


def majority(values: Sequence[str | None]) -> str | None:
    """시행 다수결 - 파싱 실패는 표가 아니고, 동률이면 판정하지 않는다."""
    votes = Counter(v for v in values if v is not None)
    if not votes:
        return None
    ranked = votes.most_common(2)
    if len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    case: ProbeCase
    bare_raws: tuple[str, ...]
    ruled_raws: tuple[str, ...]

    @property
    def bare_raw(self) -> str:
        return self.bare_raws[0] if self.bare_raws else ""

    @property
    def ruled_raw(self) -> str:
        return self.ruled_raws[0] if self.ruled_raws else ""

    @property
    def bare_values(self) -> tuple[str | None, ...]:
        return tuple(self.case.value_of(parse_choice(raw)) for raw in self.bare_raws)

    @property
    def ruled_values(self) -> tuple[str | None, ...]:
        return tuple(self.case.value_of(parse_choice(raw)) for raw in self.ruled_raws)

    @property
    def bare_value(self) -> str | None:
        return majority(self.bare_values)

    @property
    def ruled_value(self) -> str | None:
        return majority(self.ruled_values)

    @property
    def trials(self) -> int:
        return len(self.ruled_raws)

    @property
    def held_trials(self) -> int:
        return sum(v == self.case.survivor for v in self.ruled_values)

    @property
    def held(self) -> bool:
        return self.ruled_value == self.case.survivor

    @property
    def held_every_trial(self) -> bool:
        return self.trials > 0 and self.held_trials == self.trials

    @property
    def moved(self) -> bool:
        return self.held and self.bare_value != self.case.survivor

    def to_dict(self) -> dict[str, object]:
        c = self.case
        return {
            "scene_id": c.scene_id,
            "context": c.context,
            "axis": c.axis,
            "survivor": c.survivor,
            "alternative": c.alternative,
            "shown_as_a": c.first,
            "bare": {
                "raw": self.bare_raw,
                "value": self.bare_value,
                "raws": list(self.bare_raws),
                "values": list(self.bare_values),
            },
            "ruled": {
                "raw": self.ruled_raw,
                "value": self.ruled_value,
                "raws": list(self.ruled_raws),
                "values": list(self.ruled_values),
            },
            "trials": self.trials,
            "held_trials": self.held_trials,
            "held": self.held,
            "held_every_trial": self.held_every_trial,
            "moved": self.moved,
        }


@dataclass(frozen=True, slots=True)
class ProbeReport:
    lang: str
    runner: tuple[str, ...]
    rules_sha256: str
    started_at: str
    outcomes: tuple[ProbeOutcome, ...]
    receipt_path: str | None = None
    repeat: int = 1
    delivery: str = "prompt"
    context_sha256: str | None = None

    @property
    def summary(self) -> dict[str, int]:
        n = len(self.outcomes)
        return {
            "cases": n,
            "held": sum(o.held for o in self.outcomes),
            "held_every_trial": sum(o.held_every_trial for o in self.outcomes),
            "moved": sum(o.moved for o in self.outcomes),
            "bare_matched": sum(
                o.bare_value == o.case.survivor for o in self.outcomes
            ),
            "unparsed": sum(
                o.bare_value is None or o.ruled_value is None for o in self.outcomes
            ),
            "trials": sum(o.trials for o in self.outcomes),
            "trials_held": sum(o.held_trials for o in self.outcomes),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "xout_probe_receipt",
            "lang": self.lang,
            "runner": list(self.runner),
            "rules_sha256": self.rules_sha256,
            "started_at": self.started_at,
            "repeat": self.repeat,
            "delivery": self.delivery,
            "context_sha256": self.context_sha256,
            "summary": self.summary,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


_CHOICE = re.compile(r"(?<![A-Za-z])([AaBb])(?![A-Za-z])")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def parse_choice(raw: str) -> str | None:
    """응답에서 홀로 선 첫 A/B 글자 - 없으면 None (파싱 실패는 실패로 센다).

    터미널 색상 코드(kiro-cli 같은 러너가 섞어 낸다)는 먼저 걷어낸다.
    """
    match = _CHOICE.search(_ANSI.sub("", raw or ""))
    return match.group(1).upper() if match else None


def _order(scene_id: str, axis: str, survivor: str, alternative: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{scene_id}:{axis}".encode("utf-8")).digest()
    return (survivor, alternative) if digest[0] % 2 == 0 else (alternative, survivor)


#: 축별로 생존값과 가장 강하게 대비되는 값. 서로 양립 가능한 값(예: prefer_existing과
#: ask_first)을 A/B로 붙이면 탐침이 아무것도 재지 못한다 - 실측에서 확인된 약점.
OPPOSITE: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "act_then_report",
    ("autonomy", "propose_then_act"): "act_then_report",
    ("autonomy", "act_then_report"): "ask_first",
    ("commit_style", "no_auto_commit"): "conventional",
    ("commit_style", "conventional"): "no_auto_commit",
    ("commit_style", "narrative"): "no_auto_commit",
    ("test_discipline", "test_after"): "test_first",
    ("test_discipline", "test_first"): "test_after",
    ("test_discipline", "on_request"): "test_first",
    ("comment_doc", "minimal"): "thorough",
    ("comment_doc", "docstring_only"): "thorough",
    ("comment_doc", "thorough"): "minimal",
    ("error_behavior", "stop_and_report"): "self_heal",
    ("error_behavior", "retry_then_report"): "self_heal",
    ("error_behavior", "self_heal"): "stop_and_report",
    ("scope_adherence", "strict"): "proactive",
    ("scope_adherence", "adjacent_fix_ok"): "strict",
    ("scope_adherence", "proactive"): "strict",
    ("verification", "always_run"): "trust_static",
    ("verification", "on_risky"): "trust_static",
    ("verification", "trust_static"): "always_run",
    ("dependency_policy", "prefer_existing"): "free",
    ("dependency_policy", "ask_first"): "free",
    ("dependency_policy", "free"): "ask_first",
}


def _alternative(axis: str, survivor: str, spec: RuleSpec, context: str) -> str:
    """가장 날카로운 대안: 정반대 값 > 사용자가 지운 값 > 다른 맥락의 생존값 > 카탈로그 순서."""
    opposite = OPPOSITE.get((axis, survivor))
    if opposite and opposite != survivor:
        return opposite
    for value in spec.eliminated:
        if value != survivor:
            return value
    other = spec.value if context == CONTEXT_IRREVERSIBLE else spec.irreversible_value
    if other and other != survivor:
        return other
    return next(v for v in DEFAULT_CATALOG[axis] if v != survivor)


def build_cases(
    pack: FixturePack,
    rules: Mapping[str, RuleSpec],
    skin: RepoSkin,
    axes: Sequence[str] | None = None,
) -> tuple[ProbeCase, ...]:
    cases: list[ProbeCase] = []
    for scene in pack.scenes:
        task = "\n".join(
            _substitute(seg.text, skin) for seg in scene.skeleton if seg.kind == KIND_STATIC
        )
        for axis in scene.slot_axes:
            if axes and axis not in axes:
                continue
            spec = rules.get(axis)
            if spec is None:
                continue
            survivor = spec.survivor_for(scene.context)
            alternative = _alternative(axis, survivor, spec, scene.context)
            first, second = _order(scene.scene_id, axis, survivor, alternative)
            slots = scene.axis_slots[axis]
            cases.append(
                ProbeCase(
                    scene_id=scene.scene_id,
                    context=scene.context,
                    axis=axis,
                    survivor=survivor,
                    alternative=alternative,
                    first=first,
                    second=second,
                    task=task,
                    a_text=_substitute(slots[first], skin),
                    b_text=_substitute(slots[second], skin),
                )
            )
    return tuple(cases)


def build_prompt(
    case: ProbeCase,
    lang: str,
    rules_text: str | None,
    context_text: str | None = None,
) -> str:
    """방해 문서(context) -> 규칙 -> 장면 순서. 규칙이 문서 뒤에 묻히는 어려운 배치다."""
    texts = PROMPT.get(lang) or PROMPT["ko"]
    body = texts["frame"].format(task=case.task, a=case.a_text, b=case.b_text)
    if rules_text is not None:
        body = texts["ruled"].format(rules=rules_text.rstrip("\n") + "\n") + body
    if context_text is not None and context_text.strip():
        body = texts["context"].format(context=context_text.rstrip("\n") + "\n") + body
    return body


Runner = Callable[[str], str]


def subprocess_runner(command: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> Runner:
    """프롬프트를 마지막 인자로 받아 stdout에 답하는 외부 CLI를 러너로 감싼다."""
    if not command:
        raise ProbeError("empty runner command")
    if shutil.which(command[0]) is None:
        raise ProbeError(f"runner not found: {command[0]}")

    def run(prompt: str) -> str:
        completed = subprocess.run(
            [*command, prompt],
            stdin=subprocess.DEVNULL,  # codex exec 같은 러너가 stdin을 기다리지 않게
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise ProbeError(
                f"runner exited {completed.returncode}: {completed.stderr.strip()[:300]}"
            )
        return completed.stdout

    return run


def probe(
    cases: Sequence[ProbeCase],
    rules_text: str,
    runner: Runner,
    lang: str,
    runner_command: Sequence[str] = DEFAULT_RUNNER,
    on_outcome: Callable[[ProbeOutcome], None] | None = None,
    now: str | None = None,
    repeat: int = 1,
    context_text: str | None = None,
    rules_in_prompt: bool = True,
    phase_hook: Callable[[str], None] | None = None,
    delivery: str = "prompt",
) -> ProbeReport:
    """케이스마다 bare/ruled를 repeat번씩 묻는다.

    rules_in_prompt=False면 두 프롬프트가 같고, 차이는 phase_hook("bare") /
    phase_hook("ruled")가 바깥에서 만든다 (도구의 규칙 파일에서 블록을 뺐다 넣기).
    그때는 bare 패스를 전부 돌린 뒤 ruled 패스를 돈다 - 파일을 케이스마다
    뒤집지 않기 위해서다.
    """
    repeat = max(1, int(repeat))
    ruled_text = rules_text if rules_in_prompt else None

    def ask(case: ProbeCase, with_rules: bool) -> tuple[str, ...]:
        prompt = build_prompt(case, lang, ruled_text if with_rules else None, context_text)
        return tuple(runner(prompt) for _ in range(repeat))

    outcomes: list[ProbeOutcome] = []
    if phase_hook is None:
        for case in cases:
            outcome = ProbeOutcome(case=case, bare_raws=ask(case, False), ruled_raws=ask(case, True))
            outcomes.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
    else:
        phase_hook("bare")
        bare_by_case = [ask(case, False) for case in cases]
        phase_hook("ruled")
        for case, bare in zip(cases, bare_by_case):
            outcome = ProbeOutcome(case=case, bare_raws=bare, ruled_raws=ask(case, True))
            outcomes.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
    return ProbeReport(
        lang=lang,
        runner=tuple(runner_command),
        rules_sha256=hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
        started_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        outcomes=tuple(outcomes),
        repeat=repeat,
        delivery=delivery,
        context_sha256=(
            hashlib.sha256(context_text.encode("utf-8")).hexdigest() if context_text else None
        ),
    )


def write_receipt(base_dir: Path, report: ProbeReport) -> Path:
    """영수증은 소유 디렉토리 안(~/.claude/xout/probes/)에만 쓴다 - 원장은 건드리지 않는다."""
    directory = base_dir / PROBE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9T]", "", report.started_at.split("+")[0])
    path = directory / f"probe-{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"probe-{stamp}-{counter}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
