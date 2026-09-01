"""세션 프로파일 판정 - 봉인 수치를 강제하는 순수 fold.

세션은 session_start.profile in {product, validation}로 시작한다.

- product    : 판별 슬롯만으로 채워진다. 프로브 이벤트가 하나라도 나타나면
               판정 fold가 해당 스트림 전체를 무효 처리한다.
- validation : 판별 슬롯 + 고정 위치의 미러 프로브로 구성된다. 프로브 쌍은
               이벤트 prefix의 순수 함수인 결정론적 규칙으로 선정되고
               (같은 prefix면 항상 같은 선정), 컴파일/카운터 fold에는
               불활성이며, terminal(재추첨 불가)이다. 프로브는 좌우 미러로
               제시되며 원본 pair id와 mirrored 플래그를 기록한다.

완전 판별 통과 축이 봉인 하한에 못 미치면 session_voided(reason=axis_shortfall)
가 방출된다. 자동 연장은 없다.

판정 영향 수치의 소유자는 봉인 사전등록 문서(docs/prereg/prereg_sealed.json)다.
이 모듈은 수치를 소유하지 않는다 - 그 문서의 session_slot_layout 절과
unit="axes"인 동결 항목에서 읽거나, 호출자 주입(SessionSpec)으로 받는다.
파생 상태는 어디에도 저장되지 않으며 같은 스트림의 replay는 항상 같은
판정을 낳는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from xout.counter import AxisCatalog, fold as fold_counter
from xout.events import Event, EventType, StrikeEvent

logger = logging.getLogger(__name__)


class SessionViolation(ValueError):
    """세션 프로파일 계약 위반."""


PROFILE_PRODUCT = "product"
PROFILE_VALIDATION = "validation"
PROFILE_RECHECK = "recheck"

#: probe_result의 허용 결과값 - 출처: 봉인 문서 probe_selection_rule.properties.
PROBE_RESULT_DOMAIN: frozenset[str] = frozenset({"flip", "consistent"})

#: 컴파일/카운터 fold에 불활성인 프로브 이벤트 타입.
PROBE_EVENT_TYPES: frozenset[EventType] = frozenset(
    {EventType.PROBE_SHOWN, EventType.PROBE_RESULT}
)

VOID_REASON_AXIS_SHORTFALL = "axis_shortfall"

REASON_MISSING_SESSION_START = "missing_session_start"
REASON_DUPLICATE_SESSION_START = "duplicate_session_start"
REASON_UNKNOWN_PROFILE = "unknown_profile"
REASON_PROBE_IN_PRODUCT = "probe_in_product"
REASON_SLOT_OVERRUN = "slot_overrun"
REASON_PROBE_OUT_OF_POSITION = "probe_out_of_position"
REASON_PROBE_MISSING = "probe_missing"
REASON_PROBE_PAIR_MISMATCH = "probe_pair_mismatch"
REASON_PROBE_NOT_MIRRORED = "probe_not_mirrored"
REASON_PROBE_RESULT_ORPHAN = "probe_result_orphan"
REASON_PROBE_RESULT_INVALID = "probe_result_invalid"
REASON_PROBE_UNRESOLVED = "probe_unresolved"
REASON_PROBE_REDRAWN = "probe_redrawn"

_COMPLETE = "complete"
_PARTIAL = "partial"

_SOURCE_PREREG_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "prereg" / "prereg_sealed.json"
)
_PACKAGED_PREREG_PATH = (
    Path(__file__).resolve().parent / "_data" / "prereg" / "prereg_sealed.txt"
)
DEFAULT_PREREG_PATH: Path = (
    _SOURCE_PREREG_PATH
    if _SOURCE_PREREG_PATH.is_file()
    else _PACKAGED_PREREG_PATH
)

# 아래 기본값의 출처는 봉인 사전등록 문서 docs/prereg/prereg_sealed.json이다
# (session_slot_layout 절 + unit="axes"인 동결 항목). 수치의 소유권은 그 문서에
# 있으며, 여기 값은 문서를 읽을 수 없을 때 쓰는 주입 기본값일 뿐이다.
_FALLBACK_LAYOUT: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        PROFILE_PRODUCT: MappingProxyType(
            {"discriminative_slots": 15, "probe_slots": ()}
        ),
        PROFILE_VALIDATION: MappingProxyType(
            {"discriminative_slots": 13, "probe_slots": (9, 13)}
        ),
    }
)
_FALLBACK_REQUIRED_FULL_AXES: int = 5


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """프로파일별 봉인 세션 규격 - 수치의 소유자는 봉인 사전등록 문서다."""

    profile: str
    discriminative_slots: int
    probe_slots: tuple[int, ...]
    required_full_axes: int

    def __post_init__(self) -> None:
        if not self.profile:
            raise SessionViolation("profile은 비울 수 없다")
        if self.discriminative_slots <= 0:
            raise SessionViolation("판별 슬롯 수는 양수여야 한다")
        if len(set(self.probe_slots)) != len(self.probe_slots):
            raise SessionViolation("프로브 위치가 중복됐다")
        for position in self.probe_slots:
            if not 1 <= position <= self.total_slots:
                raise SessionViolation(f"프로브 위치가 세션 범위를 벗어났다: {position}")
        if self.required_full_axes < 0:
            raise SessionViolation("필요 완전 판별 축 수는 음수일 수 없다")

    @property
    def total_slots(self) -> int:
        """세션이 소비하는 슬롯 총량 = 판별 슬롯 + 프로브 슬롯."""
        return self.discriminative_slots + len(self.probe_slots)


def _required_axes_from(document: Mapping[str, Any]) -> int:
    """세션 유효성에 필요한 완전 판별 축 수를 봉인 문서에서 찾는다.

    해당 동결 항목의 키 이름 자체가 런타임 코드에 존재해선 안 되므로
    (code_scan_guard), unit == "axes"인 유일한 항목으로 식별해 읽는다.
    """
    frozen = document.get("frozen_parameters")
    if not isinstance(frozen, Mapping):
        return _FALLBACK_REQUIRED_FULL_AXES
    values = [
        entry.get("value")
        for entry in frozen.values()
        if isinstance(entry, Mapping) and entry.get("unit") == "axes"
    ]
    if len(values) == 1 and isinstance(values[0], int):
        return values[0]
    return _FALLBACK_REQUIRED_FULL_AXES


def load_session_specs(prereg_path: Path | None = None) -> Mapping[str, SessionSpec]:
    """봉인 사전등록 문서에서 프로파일별 세션 규격을 읽는다.

    문서를 읽을 수 없으면 동일 출처의 주입 기본값으로 대체한다.
    recheck처럼 고정 슬롯 수가 없는 프로파일은 판정 대상이 아니므로 건너뛴다.
    """
    path = prereg_path if prereg_path is not None else DEFAULT_PREREG_PATH
    layout: Mapping[str, Any] = _FALLBACK_LAYOUT
    required = _FALLBACK_REQUIRED_FULL_AXES
    try:
        document = json.loads(path.read_text(encoding="utf-8"))["document"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            "봉인 문서를 읽지 못해 주입 기본값을 쓴다: %s (%s)", path, e, exc_info=True
        )
    else:
        if isinstance(document, Mapping):
            raw_layout = document.get("session_slot_layout")
            if isinstance(raw_layout, Mapping):
                layout = raw_layout
            required = _required_axes_from(document)
        else:
            logger.warning("봉인 문서 형식이 예상과 다르다 - 주입 기본값을 쓴다: %s", path)

    specs: dict[str, SessionSpec] = {}
    for profile, entry in layout.items():
        if not isinstance(entry, Mapping):
            continue
        disc = entry.get("discriminative_slots")
        if not isinstance(disc, int):
            continue
        positions = tuple(int(p) for p in entry.get("probe_slots") or ())
        specs[profile] = SessionSpec(
            profile=str(profile),
            discriminative_slots=disc,
            probe_slots=positions,
            required_full_axes=required,
        )
    if not specs:
        raise SessionViolation("세션 규격을 하나도 구성하지 못했다")
    return MappingProxyType(specs)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """미러 프로브 한 건의 판정 자국 - 원본 pair id와 mirrored 플래그를 담는다."""

    position: int
    pair_id: str | None
    expected_pair_id: str | None
    mirrored: bool
    result: str | None
    shown_event_id: str
    result_event_id: str | None = None

    @property
    def matches_selection(self) -> bool:
        """prefix 결정론 선정이 요구한 원본 pair와 일치하는가."""
        return self.expected_pair_id is not None and self.pair_id == self.expected_pair_id

    @property
    def resolved(self) -> bool:
        return self.result in PROBE_RESULT_DOMAIN


@dataclass(frozen=True, slots=True)
class SessionVoidRecord:
    """세션 무효 판정 - fold가 파생만 하고 저장하지 않는 방출 레코드."""

    session_id: str
    reason: str
    fully_discriminated_axes: tuple[str, ...]
    required_full_axes: int

    def to_event(self) -> Event:
        """스트림에 적재할 경우의 session_voided 봉투 - fold는 저장하지 않는다."""
        return Event(
            type=EventType.SESSION_VOIDED,
            session_id=self.session_id,
            payload={
                "reason": self.reason,
                "fully_discriminated_axes": list(self.fully_discriminated_axes),
                "required_full_axes": self.required_full_axes,
            },
        )


@dataclass(frozen=True, slots=True)
class SessionJudgment:
    """세션 스트림 판정 fold의 결과 - 파생 상태이며 어디에도 저장되지 않는다."""

    session_id: str | None
    profile: str | None
    slots_used: int
    complete: bool
    reasons: tuple[str, ...]
    probes: tuple[ProbeOutcome, ...]
    fully_discriminated_axes: tuple[str, ...]
    voided: SessionVoidRecord | None

    @property
    def stream_valid(self) -> bool:
        """무효 사유가 하나도 없는가."""
        return not self.reasons


def probe_shown(
    session_id: str,
    position: int,
    pair_id: str,
    axis: str | None = None,
    mirrored: bool = True,
) -> Event:
    """미러 프로브 제시 이벤트 - 원본 pair id와 mirrored 플래그를 기록한다."""
    payload: dict[str, Any] = {
        "slot": position,
        "pair_id": pair_id,
        "mirrored": mirrored,
    }
    if axis is not None:
        payload["axis"] = axis
    return Event(type=EventType.PROBE_SHOWN, session_id=session_id, payload=payload)


def probe_result(
    session_id: str,
    position: int,
    pair_id: str,
    result: str,
    axis: str | None = None,
) -> Event:
    """프로브 결과 이벤트 - 결과값은 flip/consistent만 허용한다."""
    if result not in PROBE_RESULT_DOMAIN:
        raise SessionViolation(f"허용되지 않은 probe 결과: {result!r}")
    payload: dict[str, Any] = {
        "slot": position,
        "pair_id": pair_id,
        "result": result,
    }
    if axis is not None:
        payload["axis"] = axis
    return Event(type=EventType.PROBE_RESULT, session_id=session_id, payload=payload)


def _slotted_strikes(
    events: Iterable[StrikeEvent | Event],
) -> tuple[tuple[int, StrikeEvent], ...]:
    """슬롯 번호가 붙은 스트라이크 목록 - 스트라이크와 probe_shown이 슬롯을 소비한다."""
    slotted: list[tuple[int, StrikeEvent]] = []
    slot = 0
    for event in events:
        if isinstance(event, StrikeEvent):
            slot += 1
            slotted.append((slot, event))
            continue
        if getattr(event, "type", None) is EventType.PROBE_SHOWN:
            slot += 1
    return tuple(slotted)


def select_probe_pairs(
    events: Iterable[StrikeEvent | Event],
    spec: SessionSpec,
) -> Mapping[int, str | None]:
    """프로브 위치별 미러 대상 pair id를 이벤트 prefix에서 결정론적으로 선정한다.

    봉인 규칙(probe_selection_rule): 첫 프로브 위치의 프로브는 전반부에서
    판별력이 인정된(pair-strike가 아닌) 최초 판별쌍의 미러, 다음 프로브는 그
    다음으로 판별력이 인정된 판별쌍의 미러다. 전반부 판별쌍이 모자라면
    부족분은 해당 위치 직전 판별쌍의 미러로 채운다. 선정은 prefix의 순수
    함수라서 같은 prefix면 항상 같은 선정이 나온다.
    """
    positions = tuple(sorted(spec.probe_slots))
    if not positions:
        return MappingProxyType({})
    disc = [
        (slot, event)
        for slot, event in _slotted_strikes(events)
        if event.has_discriminating_power
    ]
    half_end = positions[0] - 1
    first_half = [event for slot, event in disc if slot <= half_end]
    chosen: dict[int, str | None] = {}
    for ordinal, position in enumerate(positions):
        if ordinal < len(first_half):
            chosen[position] = first_half[ordinal].pair_id
            continue
        prior = [event for slot, event in disc if slot < position]
        chosen[position] = prior[-1].pair_id if prior else None
    return MappingProxyType(chosen)


@dataclass(slots=True)
class _ProbeDraft:
    """fold 내부에서만 쓰는 가변 작업본."""

    position: int
    pair_id: str | None
    mirrored: bool
    shown_event_id: str
    result: str | None = None
    result_event_id: str | None = None


def _note(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def fold_session(
    events: Iterable[StrikeEvent | Event],
    specs: Mapping[str, SessionSpec] | None = None,
    catalog: AxisCatalog | None = None,
) -> SessionJudgment:
    """세션 스트림 하나를 접어 프로파일 봉인 수치를 강제한다(순수 fold).

    - product 스트림에 프로브 이벤트가 나타나면 스트림을 무효 처리한다.
    - validation 스트림은 판별 슬롯 + 고정 위치 프로브 구성을 검증한다.
    - 완전 판별 통과 축이 하한 미만이면 session_voided(reason=axis_shortfall)
      레코드를 방출한다. 자동 연장은 없다.

    replay 결정적이다 - 같은 입력이면 항상 같은 판정이 나온다.
    """
    stream = tuple(events)
    active_specs = specs if specs is not None else load_session_specs()

    session_id: str | None = None
    profile: str | None = None
    started = False
    reasons: list[str] = []
    slot = 0
    drafts: list[_ProbeDraft] = []
    open_draft: _ProbeDraft | None = None
    probe_seen = False

    for event in stream:
        etype = getattr(event, "type", None)
        if not isinstance(etype, EventType):
            raise SessionViolation(f"알 수 없는 이벤트 객체: {type(event).__name__}")

        if etype is EventType.SESSION_START:
            if started:
                _note(reasons, REASON_DUPLICATE_SESSION_START)
                continue
            started = True
            session_id = event.session_id
            raw_profile = event.payload.get("profile")
            profile = raw_profile if isinstance(raw_profile, str) else None
            if profile not in active_specs:
                _note(reasons, REASON_UNKNOWN_PROFILE)
            continue

        if isinstance(event, StrikeEvent):
            slot += 1
            continue

        if etype is EventType.PROBE_SHOWN:
            probe_seen = True
            slot += 1
            if open_draft is not None:
                _note(reasons, REASON_PROBE_UNRESOLVED)
            declared = event.payload.get("slot")
            if isinstance(declared, int):
                if any(draft.position == declared for draft in drafts):
                    # terminal - 이미 소비된 프로브 위치의 재추첨 시도.
                    _note(reasons, REASON_PROBE_REDRAWN)
                if declared != slot:
                    _note(reasons, REASON_PROBE_OUT_OF_POSITION)
            raw_pair = event.payload.get("pair_id")
            draft = _ProbeDraft(
                position=slot,
                pair_id=str(raw_pair) if raw_pair is not None else None,
                mirrored=event.payload.get("mirrored") is True,
                shown_event_id=event.event_id,
            )
            drafts.append(draft)
            open_draft = draft
            continue

        if etype is EventType.PROBE_RESULT:
            probe_seen = True
            if open_draft is None:
                _note(reasons, REASON_PROBE_RESULT_ORPHAN)
                continue
            raw_result = event.payload.get("result")
            if raw_result not in PROBE_RESULT_DOMAIN:
                _note(reasons, REASON_PROBE_RESULT_INVALID)
            open_draft.result = raw_result if isinstance(raw_result, str) else None
            open_draft.result_event_id = event.event_id
            open_draft = None
            continue

        # 그 외 타입(undo/revive/contradiction 등)은 슬롯을 소비하지 않는다.

    if open_draft is not None:
        _note(reasons, REASON_PROBE_UNRESOLVED)
    if not started:
        _note(reasons, REASON_MISSING_SESSION_START)

    spec = active_specs.get(profile) if profile is not None else None

    expected: Mapping[int, str | None] = MappingProxyType({})
    if spec is not None:
        if not spec.probe_slots and probe_seen:
            # 프로브가 허용되지 않는 프로파일(product) 스트림 무효 처리.
            _note(reasons, REASON_PROBE_IN_PRODUCT)
        if slot > spec.total_slots:
            _note(reasons, REASON_SLOT_OVERRUN)
        if spec.probe_slots:
            expected = select_probe_pairs(stream, spec)
            allowed = set(spec.probe_slots)
            for draft in drafts:
                if draft.position not in allowed:
                    _note(reasons, REASON_PROBE_OUT_OF_POSITION)
                want = expected.get(draft.position)
                if want is not None and draft.pair_id != want:
                    _note(reasons, REASON_PROBE_PAIR_MISMATCH)
                if not draft.mirrored:
                    _note(reasons, REASON_PROBE_NOT_MIRRORED)
            if slot >= spec.total_slots:
                observed = {draft.position for draft in drafts}
                if allowed - observed:
                    _note(reasons, REASON_PROBE_MISSING)

    complete = spec is not None and started and slot == spec.total_slots

    counter_state = fold_counter(stream, catalog)
    # v2: 세션 유효성은 "판별 증거가 남은 축" 수로 판정한다. 다중 장면 설계에서
    # 한 축의 완전 판별(생존 1값)은 맥락 간 값 분화에 달려 있어 세션 품질의
    # 지표가 아니다 - 증거 0축(전부 pair-strike) 세션만 무효가 된다.
    fully_discriminated = tuple(
        state.axis
        for state in counter_state.axes
        if state.effective_discrimination in (_COMPLETE, _PARTIAL)
    )

    voided: SessionVoidRecord | None = None
    if (
        spec is not None
        and session_id is not None
        and complete
        and not reasons
        and len(fully_discriminated) < spec.required_full_axes
    ):
        voided = SessionVoidRecord(
            session_id=session_id,
            reason=VOID_REASON_AXIS_SHORTFALL,
            fully_discriminated_axes=fully_discriminated,
            required_full_axes=spec.required_full_axes,
        )
        logger.warning(
            "세션 무효 방출: session=%s reason=%s 통과축=%d/%d",
            session_id,
            VOID_REASON_AXIS_SHORTFALL,
            len(fully_discriminated),
            spec.required_full_axes,
        )

    outcomes = tuple(
        ProbeOutcome(
            position=draft.position,
            pair_id=draft.pair_id,
            expected_pair_id=expected.get(draft.position),
            mirrored=draft.mirrored,
            result=draft.result,
            shown_event_id=draft.shown_event_id,
            result_event_id=draft.result_event_id,
        )
        for draft in drafts
    )

    return SessionJudgment(
        session_id=session_id,
        profile=profile,
        slots_used=slot,
        complete=complete,
        reasons=tuple(reasons),
        probes=outcomes,
        fully_discriminated_axes=fully_discriminated,
        voided=voided,
    )
