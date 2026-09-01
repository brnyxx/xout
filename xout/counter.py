"""가설 공간 카운터 - 이벤트 스트림의 순수 fold.

서로 다른 두 양을 분리해서 낸다.

  (a) remaining_combinations - 축별 생존값 개수의 곱. 초기값 3^8 = 6,561.
  (b) eliminated_pairs       - 제거된 (축,값) 쌍의 누적 수. 0..24.

이벤트당 arity(-0/-1/-2)는 (b)에 적용되는 값이지 (a)의 감소폭이 아니다.
첫 arity 1 반증에서 (a)는 6,561 -> 4,374로 떨어지고 (b)는 0 -> 1로 오른다.

카운터는 어디에도 저장되지 않는다. 같은 스트림을 다시 replay하면 두 값 모두
항상 같은 정수가 나온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from typing import Any, Iterable, Mapping, Sequence

from xout.events import Event, EventType, Refutation, StrikeEvent

AxisCatalog = Mapping[str, Sequence[str]]


class CounterViolation(ValueError):
    """카탈로그 밖의 축/값이거나 해석 불가능한 페이로드."""


#: 값 순서 = 채굴 prior (index 0이 현장 최빈값). 순서의 근거는 docs/mined-prior.md -
#: 고스타 프로젝트 100+개의 규칙 파일/시스템프롬프트 실측 조사다.
DEFAULT_CATALOG: dict[str, tuple[str, str, str]] = {
    # 자율성
    "autonomy": ("ask_first", "propose_then_act", "act_then_report"),
    # 커밋정책
    "commit_style": ("no_auto_commit", "conventional", "narrative"),
    # 테스트규율
    "test_discipline": ("test_after", "test_first", "on_request"),
    # 주석문서화
    "comment_doc": ("minimal", "docstring_only", "thorough"),
    # 에러시행동
    "error_behavior": ("stop_and_report", "retry_then_report", "self_heal"),
    # 범위준수
    "scope_adherence": ("strict", "adjacent_fix_ok", "proactive"),
    # 완료전검증
    "verification": ("always_run", "on_risky", "trust_static"),
    # 의존성정책
    "dependency_policy": ("prefer_existing", "ask_first", "free"),
}

#: v1 카탈로그에만 있던 축 - 구버전 이벤트 재생 시 무시한다(관용 fold).
LEGACY_AXES: frozenset[str] = frozenset({"response_language", "verbosity"})

INITIAL_COMBINATIONS: int = prod(len(v) for v in DEFAULT_CATALOG.values())
TOTAL_PAIRS: int = sum(len(v) for v in DEFAULT_CATALOG.values())

#: fold가 소비하는 이벤트 타입. 나머지(probe 등)는 카운터에 불활성이다.
CONSUMED_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.STRIKE,
        EventType.UNDO_TOMBSTONE,
        EventType.REVIVE,
        EventType.CONTRADICTION,
    }
)

_TARGET_KEYS: tuple[str, ...] = (
    "target_event_id",
    "strike_event_id",
    "undone_event_id",
)

_COMPLETE = "complete"
_PARTIAL = "partial"
_UNTESTED = "untested"


@dataclass(frozen=True, slots=True)
class AxisState:
    """축별 파생 상태 - 저장되지 않고 fold로만 만들어진다."""

    axis: str
    surviving: tuple[str, ...]
    eliminated: tuple[str, ...]
    contested: bool = False
    revived: bool = False

    @property
    def discrimination(self) -> str:
        """생존값 개수로만 정해지는 판별 상태."""
        count = len(self.surviving)
        if count == 1:
            return _COMPLETE
        if count == 2:
            return _PARTIAL
        return _UNTESTED

    @property
    def demoted(self) -> bool:
        """부활값/모순축은 미시험으로 강등된다."""
        return self.revived or self.contested

    @property
    def effective_discrimination(self) -> str:
        if self.demoted:
            return _UNTESTED
        return self.discrimination


@dataclass(frozen=True, slots=True)
class EventEffect:
    """이벤트 한 건이 두 카운터에 남긴 자국."""

    event_id: str
    event_type: str
    arity: int
    restored: int
    eliminated_delta: int
    remaining_combinations: int
    eliminated_pairs: int
    contradiction_axes: tuple[str, ...] = ()

    @property
    def counter_delta(self) -> int:
        """-0/-1/-2 표기의 arity. (b)에 적용되는 값이다."""
        return -self.arity


@dataclass(frozen=True, slots=True)
class CounterState:
    """fold 결과 - 두 카운터와 축별 상태, 이벤트별 자국."""

    remaining_combinations: int
    eliminated_pairs: int
    axes: tuple[AxisState, ...] = ()
    effects: tuple[EventEffect, ...] = ()

    def axis(self, name: str) -> AxisState:
        for state in self.axes:
            if state.axis == name:
                return state
        raise CounterViolation(f"카탈로그에 없는 축: {name!r}")

    @property
    def contradiction_axes(self) -> tuple[str, ...]:
        return tuple(a.axis for a in self.axes if a.contested)

    @property
    def arities(self) -> tuple[int, ...]:
        return tuple(e.arity for e in self.effects)


class HypothesisSpace:
    """가설 공간의 가변 작업본. fold 내부에서만 쓰인다."""

    __slots__ = ("_catalog", "_surviving", "_contested", "_revived", "_by_event")

    def __init__(self, catalog: AxisCatalog) -> None:
        self._catalog: dict[str, tuple[str, ...]] = {
            axis: tuple(values) for axis, values in catalog.items()
        }
        if not self._catalog:
            raise CounterViolation("축 카탈로그가 비어 있다")
        self._surviving: dict[str, set[str]] = {
            axis: set(values) for axis, values in self._catalog.items()
        }
        self._contested: dict[str, bool] = {axis: False for axis in self._catalog}
        self._revived: dict[str, bool] = {axis: False for axis in self._catalog}
        self._by_event: dict[str, tuple[tuple[str, str], ...]] = {}

    @property
    def total_pairs(self) -> int:
        return sum(len(values) for values in self._catalog.values())

    @property
    def remaining_combinations(self) -> int:
        return prod(len(values) for values in self._surviving.values())

    @property
    def eliminated_pairs(self) -> int:
        return self.total_pairs - sum(len(v) for v in self._surviving.values())

    def _check(self, axis: str, value: str) -> None:
        known = self._catalog.get(axis)
        if known is None:
            raise CounterViolation(f"카탈로그에 없는 축: {axis!r}")
        if value not in known:
            raise CounterViolation(f"축 {axis!r}에 없는 값: {value!r}")

    def eliminate(self, axis: str, value: str) -> tuple[int, bool]:
        """(axis, value) 하나를 제거한다. (arity, 모순여부)를 돌려준다."""
        self._check(axis, value)
        survivors = self._surviving[axis]
        if value not in survivors:
            # 이미 제거된 (축,값)에 대한 중복 반증 - arity 0 멱등 처리
            return 0, False
        if len(survivors) == 1:
            # 축의 마지막 생존값 - arity 0으로 격리하고 모순만 파생한다.
            # 곱 카운터가 0으로 붕괴하지 않는 지점이 여기다.
            self._contested[axis] = True
            return 0, True
        survivors.discard(value)
        return 1, False

    def restore(self, axis: str, value: str) -> int:
        """(axis, value) 하나를 되살린다. 되살아난 개수를 돌려준다."""
        self._check(axis, value)
        survivors = self._surviving[axis]
        if value in survivors:
            return 0
        survivors.add(value)
        return 1

    def restore_axis(self, axis: str) -> int:
        known = self._catalog.get(axis)
        if known is None:
            raise CounterViolation(f"카탈로그에 없는 축: {axis!r}")
        return sum(self.restore(axis, value) for value in known)

    def mark_contested(self, axis: str, flag: bool = True) -> None:
        self._check_axis(axis)
        self._contested[axis] = flag

    def mark_revived(self, axis: str) -> None:
        self._check_axis(axis)
        self._revived[axis] = True

    def _check_axis(self, axis: str) -> None:
        if axis not in self._catalog:
            raise CounterViolation(f"카탈로그에 없는 축: {axis!r}")

    def record(self, event_id: str, pairs: Iterable[tuple[str, str]]) -> None:
        removed = tuple(pairs)
        if removed:
            self._by_event[event_id] = removed

    def take(self, event_id: str) -> tuple[tuple[str, str], ...]:
        return self._by_event.pop(event_id, ())

    def snapshot(self) -> tuple[AxisState, ...]:
        states: list[AxisState] = []
        for axis in sorted(self._catalog):
            survivors = self._surviving[axis]
            states.append(
                AxisState(
                    axis=axis,
                    surviving=tuple(v for v in self._catalog[axis] if v in survivors),
                    eliminated=tuple(
                        v for v in self._catalog[axis] if v not in survivors
                    ),
                    contested=self._contested[axis],
                    revived=self._revived[axis],
                )
            )
        return tuple(states)


def _pairs_from_payload(
    payload: Mapping[str, Any], space: HypothesisSpace
) -> list[tuple[str, str]]:
    """undo/revive/contradiction 페이로드에서 (축,값) 목록을 뽑는다."""
    pairs: list[tuple[str, str]] = []

    for key in _TARGET_KEYS:
        target = payload.get(key)
        if target:
            pairs.extend(space.take(str(target)))

    raw_refutations = payload.get("refutations") or ()
    for item in raw_refutations:
        if isinstance(item, Refutation):
            pairs.append((item.axis, item.value))
            continue
        if isinstance(item, Mapping) and "axis" in item and "value" in item:
            pairs.append((str(item["axis"]), str(item["value"])))
            continue
        raise CounterViolation(f"해석할 수 없는 반증 항목: {item!r}")

    raw_pairs = payload.get("pairs") or ()
    for item in raw_pairs:
        if isinstance(item, Sequence) and not isinstance(item, str) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
            continue
        raise CounterViolation(f"해석할 수 없는 (축,값) 항목: {item!r}")

    axis = payload.get("axis")
    if axis is not None:
        value = payload.get("value")
        if value is not None:
            pairs.append((str(axis), str(value)))
        for item in payload.get("values") or ():
            pairs.append((str(axis), str(item)))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        deduped.append(pair)
    return deduped


def _apply_strike(event: StrikeEvent, space: HypothesisSpace) -> EventEffect:
    arity = 0
    removed: list[tuple[str, str]] = []
    contradictions: list[str] = []

    for refutation in event.refutations:
        # skeleton span은 대비축을 가리지 않으므로 arity 0이다.
        if getattr(refutation, "span_kind", None) == "skeleton":
            continue
        if refutation.axis in LEGACY_AXES:
            # v1 축의 과거 긋기 - 현재 공간에 좌표가 없으므로 조용히 지나간다.
            continue
        delta, contradicted = space.eliminate(refutation.axis, refutation.value)
        if delta:
            removed.append((refutation.axis, refutation.value))
            arity += delta
        if contradicted and refutation.axis not in contradictions:
            contradictions.append(refutation.axis)

    space.record(event.event_id, removed)
    return EventEffect(
        event_id=event.event_id,
        event_type=EventType.STRIKE.value,
        arity=arity,
        restored=0,
        eliminated_delta=arity,
        remaining_combinations=space.remaining_combinations,
        eliminated_pairs=space.eliminated_pairs,
        contradiction_axes=tuple(contradictions),
    )


def _apply_restore(event: Event, space: HypothesisSpace) -> EventEffect:
    payload = event.payload
    pairs = _pairs_from_payload(payload, space)

    if event.type is EventType.CONTRADICTION:
        axis = payload.get("axis")
        if axis is not None:
            # 모순 이벤트는 동일 축을 통째로 재시험 대상으로 되돌린다.
            restored = space.restore_axis(str(axis))
            space.mark_contested(str(axis))
            return _restore_effect(event, space, restored, (str(axis),))

    restored = sum(space.restore(axis, value) for axis, value in pairs)

    if event.type is EventType.REVIVE:
        for axis in dict.fromkeys(a for a, _ in pairs):
            space.mark_revived(axis)

    return _restore_effect(event, space, restored, ())


def _restore_effect(
    event: Event,
    space: HypothesisSpace,
    restored: int,
    contradiction_axes: tuple[str, ...],
) -> EventEffect:
    return EventEffect(
        event_id=event.event_id,
        event_type=event.type.value,
        arity=0,
        restored=restored,
        eliminated_delta=-restored,
        remaining_combinations=space.remaining_combinations,
        eliminated_pairs=space.eliminated_pairs,
        contradiction_axes=contradiction_axes,
    )


def fold(
    events: Iterable[StrikeEvent | Event],
    catalog: AxisCatalog | None = None,
) -> CounterState:
    """이벤트 스트림을 접어 두 카운터를 낸다. 순수 함수다."""
    space = HypothesisSpace(catalog or DEFAULT_CATALOG)
    effects: list[EventEffect] = []

    for event in events:
        etype = getattr(event, "type", None)
        if etype not in CONSUMED_TYPES:
            continue
        if isinstance(event, StrikeEvent):
            effects.append(_apply_strike(event, space))
            continue
        if isinstance(event, Event):
            effects.append(_apply_restore(event, space))
            continue
        raise CounterViolation(f"알 수 없는 이벤트 객체: {type(event).__name__}")

    remaining = space.remaining_combinations
    eliminated = space.eliminated_pairs
    total = space.total_pairs
    if remaining <= 0:
        raise CounterViolation("곱 카운터가 0으로 붕괴했다 - 마지막 생존값 격리 실패")
    if not 0 <= eliminated <= total:
        raise CounterViolation(f"eliminated_pairs가 0..{total} 범위를 벗어났다")

    return CounterState(
        remaining_combinations=remaining,
        eliminated_pairs=eliminated,
        axes=space.snapshot(),
        effects=tuple(effects),
    )


def remaining_combinations(
    events: Iterable[StrikeEvent | Event],
    catalog: AxisCatalog | None = None,
) -> int:
    return fold(events, catalog).remaining_combinations


def eliminated_pairs(
    events: Iterable[StrikeEvent | Event],
    catalog: AxisCatalog | None = None,
) -> int:
    return fold(events, catalog).eliminated_pairs
