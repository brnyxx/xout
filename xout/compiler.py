"""AC4 - 세션 이벤트를 8축 실행 룰(XOUT.md)과 인식론 사이드카(manifest.json)로 컴파일한다.

컴파일러는 순수 fold 파생만 사용한다. 카운터/등급/판정은 저장하지 않고 매 호출마다
이벤트 스트림에서 다시 계산한다. 산출 경로는 ~/.claude/popper/ 단독이며 사용자 파일에는
쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from xout.atomic import atomic_write_text
from xout.counter import DEFAULT_CATALOG, AxisCatalog, fold
from xout.events import EventType, StrikeEvent
from xout.fixtures import CONTEXT_IRREVERSIBLE, CONTEXT_ROUTINE, SCENE_CONTEXTS
from xout.locking import base_lock

logger = logging.getLogger(__name__)

CATALOG_VERSION = "v2"
METRIC_SPEC_VERSION = "v1"
MANIFEST_VERSION = "1"
SCOPE = "global"

XOUT_MD = "XOUT.md"
MANIFEST_JSON = "manifest.json"
SETTINGS_JSON = "settings.xout.json"
OUTPUT_FILES = (XOUT_MD, MANIFEST_JSON, SETTINGS_JSON)

DEFAULT_PREREG_REF = "docs/prereg/prereg_sealed.json"

GRADE_DISCRIMINATED = "discriminated"
GRADE_INDISCRIMINATE = "indiscriminate"
GRADE_UNTESTED = "untested"
GRADE_UNSTABLE = "unstable"

GRADE_LABELS = {
    GRADE_DISCRIMINATED: "판별시험 통과",
    GRADE_INDISCRIMINATE: "무차별 생존",
    GRADE_UNTESTED: "완전 미시험",
    GRADE_UNSTABLE: "불안정",
}

SOURCE_ELICITED = "elicited"
SOURCE_MINED_PRIOR = "mined-prior"

RECHECK_CLASS_PRIORITY = ("unstable", "untested-prior", "conflict")

# 채굴 prior 순위 - DEFAULT_CATALOG 튜플 순서를 내림차순 prior로 해석한다.
# index 0 이 커뮤니티 최빈값(채굴 모드)이며, 생존값이 여러 개면 이 순위가 높은 값을 방출한다.
MINED_PRIOR: dict[str, tuple[str, ...]] = {
    axis: tuple(values) for axis, values in DEFAULT_CATALOG.items()
}

RULE_TEXT: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "코드를 수정하기 전에 계획을 제시하고 승인을 받는다. 명백한 오타 수정처럼 자명한 변경만 예외다.",
    ("autonomy", "propose_then_act"): "짧은 계획을 먼저 적고 곧바로 이어서 실행한다.",
    ("autonomy", "act_then_report"): "먼저 실행하고 변경 내역을 요약해 보고한다.",
    ("commit_style", "conventional"): "커밋 메시지는 feat/fix/refactor 같은 conventional prefix로 시작한다. 리포에 이미 자리잡은 커밋 컨벤션이 있으면 그것을 우선한다.",
    ("commit_style", "narrative"): "커밋 메시지 제목은 변경 의도를 서술형 문장으로 적는다. 리포에 이미 자리잡은 커밋 컨벤션이 있으면 그것을 우선한다.",
    ("commit_style", "no_auto_commit"): "요청받지 않은 커밋은 만들지 않는다. 변경은 워킹 트리에 남겨 사용자가 확인 후 직접 커밋하게 한다.",
    ("test_discipline", "test_first"): "버그 수정은 재현 테스트를 먼저 작성해 실패를 확인한 뒤 고친다. 기능 구현도 테스트를 먼저 쓴다.",
    ("test_discipline", "test_after"): "구현 직후 같은 변경 안에서 테스트를 추가한다. 테스트 없는 변경을 완료로 선언하지 않는다.",
    ("test_discipline", "on_request"): "테스트는 명시적으로 요청받았을 때만 작성한다. 단, 기존 테스트가 깨지는지 확인은 항상 한다.",
    ("comment_doc", "minimal"): "주석은 코드로 표현할 수 없는 제약과 이유에만 남긴다. 코드가 하는 일을 다시 서술하는 주석은 쓰지 않는다.",
    ("comment_doc", "docstring_only"): "공개 함수와 클래스에는 docstring을 쓰고, 인라인 주석은 남기지 않는다.",
    ("comment_doc", "thorough"): "공개 API에는 docstring을 쓰고, 비자명한 로직에만 인라인 주석을 남긴다.",
    ("error_behavior", "stop_and_report"): "에러가 나면 즉시 멈추고 원문 로그 그대로 보고한다. 로그를 요약하거나 가공하지 않는다.",
    ("error_behavior", "retry_then_report"): "일시적으로 보이는 실패는 한 번만 재시도한다. 그래도 실패하면 원문 로그와 함께 보고하고 멈춘다.",
    ("error_behavior", "self_heal"): "테스트나 빌드 실패는 원인을 고쳐 통과할 때까지 진행한 뒤 결과를 보고한다.",
    ("scope_adherence", "strict"): "요청받은 범위 밖의 파일은 수정하지 않는다. 범위 밖 결함을 발견하면 고치지 말고 보고만 한다.",
    ("scope_adherence", "adjacent_fix_ok"): "요청 범위와 직접 맞닿은 결함까지만 같은 변경에서 고치고, 그 사실을 보고에 명시한다. 그 밖의 발견은 보고만 한다.",
    ("scope_adherence", "proactive"): "작업 중 발견한 개선점은 같은 변경에 포함하되, 요청 범위와 부수 정리를 보고에서 구분해 적는다. diff가 요청보다 커지면 사전에 알린다.",
    ("verification", "always_run"): "완료를 선언하기 전에 테스트와 빌드를 실제로 돌려 통과 출력을 확인한다.",
    ("verification", "on_risky"): "위험한 변경일 때만 전체 검증을 돌리고, 평소에는 변경과 직접 관련된 테스트만 확인한다.",
    ("verification", "trust_static"): "정적 확인(코드 재독, 타입 체크)으로 충분하다고 판단되면 그대로 완료를 선언한다.",
    ("dependency_policy", "prefer_existing"): "새 패키지보다 기존 의존성과 표준 라이브러리를 우선한다. 불가피하게 추가하면 그 사실을 보고한다.",
    ("dependency_policy", "ask_first"): "새 의존성은 추가하기 전에 반드시 확인을 받는다.",
    ("dependency_policy", "free"): "필요한 의존성은 바로 추가하고, 추가한 목록을 보고에 남긴다.",
}

#: 맥락 분기 규칙의 되돌리기-어려운-작업 절 - 앞에 조건 접두가 붙는다.
IRREVERSIBLE_CLAUSE: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "실행 전에 반드시 승인을 받는다",
    ("autonomy", "propose_then_act"): "계획을 알린 뒤 진행하되 최종 적용은 승인을 기다린다",
    ("autonomy", "act_then_report"): "먼저 실행하고 결과를 보고한다",
    ("error_behavior", "stop_and_report"): "에러 즉시 멈추고 원문 로그로 보고한다",
    ("error_behavior", "retry_then_report"): "일시적 실패만 한 번 재시도하고 이후에는 멈춰 보고한다",
    ("error_behavior", "self_heal"): "복구 로직을 넣어 끝까지 진행한 뒤 결과를 보고한다",
    ("verification", "always_run"): "적용 전에 사본 리허설과 롤백 검증까지 실제로 돌린다",
    ("verification", "on_risky"): "전체 검증과 리허설을 반드시 돌린다",
    ("verification", "trust_static"): "정적 확인만으로 완료를 선언한다",
    ("dependency_policy", "prefer_existing"): "기존 의존성만으로 해결한다",
    ("dependency_policy", "ask_first"): "새 도구 설치 전에 반드시 확인을 받는다",
    ("dependency_policy", "free"): "필요한 도구를 바로 설치해 진행한다",
    ("commit_style", "conventional"): "커밋은 만들되 리포 컨벤션을 따른다",
    ("commit_style", "narrative"): "커밋 제목은 변경 의도를 서술형으로 적는다",
    ("commit_style", "no_auto_commit"): "커밋을 만들지 않고 변경을 워킹 트리에 남긴다",
}

IRREVERSIBLE_CONDITION_PREFIX = (
    "단, 삭제, push, 배포, 마이그레이션처럼 되돌리기 어려운 작업에서는 "
)

RULE_TEXT_EN: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "Present a plan and get approval before modifying code. The only exception is self-evident changes like obvious typo fixes.",
    ("autonomy", "propose_then_act"): "Write a short plan first, then proceed immediately.",
    ("autonomy", "act_then_report"): "Act first, then report a summary of what changed.",
    ("commit_style", "conventional"): "Start commit messages with a conventional prefix like feat/fix/refactor. If the repo already has an established commit convention, that convention wins.",
    ("commit_style", "narrative"): "Write commit titles as narrative sentences describing the intent of the change. If the repo already has an established commit convention, that convention wins.",
    ("commit_style", "no_auto_commit"): "Never create commits that were not requested. Leave changes in the working tree for the user to review and commit themselves.",
    ("test_discipline", "test_first"): "For bug fixes, write a reproducing test first and watch it fail before fixing. For features, write the tests first too.",
    ("test_discipline", "test_after"): "Add tests in the same change right after implementing. Never declare a change done without tests.",
    ("test_discipline", "on_request"): "Write tests only when explicitly asked. Always check whether existing tests break, though.",
    ("comment_doc", "minimal"): "Leave comments only for constraints and reasons the code cannot express. Never write comments that restate what the code does.",
    ("comment_doc", "docstring_only"): "Write docstrings on public functions and classes; leave no inline comments.",
    ("comment_doc", "thorough"): "Write docstrings on public APIs and leave inline comments only on non-obvious logic.",
    ("error_behavior", "stop_and_report"): "On error, stop immediately and report the raw log verbatim. Never summarize or massage the log.",
    ("error_behavior", "retry_then_report"): "Retry a transient-looking failure exactly once. If it still fails, report with the raw log and stop.",
    ("error_behavior", "self_heal"): "On test or build failures, fix the cause and keep going until they pass, then report the outcome.",
    ("scope_adherence", "strict"): "Never modify files outside the requested scope. If you find a defect outside the scope, report it without fixing it.",
    ("scope_adherence", "adjacent_fix_ok"): "Fix defects directly adjacent to the requested scope in the same change and say so in the report. Anything further out gets reported only.",
    ("scope_adherence", "proactive"): "Include improvements discovered during the work in the same change, but separate the requested scope from incidental cleanup in the report. If the diff grows beyond the request, flag it beforehand.",
    ("verification", "always_run"): "Before declaring done, actually run the tests and the build and confirm the passing output.",
    ("verification", "on_risky"): "Run full verification only for risky changes; otherwise check just the tests directly related to the change.",
    ("verification", "trust_static"): "When static checks (re-reading the code, type checking) seem sufficient, declare done on that basis.",
    ("dependency_policy", "prefer_existing"): "Favor existing dependencies and the standard library over new packages. If an addition is unavoidable, report it.",
    ("dependency_policy", "ask_first"): "Always ask before adding a new dependency.",
    ("dependency_policy", "free"): "Add whatever dependencies are needed right away, and list the additions in the report.",
}

IRREVERSIBLE_CLAUSE_EN: dict[tuple[str, str], str] = {
    ("autonomy", "ask_first"): "always get approval before executing",
    ("autonomy", "propose_then_act"): "announce the plan and proceed, but wait for approval on the final apply",
    ("autonomy", "act_then_report"): "act first and report the result",
    ("error_behavior", "stop_and_report"): "stop on error immediately and report with the raw log",
    ("error_behavior", "retry_then_report"): "retry only transient failures once, then stop and report",
    ("error_behavior", "self_heal"): "add recovery logic, run through to the end, then report the result",
    ("verification", "always_run"): "actually run a copy rehearsal and a rollback check before applying",
    ("verification", "on_risky"): "always run full verification and a rehearsal",
    ("verification", "trust_static"): "declare done on static checks alone",
    ("dependency_policy", "prefer_existing"): "solve it with existing dependencies only",
    ("dependency_policy", "ask_first"): "always ask before installing any new tool",
    ("dependency_policy", "free"): "install whatever tools are needed and proceed",
    ("commit_style", "conventional"): "create commits but follow the repo convention",
    ("commit_style", "narrative"): "write commit titles as narrative intent",
    ("commit_style", "no_auto_commit"): "create no commits and leave changes in the working tree",
}

IRREVERSIBLE_CONDITION_PREFIX_EN = (
    "However, for hard-to-reverse work like deletes, pushes, deploys, and migrations, "
)

#: 언어별 규칙 문안 테이블 - 이벤트 원장은 언어 중립이고 언어는 컴파일 시점 렌더 선택이다.
RULE_LANG_TABLES: dict[str, tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], str]] = {
    "ko": (RULE_TEXT, IRREVERSIBLE_CLAUSE, IRREVERSIBLE_CONDITION_PREFIX),
    "en": (RULE_TEXT_EN, IRREVERSIBLE_CLAUSE_EN, IRREVERSIBLE_CONDITION_PREFIX_EN),
}

DEFAULT_RULE_LANG = "ko"

# XOUT.md 본문에 절대 실려서는 안 되는 인식론 어휘.
EPISTEMIC_TOKENS: tuple[str, ...] = (
    "corroboration",
    "corroborated",
    "grade",
    "untested",
    "unstable",
    "mined-prior",
    "mined prior",
    "elicited",
    "provenance",
    "n=0",
    "미시험",
    "무차별",
    "불안정",
    "판별시험",
    "반증",
    "추측",
    "가설",
    "prior",
    "TODO",
)


class CompileViolation(ValueError):
    """컴파일 입력이 스키마를 위반했을 때."""


class HashMismatch(RuntimeError):
    """마지막 쓰기 content hash와 디스크 현재 내용이 다를 때 - silent overwrite 금지."""

    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.records = tuple(dict(record) for record in records)
        paths = ", ".join(str(record.get("path")) for record in self.records)
        super().__init__(f"content hash mismatch: {paths}")


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """XOUT.md 한 줄과 manifest 한 항목이 공유하는 단일 룰."""

    axis: str
    value: str
    text: str
    corroboration_grade: str
    value_source: str
    surviving: tuple[str, ...]
    eliminated: tuple[str, ...]
    provenance: tuple[str, ...] = ()
    pair_struck: bool = False
    demoted: bool = False

    @property
    def rule_id(self) -> str:
        return f"{CATALOG_VERSION}:{self.axis}:{self.value}"

    @property
    def grade_label(self) -> str:
        return GRADE_LABELS[self.corroboration_grade]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "axis": self.axis,
            "value": self.value,
            "rule": self.text,
            "catalog_version": CATALOG_VERSION,
            "corroboration_grade": self.corroboration_grade,
            "corroboration_label": self.grade_label,
            "value_source": self.value_source,
            "surviving_values": list(self.surviving),
            "eliminated_values": list(self.eliminated),
            "refutation_provenance": list(self.provenance),
            "pair_struck": self.pair_struck,
            "demoted_to_untested": self.demoted,
        }


@dataclass(frozen=True, slots=True)
class WriteResult:
    """write_outputs 반환값 - 착지 경로와 방출된 manifest."""

    base_dir: Path
    manifest: dict[str, Any]
    written: tuple[Path, ...] = ()
    mismatches: tuple[dict[str, Any], ...] = field(default=())

    def path(self, name: str) -> Path:
        return self.base_dir / name


def default_base_dir() -> Path:
    """xout 단독 소유 산출 디렉터리."""
    return Path.home() / ".claude" / "xout"


def _now(now: str | None = None) -> str:
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


def content_hash(payload: str | bytes) -> str:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def mined_prior_rank(axis: str, value: str) -> int:
    """작을수록 강한 채굴 prior. 카탈로그에 없는 값은 맨 뒤로 민다."""
    order = MINED_PRIOR.get(axis, ())
    if value in order:
        return order.index(value)
    return len(order) + 1


def mined_mode(axis: str) -> str:
    """축의 채굴 최빈값 - 반증 이력 0건 축이 방출하는 값."""
    order = MINED_PRIOR.get(axis)
    if not order:
        raise CompileViolation(f"unknown axis: {axis}")
    return order[0]


def select_value(axis: str, surviving: Sequence[str]) -> str:
    """생존값 중 채굴 prior가 가장 강한 값.

    생존 1개면 그 값, 생존 2개면 prior 상위값, 반증 0건(생존 3개)이면 채굴 최빈값으로
    세 갈래가 하나의 규칙으로 수렴한다.
    """
    if not surviving:
        return mined_mode(axis)
    return min(surviving, key=lambda value: (mined_prior_rank(axis, value), value))


def _probe_flip_axes(events: Iterable[Any]) -> frozenset[str]:
    flipped: set[str] = set()
    for event in events:
        if getattr(event, "type", None) is not EventType.PROBE_RESULT:
            continue
        payload = getattr(event, "payload", None) or {}
        result = str(payload.get("result", "")).strip().lower()
        if result != "flip" and payload.get("flip") is not True:
            continue
        axis = payload.get("axis")
        if axis:
            flipped.add(str(axis))
    return frozenset(flipped)


def _strike_scan(events: Iterable[Any]) -> tuple[frozenset[str], dict[str, list[str]]]:
    """pair-strike만 받은 축 후보와 축별 반증 provenance를 원본 스트림에서 긁어온다."""
    pair_struck: set[str] = set()
    provenance: dict[str, list[str]] = {}
    for event in events:
        if getattr(event, "type", None) is not EventType.STRIKE:
            continue
        if not getattr(event, "has_discriminating_power", False):
            axis = getattr(event, "axis", None)
            if axis:
                pair_struck.add(str(axis))
            continue
        for refutation in getattr(event, "refutations", ()):
            provenance.setdefault(str(refutation.axis), []).append(str(event.event_id))
    return frozenset(pair_struck), provenance


def _grade(
    axis: str,
    discrimination: str,
    *,
    flipped: bool,
    pair_struck: bool,
) -> str:
    if flipped:
        return GRADE_UNSTABLE
    if discrimination == "complete":
        return GRADE_DISCRIMINATED
    if discrimination == "partial":
        return GRADE_INDISCRIMINATE
    if pair_struck:
        return GRADE_INDISCRIMINATE
    return GRADE_UNTESTED


def _context_stream(
    stream: Sequence[Any], context: str
) -> tuple[Any, ...]:
    """맥락 클래스에 속한 긋기만 남긴다 - 비긋기 이벤트(undo/revive)는 양쪽에 남는다."""
    kept: list[Any] = []
    for event in stream:
        if isinstance(event, StrikeEvent):
            if SCENE_CONTEXTS.get(event.scene_id, CONTEXT_ROUTINE) == context:
                kept.append(event)
        else:
            kept.append(event)
    return tuple(kept)


def _lang_tables(
    lang: str,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str], str]:
    tables = RULE_LANG_TABLES.get(lang)
    if tables is None:
        raise CompileViolation(f"unsupported rule language: {lang!r}")
    return tables


def conditional_rule_text(
    axis: str,
    routine_value: str,
    irreversible_value: str,
    lang: str = DEFAULT_RULE_LANG,
) -> str:
    """두 맥락의 생존값이 갈릴 때 - 평시 문장 + 되돌리기-어려운-작업 절."""
    rule_text, clauses, prefix = _lang_tables(lang)
    clause = clauses[(axis, irreversible_value)]
    return f"{rule_text[(axis, routine_value)]} {prefix}{clause}."


def compile_rules(
    events: Iterable[Any],
    catalog: AxisCatalog | None = None,
    lang: str = DEFAULT_RULE_LANG,
) -> tuple[CompiledRule, ...]:
    """이벤트 스트림에서 8축 전부의 실행 룰을 파생한다(순수 함수, 항상 8개)."""
    rule_text, _, _ = _lang_tables(lang)
    stream = tuple(events)
    active = dict(catalog) if catalog is not None else dict(DEFAULT_CATALOG)
    state = fold(stream, catalog)
    routine_state = fold(_context_stream(stream, CONTEXT_ROUTINE), catalog)
    irreversible_state = fold(_context_stream(stream, CONTEXT_IRREVERSIBLE), catalog)
    flipped_axes = _probe_flip_axes(stream)
    pair_struck_axes, provenance = _strike_scan(stream)

    rules: list[CompiledRule] = []
    for axis in sorted(active):
        axis_state = state.axis(axis)
        surviving = tuple(axis_state.surviving)
        value = select_value(axis, surviving)
        routine_axis = routine_state.axis(axis)
        irreversible_axis = irreversible_state.axis(axis)
        if routine_axis.eliminated and irreversible_axis.eliminated:
            routine_value = select_value(axis, tuple(routine_axis.surviving))
            irreversible_value = select_value(
                axis, tuple(irreversible_axis.surviving)
            )
            if routine_value != irreversible_value:
                value = routine_value
                text = conditional_rule_text(
                    axis, routine_value, irreversible_value, lang
                )
            else:
                value = routine_value
                text = rule_text.get((axis, value))
        else:
            text = rule_text.get((axis, value))
        if text is None:
            raise CompileViolation(f"no executable rule text for {axis}={value}")
        pair_struck = axis in pair_struck_axes
        grade = _grade(
            axis,
            axis_state.effective_discrimination,
            flipped=axis in flipped_axes,
            pair_struck=pair_struck,
        )
        source = SOURCE_ELICITED if axis_state.eliminated else SOURCE_MINED_PRIOR
        rules.append(
            CompiledRule(
                axis=axis,
                value=value,
                text=text,
                corroboration_grade=grade,
                value_source=source,
                surviving=surviving,
                eliminated=tuple(axis_state.eliminated),
                provenance=tuple(provenance.get(axis, ())),
                pair_struck=pair_struck,
                demoted=bool(axis_state.demoted),
            )
        )
    return tuple(rules)


def render_xout_md(rules: Sequence[CompiledRule]) -> str:
    """실행 가능한 룰만 렌더한다 - 인식론 주석 0줄."""
    lines = ["# xout Rules", ""]
    lines.extend(f"- {rule.text}" for rule in rules)
    return "\n".join(lines) + "\n"


def render_settings(rules: Sequence[CompiledRule]) -> str:
    """제안 파일 - 라이브 settings.json에 자동 병합하지 않는다."""
    payload = {
        "_popper": {
            "proposal_only": True,
            "catalog_version": CATALOG_VERSION,
            "scope": SCOPE,
            "rules_ref": XOUT_MD,
        },
        "recommended_hooks": [],
        "rule_ids": [rule.rule_id for rule in rules],
    }
    return _canonical(payload)


def _recheck_queue(rules: Sequence[CompiledRule], conflicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for rule in rules:
        if rule.corroboration_grade == GRADE_UNSTABLE:
            queue.append({"class": "unstable", "axis": rule.axis, "rule_id": rule.rule_id})
    for rule in rules:
        if rule.corroboration_grade == GRADE_UNSTABLE:
            continue
        if rule.demoted or rule.corroboration_grade == GRADE_UNTESTED:
            queue.append({"class": "untested-prior", "axis": rule.axis, "rule_id": rule.rule_id})
    for conflict in conflicts:
        entry = {"class": "conflict"}
        entry.update(dict(conflict))
        queue.append(entry)
    for index, entry in enumerate(queue):
        entry["priority"] = RECHECK_CLASS_PRIORITY.index(str(entry["class"]))
        entry["order"] = index
    return queue


def build_manifest(
    rules: Sequence[CompiledRule],
    *,
    documents: Mapping[str, str],
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    remaining_combinations: int | None = None,
    eliminated_pairs: int | None = None,
) -> dict[str, Any]:
    """사이드카 manifest - 인식론 메타데이터, 파일 단위 content hash, last_review, scope."""
    stamp = _now(now)
    outputs: dict[str, Any] = {}
    for name in OUTPUT_FILES:
        if name == MANIFEST_JSON:
            continue
        body = documents.get(name)
        if body is None:
            continue
        outputs[name] = {"content_hash": content_hash(body), "bytes": len(body.encode("utf-8"))}
    outputs[MANIFEST_JSON] = {"content_hash": None, "self_excluding": True}

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "catalog_version": CATALOG_VERSION,
        "metric_spec_version": METRIC_SPEC_VERSION,
        "scope": SCOPE,
        "session_id": session_id,
        "generated_at": stamp,
        "last_review": stamp,
        "prereg_ref": prereg_ref,
        "remaining_combinations": remaining_combinations,
        "eliminated_pairs": eliminated_pairs,
        "rules": [rule.to_dict() for rule in rules],
        "outputs": outputs,
        "conflicts": [dict(conflict) for conflict in conflicts],
        "recheck_queue": _recheck_queue(rules, conflicts),
    }
    manifest["outputs"][MANIFEST_JSON]["content_hash"] = manifest_self_hash(manifest)
    return manifest


def manifest_self_hash(manifest: Mapping[str, Any]) -> str:
    """manifest 자기 참조 해시 - outputs['manifest.json'].content_hash 필드를 제외하고 계산한다."""
    shadow = json.loads(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    entry = shadow.get("outputs", {}).get(MANIFEST_JSON)
    if isinstance(entry, dict):
        entry.pop("content_hash", None)
    return content_hash(_canonical(shadow))


def _recorded_hash(manifest: Mapping[str, Any], name: str) -> str | None:
    entry = manifest.get("outputs", {}).get(name)
    if not isinstance(entry, Mapping):
        return None
    recorded = entry.get("content_hash")
    return str(recorded) if recorded else None


def verify_outputs(base_dir: Path) -> tuple[dict[str, Any], ...]:
    """디스크 현재 내용과 manifest에 적힌 마지막 쓰기 hash를 대조한다.

    반환값은 hash_mismatch 이벤트 payload 목록(축 귀속 없음, arity 0)이다.
    """
    manifest_path = base_dir / MANIFEST_JSON
    if not manifest_path.exists():
        return ()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("manifest 파싱 실패: %s", manifest_path, exc_info=True)
        raise HashMismatch(
            [{"path": str(manifest_path), "reason": "unreadable_manifest", "detail": str(exc)}]
        ) from exc

    records: list[dict[str, Any]] = []
    for name in OUTPUT_FILES:
        recorded = _recorded_hash(manifest, name)
        if recorded is None:
            continue
        target = base_dir / name
        if not target.exists():
            records.append({"path": str(target), "reason": "missing", "recorded_hash": recorded})
            continue
        body = target.read_text(encoding="utf-8")
        actual = manifest_self_hash(manifest) if name == MANIFEST_JSON else content_hash(body)
        if name == MANIFEST_JSON:
            try:
                actual = manifest_self_hash(json.loads(body))
            except json.JSONDecodeError as exc:
                raise HashMismatch(
                    [{"path": str(target), "reason": "unreadable_manifest", "detail": str(exc)}]
                ) from exc
        if actual != recorded:
            records.append(
                {
                    "path": str(target),
                    "reason": "manual_edit",
                    "recorded_hash": recorded,
                    "actual_hash": actual,
                }
            )
    return tuple(records)


def _write_outputs_unlocked(
    events: Iterable[Any],
    *,
    catalog: AxisCatalog | None = None,
    base_dir: Path | None = None,
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    acknowledge_mismatch: bool = False,
    lang: str = DEFAULT_RULE_LANG,
) -> WriteResult:
    """~/.claude/popper/ 안에만 XOUT.md + manifest.json + settings.xout.json을 착지시킨다."""
    target_dir = Path(base_dir) if base_dir is not None else default_base_dir()

    mismatches = verify_outputs(target_dir) if target_dir.exists() else ()
    if mismatches and not acknowledge_mismatch:
        logger.warning("content hash 불일치 - silent overwrite 금지: %s", mismatches)
        raise HashMismatch(mismatches)

    stream = tuple(events)
    rules = compile_rules(stream, catalog, lang)
    state = fold(stream, catalog)

    popper_md = render_xout_md(rules)
    settings = render_settings(rules)
    manifest = build_manifest(
        rules,
        documents={XOUT_MD: popper_md, SETTINGS_JSON: settings},
        session_id=session_id,
        now=now,
        conflicts=conflicts,
        prereg_ref=prereg_ref,
        remaining_combinations=state.remaining_combinations,
        eliminated_pairs=state.eliminated_pairs,
    )
    if mismatches:
        manifest["hash_mismatch_records"] = [dict(record) for record in mismatches]
        # 기록 주입 후 자기 해시를 재계산해야 디스크 내용과 hash 대조가 다시 성립한다.
        manifest["outputs"][MANIFEST_JSON]["content_hash"] = manifest_self_hash(manifest)

    target_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        XOUT_MD: popper_md,
        SETTINGS_JSON: settings,
        MANIFEST_JSON: _canonical(manifest),
    }
    written_by_name: dict[str, Path] = {}
    # manifest를 마지막에 교체해 세 파일 세대의 commit marker로 사용한다.
    for name in (XOUT_MD, SETTINGS_JSON, MANIFEST_JSON):
        path = target_dir / name
        atomic_write_text(path, documents[name])
        written_by_name[name] = path
    logger.info("popper 산출물 착지: %s", target_dir)
    return WriteResult(
        base_dir=target_dir,
        manifest=manifest,
        written=tuple(written_by_name[name] for name in OUTPUT_FILES),
        mismatches=tuple(mismatches),
    )


def write_outputs(
    events: Iterable[Any],
    *,
    catalog: AxisCatalog | None = None,
    base_dir: Path | None = None,
    session_id: str | None = None,
    now: str | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    prereg_ref: str = DEFAULT_PREREG_REF,
    acknowledge_mismatch: bool = False,
    lang: str = DEFAULT_RULE_LANG,
) -> WriteResult:
    """누적 스트림 판독부터 세 파일 착지까지 프로세스 간 단일 트랜잭션으로 실행한다."""
    target_dir = Path(base_dir) if base_dir is not None else default_base_dir()
    with base_lock(target_dir):
        return _write_outputs_unlocked(
            events,
            catalog=catalog,
            base_dir=target_dir,
            session_id=session_id,
            now=now,
            conflicts=conflicts,
            prereg_ref=prereg_ref,
            acknowledge_mismatch=acknowledge_mismatch,
            lang=lang,
        )
