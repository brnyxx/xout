"""오긋기 복구 채널 - 두 번째 동사 없이 명시 이벤트로만 동작하는 순수 fold.

채널은 정확히 셋뿐이며 어느 것도 새 동사를 만들지 않는다.

1. undo_tombstone
   취소 버튼이 아니다. 이미 그은 (pair_id, fragment_id, strike_target) 조합을
   그대로 다시 긋는 행위를 fold가 tombstone으로 해석한다. strike_target enum은
   {left, right, both, pair} 4값 그대로이며 새 타깃도 새 컨트롤도 생기지 않는다.
   tombstone은 원 반증의 provenance를 즉시 무효화한다(warranted 원장에서 제거).
   다만 잔존 조합 수는 '세션 내 단조 감소' 계약을 지켜야 하므로 이 시점에
   오르지 않는다. 무효화된 반증이 가리키던 (축,값)은 pending_revive로 적재되어
   막 경계에서만 소비된다. 같은 조합을 세 번째로 그으면 다시 활성 반증이 된다.

2. revive
   원 strike를 참조한다. MVP에 실재하는 유일한 막 경계인
   '세션 종료 -> 4막 재심 세션 시작'에서만 유효하고, 곱 카운터가 상승하는
   지점은 여기뿐이다. 그 밖의 위치에서 관측된 revive는 상태를 바꾸지 않고
   거절 기록만 남긴다.

3. contradiction
   축의 마지막 생존값에 대한 긋기는 소거로 반영되지 않는다(적용 arity 0).
   대신 contradiction을 파생시켜 해당 축을 contested로 표시하고 동일 축
   재시험을 강제한다. 축의 생존값 집합은 결코 비지 않는다.

부활값과 contested 값은 미시험으로 강등되어 재심 큐 untested-prior 클래스의
선두에 놓인다. 전역 클래스 우선순위(불안정 > untested-prior > 충돌)는 불변이다.

모든 파생 상태는 append-only 스트림에 대한 순수 fold 결과이며 저장되지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from math import prod
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from xout.events import Event, EventType, StrikeEvent

logger = logging.getLogger(__name__)


class RecoveryViolation(RuntimeError):
    """복구 채널 계약 위반."""


class RecoveryChannel(str, Enum):
    """복구가 통과할 수 있는 명시 채널 - 확률 모델은 존재하지 않는다."""

    UNDO_TOMBSTONE = "undo_tombstone"
    REVIVE = "revive"
    CONTRADICTION = "contradiction"


RECHECK_CLASS_PRIORITY: tuple[str, str, str] = ("unstable", "untested-prior", "conflict")

UNTESTED = "untested"
PARTIAL = "partial"
DISCRIMINATED = "discriminated"

DEFAULT_AXIS_CATALOG: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "응답언어": ("korean", "english", "mixed"),
        "장황함": ("terse", "balanced", "verbose"),
        "자율성": ("ask-first", "propose-then-act", "autonomous"),
        "커밋스타일": ("conventional", "narrative", "minimal"),
        "테스트규율": ("test-first", "test-after", "on-demand"),
        "주석문서화": ("minimal", "docstring", "extensive"),
        "에러시행동": ("stop-and-report", "retry-then-report", "self-heal"),
        "범위준수": ("strict", "adjacent-ok", "proactive"),
    }
)


def strike_key(event: StrikeEvent) -> tuple[str, tuple[str, ...], str]:
    """undo 판정 키 - (pair_id, fragment_id, strike_target).

    left/right는 fragment_id가 정확히 1개, both는 2개(정렬 고정), pair는 0개다.
    """
    fragments = tuple(sorted(r.fragment_id for r in event.refutations))
    return (event.pair_id, fragments, event.strike_target.value)


@dataclass(frozen=True, slots=True)
class TombstoneRecord:
    """재긋기로 파생된 undo - 원 strike의 반증을 무효화한다."""

    strike_event_id: str
    key: tuple[str, tuple[str, ...], str]
    voided_pairs: tuple[tuple[str, str], ...]
    channel: RecoveryChannel = RecoveryChannel.UNDO_TOMBSTONE


@dataclass(frozen=True, slots=True)
class ContradictionRecord:
    """축의 마지막 생존값 긋기에서 파생 - 적용 arity 0, 축을 contested로 만든다."""

    session_id: str
    strike_event_id: str
    axis: str
    value: str
    applied_arity: int = 0
    channel: RecoveryChannel = RecoveryChannel.CONTRADICTION

    def to_event(self) -> Event:
        """스트림에 적재할 경우의 봉투 - fold는 저장하지 않는다."""
        return Event(
            type=EventType.CONTRADICTION,
            session_id=self.session_id,
            payload={
                "strike_event_id": self.strike_event_id,
                "axis": self.axis,
                "value": self.value,
                "applied_arity": self.applied_arity,
            },
        )


@dataclass(frozen=True, slots=True)
class PendingRevive:
    """tombstone이 남긴 부활 대기 - 막 경계에서만 소비 가능하다."""

    axis: str
    value: str
    strike_event_id: str


@dataclass(frozen=True, slots=True)
class RejectedRevive:
    """막 경계 밖 revive - 상태를 바꾸지 않고 거절만 기록된다."""

    event_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Demotion:
    """미시험 강등 - 부활값과 contested 값이 여기로 들어온다."""

    axis: str
    value: str
    cause: RecoveryChannel


@dataclass(frozen=True, slots=True)
class AxisState:
    """축별 파생 상태 - 저장되지 않는 fold 결과."""

    axis: str
    values: tuple[str, ...]
    surviving: tuple[str, ...]
    contested: bool
    revived: tuple[str, ...]
    refutation_count: int

    @property
    def discrimination(self) -> str:
        if self.contested or self.revived:
            return UNTESTED
        if len(self.surviving) == 1:
            return DISCRIMINATED
        if len(self.surviving) == 2:
            return PARTIAL
        return UNTESTED


@dataclass(frozen=True, slots=True)
class RecheckEntry:
    """재심 큐 항목."""

    axis: str
    klass: str
    reason: str
    value: str | None = None

    def __post_init__(self) -> None:
        if self.klass not in RECHECK_CLASS_PRIORITY:
            raise RecoveryViolation(f"알 수 없는 재심 클래스: {self.klass!r}")

    @property
    def priority(self) -> int:
        return RECHECK_CLASS_PRIORITY.index(self.klass)


@dataclass(frozen=True, slots=True)
class RecoveryState:
    """복구 채널 fold의 전체 파생 상태."""

    axis_states: Mapping[str, AxisState]
    remaining_hypotheses: int
    eliminated_pairs: int
    remaining_trace: tuple[int, ...]
    tombstones: tuple[TombstoneRecord, ...]
    contradictions: tuple[ContradictionRecord, ...]
    pending_revive: tuple[PendingRevive, ...]
    rejected_revives: tuple[RejectedRevive, ...]
    demotions: tuple[Demotion, ...]
    warranted_refutations: tuple[tuple[str, str, str], ...]
    revive_indices: frozenset[int]

    @property
    def contested_axes(self) -> tuple[str, ...]:
        return tuple(a for a, s in self.axis_states.items() if s.contested)

    @property
    def forced_retest_axes(self) -> tuple[str, ...]:
        """contradiction이 재시험을 강제한 축 - 발생 순서 보존."""
        seen: list[str] = []
        for record in self.contradictions:
            if record.axis not in seen and self.axis_states[record.axis].contested:
                seen.append(record.axis)
        return tuple(seen)


@dataclass(slots=True)
class _AxisWork:
    axis: str
    values: tuple[str, ...]
    surviving: set[str]
    contested_by: set[str]
    revived: list[str]
    refutation_count: int = 0


def _session_kind(payload: Mapping[str, Any]) -> str:
    return str(payload.get("session_kind", payload.get("kind", "main")))


def fold_recovery(
    events: Iterable[StrikeEvent | Event],
    catalog: Mapping[str, Sequence[str]] = DEFAULT_AXIS_CATALOG,
) -> RecoveryState:
    """이벤트 prefix에 대한 순수 fold - 같은 입력은 항상 같은 상태를 낳는다."""
    work: dict[str, _AxisWork] = {
        axis: _AxisWork(axis=axis, values=tuple(values), surviving=set(values), contested_by=set(), revived=[])
        for axis, values in catalog.items()
    }
    active: dict[tuple[str, tuple[str, ...], str], str] = {}
    applied_by: dict[str, tuple[tuple[str, str], ...]] = {}
    session_of: dict[str, str] = {}
    voided: set[str] = set()
    pending: dict[tuple[str, str, str], PendingRevive] = {}

    tombstones: list[TombstoneRecord] = []
    contradictions: list[ContradictionRecord] = []
    rejected: list[RejectedRevive] = []
    demotions: list[Demotion] = []
    trace: list[int] = []
    revive_indices: set[int] = set()

    sessions_ended = 0
    current_kind = "main"
    strikes_in_session = 0

    def remaining() -> int:
        return prod(len(work[axis].surviving) for axis in work) if work else 0

    for index, event in enumerate(events):
        if isinstance(event, StrikeEvent):
            strikes_in_session += 1
            key = strike_key(event)
            previous_id = active.get(key)
            if previous_id is not None:
                # 같은 조합 재긋기 - fold가 undo_tombstone으로 해석한다.
                del active[key]
                voided.add(previous_id)
                pairs = applied_by.get(previous_id, ())
                for axis, value in pairs:
                    entry = PendingRevive(axis=axis, value=value, strike_event_id=previous_id)
                    pending[(previous_id, axis, value)] = entry
                    work[axis].refutation_count -= 1
                for record in contradictions:
                    if record.strike_event_id == previous_id:
                        work[record.axis].contested_by.discard(previous_id)
                tombstones.append(
                    TombstoneRecord(strike_event_id=previous_id, key=key, voided_pairs=pairs)
                )
                trace.append(remaining())
                continue

            active[key] = event.event_id
            session_of[event.event_id] = event.session_id
            # 같은 조합의 재재긋기 - tombstone이 남긴 pending을 소비해 반증을 재활성화한다.
            reactivated: list[tuple[str, str]] = []
            for tomb in reversed(tombstones):
                if tomb.key != key:
                    continue
                for axis, value in tomb.voided_pairs:
                    if pending.pop((tomb.strike_event_id, axis, value), None) is None:
                        continue
                    work[axis].refutation_count += 1
                    reactivated.append((axis, value))
                if reactivated:
                    break
            applied: list[tuple[str, str]] = list(reactivated)
            for refutation in event.refutations:
                axis = refutation.axis
                if axis not in work:
                    logger.warning("카탈로그에 없는 축 - 적용 arity 0: %s", axis)
                    continue
                state = work[axis]
                if refutation.value not in state.surviving:
                    continue  # 이미 제거된 쌍 - 멱등, arity 0
                if len(state.surviving) == 1:
                    # 축의 마지막 생존값 - 소거하지 않고 contradiction을 파생시킨다.
                    state.contested_by.add(event.event_id)
                    contradictions.append(
                        ContradictionRecord(
                            session_id=event.session_id,
                            strike_event_id=event.event_id,
                            axis=axis,
                            value=refutation.value,
                        )
                    )
                    continue
                state.surviving.discard(refutation.value)
                state.refutation_count += 1
                applied.append((axis, refutation.value))
            applied_by[event.event_id] = tuple(applied)
            trace.append(remaining())
            continue

        etype = event.type
        if etype is EventType.UNDO_TOMBSTONE:
            origin = str(
                event.payload.get("strike_event_id")
                or event.payload.get("target_event_id")
                or ""
            )
            key = next((key for key, active_id in active.items() if active_id == origin), None)
            if key is None:
                trace.append(remaining())
                continue
            del active[key]
            voided.add(origin)
            pairs = applied_by.get(origin, ())
            for axis, value in pairs:
                pending[(origin, axis, value)] = PendingRevive(
                    axis=axis,
                    value=value,
                    strike_event_id=origin,
                )
                work[axis].refutation_count -= 1
            for record in contradictions:
                if record.strike_event_id == origin:
                    work[record.axis].contested_by.discard(origin)
            tombstones.append(
                TombstoneRecord(
                    strike_event_id=origin,
                    key=key,
                    voided_pairs=pairs,
                )
            )
            trace.append(remaining())
            continue

        if etype is EventType.SESSION_START:
            if strikes_in_session or current_kind != _session_kind(event.payload):
                sessions_ended += 1
            current_kind = _session_kind(event.payload)
            strikes_in_session = 0
            trace.append(remaining())
            continue

        if etype in (EventType.SESSION_VALIDATED, EventType.SESSION_VOIDED):
            sessions_ended += 1
            strikes_in_session = 0
            trace.append(remaining())
            continue

        if etype is EventType.REVIVE:
            at_boundary = (
                current_kind == "recheck" and sessions_ended >= 1 and strikes_in_session == 0
            )
            if not at_boundary:
                rejected.append(
                    RejectedRevive(
                        event_id=event.event_id,
                        reason="막 경계('세션 종료 -> 4막 재심 세션 시작') 밖의 revive",
                    )
                )
                trace.append(remaining())
                continue

            origin = str(event.payload.get("strike_event_id", ""))
            if origin not in applied_by:
                rejected.append(
                    RejectedRevive(event_id=event.event_id, reason="원 strike 참조 없음")
                )
                trace.append(remaining())
                continue

            targets = tuple(applied_by[origin])
            axis_hint = event.payload.get("axis")
            value_hint = event.payload.get("value")
            if axis_hint is not None and value_hint is not None:
                targets = tuple(t for t in targets if t == (axis_hint, value_hint))
            restored = 0
            for axis, value in targets:
                state = work[axis]
                if value in state.surviving:
                    continue
                state.surviving.add(value)
                state.revived.append(value)
                pending.pop((origin, axis, value), None)
                demotions.append(
                    Demotion(axis=axis, value=value, cause=RecoveryChannel.REVIVE)
                )
                restored += 1
            if restored:
                revive_indices.add(index)
            else:
                rejected.append(
                    RejectedRevive(event_id=event.event_id, reason="복원할 소거 쌍 없음")
                )
            trace.append(remaining())
            continue

        trace.append(remaining())

    for axis, state in work.items():
        if state.contested_by:
            for value in state.values:
                if value in state.surviving:
                    demotions.append(
                        Demotion(axis=axis, value=value, cause=RecoveryChannel.CONTRADICTION)
                    )

    axis_states = {
        axis: AxisState(
            axis=axis,
            values=state.values,
            surviving=tuple(v for v in state.values if v in state.surviving),
            contested=bool(state.contested_by),
            revived=tuple(state.revived),
            refutation_count=max(state.refutation_count, 0),
        )
        for axis, state in work.items()
    }
    warranted = tuple(
        (event_id, axis, value)
        for event_id, pairs in applied_by.items()
        if event_id not in voided
        for axis, value in pairs
    )
    eliminated = sum(len(s.values) - len(s.surviving) for s in axis_states.values())
    return RecoveryState(
        axis_states=MappingProxyType(axis_states),
        remaining_hypotheses=prod(len(s.surviving) for s in axis_states.values()),
        eliminated_pairs=eliminated,
        remaining_trace=tuple(trace),
        tombstones=tuple(tombstones),
        contradictions=tuple(contradictions),
        pending_revive=tuple(pending.values()),
        rejected_revives=tuple(rejected),
        demotions=tuple(demotions),
        warranted_refutations=warranted,
        revive_indices=frozenset(revive_indices),
    )


def build_recheck_queue(
    state: RecoveryState,
    unstable: Iterable[RecheckEntry] = (),
    conflicts: Iterable[RecheckEntry] = (),
) -> tuple[RecheckEntry, ...]:
    """재심 큐 - 클래스 우선순위 불변, 강등값이 untested-prior 선두에 놓인다."""
    head: list[RecheckEntry] = []
    seen: set[tuple[str, str | None]] = set()
    for demotion in state.demotions:
        marker = (demotion.axis, demotion.value)
        if marker in seen:
            continue
        seen.add(marker)
        head.append(
            RecheckEntry(
                axis=demotion.axis,
                value=demotion.value,
                klass="untested-prior",
                reason=f"demoted:{demotion.cause.value}",
            )
        )

    tail: list[RecheckEntry] = []
    for axis, axis_state in state.axis_states.items():
        if axis_state.contested or axis_state.revived:
            continue
        if axis_state.refutation_count == 0:
            tail.append(
                RecheckEntry(axis=axis, value=None, klass="untested-prior", reason="mined-prior")
            )

    untested_class = tuple(head) + tuple(tail)
    ordered: list[RecheckEntry] = []
    for klass in RECHECK_CLASS_PRIORITY:
        if klass == "unstable":
            ordered.extend(e for e in unstable if e.klass == "unstable")
        elif klass == "untested-prior":
            ordered.extend(untested_class)
        else:
            ordered.extend(e for e in conflicts if e.klass == "conflict")
    return tuple(ordered)


def revive(session_id: str, strike_event_id: str, **payload: Any) -> Event:
    """revive 이벤트 생성자 - 반드시 원 strike를 참조한다."""
    if not strike_event_id:
        raise RecoveryViolation("revive는 원 strike를 참조해야 한다")
    return Event(
        type=EventType.REVIVE,
        session_id=session_id,
        payload={"strike_event_id": strike_event_id, **payload},
    )


def undo_verbs() -> tuple[str, ...]:
    """사용자가 쓸 수 있는 동사 전체 - 긋기 하나뿐임을 코드로 못 박는다."""
    return ("strike",)
