"""효과 탐침 - 컴파일된 규칙이 실제 에이전트의 선택을 움직이는지 잰다.

세션은 LLM을 호출하지 않는다는 불변식은 그대로다. 탐침은 세션 밖에서
사용자가 명시적으로 켜는 별도 명령이고, 러너는 외부 CLI(기본 `claude -p`)다.
같은 장면 프롬프트를 규칙 없이 한 번, 착지된 XOUT.md 본문을 앞세워 한 번
던져 A/B 선택이 생존값 쪽으로 움직였는지 영수증(JSON)으로 남긴다.

측정 단위는 페어와 같다: 한 장면, 한 축, 생존값 대 대안. 두 응답의 A/B
순서는 장면·축 해시로 섞어 자리 편향을 흩는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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
    },
    "en": {
        "frame": (
            "You are the coding agent in the exchange below.\n\n{task}\n\n"
            "Two ways you could respond next:\n\nA) {a}\n\nB) {b}\n\n"
            "Under the instructions you are actually operating with, which one do you do? "
            "Answer with exactly one letter, A or B, and nothing else."
        ),
        "ruled": "Standing instructions from the person you work for:\n\n{rules}\n---\n\n",
    },
    "ja": {
        "frame": (
            "あなたは以下のやり取りに登場するコーディングエージェントです。\n\n{task}\n\n"
            "次の応答として考えられるのは二つ:\n\nA) {a}\n\nB) {b}\n\n"
            "いま実際に従っている指示のもとでは、どちらを選びますか? "
            "A か B の一文字だけで答えてください。"
        ),
        "ruled": "一緒に働く相手から常に受けている指示:\n\n{rules}\n---\n\n",
    },
    "zh": {
        "frame": (
            "你是下面这段对话里的编码智能体。\n\n{task}\n\n"
            "接下来有两种回应可选:\n\nA) {a}\n\nB) {b}\n\n"
            "按照你现在实际遵循的指示，你会选哪一个? 只回答一个字母，A 或 B。"
        ),
        "ruled": "和你一起工作的人给的固定指示:\n\n{rules}\n---\n\n",
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


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    case: ProbeCase
    bare_raw: str
    ruled_raw: str

    @property
    def bare_value(self) -> str | None:
        return self.case.value_of(parse_choice(self.bare_raw))

    @property
    def ruled_value(self) -> str | None:
        return self.case.value_of(parse_choice(self.ruled_raw))

    @property
    def held(self) -> bool:
        return self.ruled_value == self.case.survivor

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
            "bare": {"raw": self.bare_raw, "value": self.bare_value},
            "ruled": {"raw": self.ruled_raw, "value": self.ruled_value},
            "held": self.held,
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

    @property
    def summary(self) -> dict[str, int]:
        n = len(self.outcomes)
        return {
            "cases": n,
            "held": sum(o.held for o in self.outcomes),
            "moved": sum(o.moved for o in self.outcomes),
            "bare_matched": sum(
                o.bare_value == o.case.survivor for o in self.outcomes
            ),
            "unparsed": sum(
                o.bare_value is None or o.ruled_value is None for o in self.outcomes
            ),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "xout_probe_receipt",
            "lang": self.lang,
            "runner": list(self.runner),
            "rules_sha256": self.rules_sha256,
            "started_at": self.started_at,
            "summary": self.summary,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


_CHOICE = re.compile(r"(?<![A-Za-z])([AaBb])(?![A-Za-z])")


def parse_choice(raw: str) -> str | None:
    """응답에서 홀로 선 첫 A/B 글자 - 없으면 None (파싱 실패는 실패로 센다)."""
    match = _CHOICE.search(raw or "")
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


def build_prompt(case: ProbeCase, lang: str, rules_text: str | None) -> str:
    texts = PROMPT.get(lang) or PROMPT["ko"]
    body = texts["frame"].format(task=case.task, a=case.a_text, b=case.b_text)
    if rules_text is None:
        return body
    return texts["ruled"].format(rules=rules_text.rstrip("\n") + "\n") + body


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
) -> ProbeReport:
    outcomes: list[ProbeOutcome] = []
    for case in cases:
        bare = runner(build_prompt(case, lang, None))
        ruled = runner(build_prompt(case, lang, rules_text))
        outcome = ProbeOutcome(case=case, bare_raw=bare, ruled_raw=ruled)
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
    return ProbeReport(
        lang=lang,
        runner=tuple(runner_command),
        rules_sha256=hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
        started_at=now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        outcomes=tuple(outcomes),
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
