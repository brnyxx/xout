"""Popper 이벤트 스키마 - strike-only, append-only.

세션의 단일 진실원은 긋기(strike) 이벤트 스트림이다.
사용자의 유일한 동사는 긋기이며, 승인/생존을 기록하는 이벤트 타입은
스키마에 존재하지 않는다. 생존은 이벤트가 아니라 '아직 반증되지 않음' 상태다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Iterator


class SchemaViolation(ValueError):
    """이벤트 스키마 계약 위반."""


class AppendOnlyViolation(RuntimeError):
    """append-only 스트림에 대한 변경/삭제 시도."""


class StrikeTarget(str, Enum):
    """긋기 타깃 - 정확히 4값. 긍정 입력도 조용한 스킵도 없다."""

    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"
    PAIR = "pair"


class EventType(str, Enum):
    """append-only 스트림에 허용된 이벤트 타입 전체 집합."""

    SESSION_START = "session_start"
    STRIKE = "strike"
    UNDO_TOMBSTONE = "undo_tombstone"
    REVIVE = "revive"
    CONTRADICTION = "contradiction"
    PROBE_SHOWN = "probe_shown"
    PROBE_RESULT = "probe_result"
    SESSION_VALIDATED = "session_validated"
    SESSION_VOIDED = "session_voided"
    PREREG_SEALED = "prereg_sealed"
    CATALOG_REVISION_CONSUMED = "catalog_revision_consumed"
    REFUTATION_CONDITION_MET = "refutation_condition_met"
    REFUTATION_ACKNOWLEDGED = "refutation_acknowledged"


POSITIVE_INTENT_LEXEMES: frozenset[str] = frozenset(
    {
        "accept",
        "accepted",
        "affirm",
        "agree",
        "allow",
        "approval",
        "approve",
        "approved",
        "choose",
        "confirm",
        "confirmation",
        "confirmed",
        "endorse",
        "keep",
        "like",
        "ok",
        "pick",
        "prefer",
        "select",
        "selected",
        "skip",
        "skipped",
        "survival",
        "survive",
        "survived",
        "yes",
    }
)


def positive_intent_violations() -> tuple[str, ...]:
    """승인/생존 의미를 담은 이벤트 타입이 스키마에 있으면 그 값들을 돌려준다."""
    violations: list[str] = []
    for member in EventType:
        tokens = member.value.split("_")
        if any(token in POSITIVE_INTENT_LEXEMES for token in tokens):
            violations.append(member.value)
    return tuple(violations)


_SCHEMA_VIOLATIONS = positive_intent_violations()
if _SCHEMA_VIOLATIONS:
    raise SchemaViolation(
        f"승인/생존을 기록하는 이벤트 타입은 허용되지 않는다: {_SCHEMA_VIOLATIONS}"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class Refutation:
    """단일 반증 - 긋기 이벤트가 남기는 provenance 한 건."""

    axis: str
    value: str
    fragment_id: str
    side: str | None = None

    def __post_init__(self) -> None:
        for name in ("axis", "value", "fragment_id"):
            if not getattr(self, name):
                raise SchemaViolation(f"Refutation.{name}는 비울 수 없다")
        if self.side not in (None, "left", "right"):
            raise SchemaViolation(f"Refutation.side 허용값 위반: {self.side!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "value": self.value,
            "fragment_id": self.fragment_id,
            "side": self.side,
        }


REFUTATION_ARITY: dict[StrikeTarget, int] = {
    StrikeTarget.LEFT: 1,
    StrikeTarget.RIGHT: 1,
    StrikeTarget.BOTH: 2,
    StrikeTarget.PAIR: 0,
}


@dataclass(frozen=True, slots=True)
class StrikeEvent:
    """긋기 이벤트 - 1:N 반증 provenance를 한 이벤트에 담는다."""

    session_id: str
    pair_id: str
    axis: str
    scene_id: str
    strike_target: StrikeTarget
    refutations: tuple[Refutation, ...] = ()
    event_id: str = field(default_factory=_new_id)
    at: str = field(default_factory=_now)
    seq: int | None = None
    type: EventType = EventType.STRIKE

    def __post_init__(self) -> None:
        if not isinstance(self.strike_target, StrikeTarget):
            raise SchemaViolation(
                f"strike_target은 StrikeTarget이어야 한다: {self.strike_target!r}"
            )
        if self.type is not EventType.STRIKE:
            raise SchemaViolation("StrikeEvent.type은 EventType.STRIKE로 고정된다")

        expected = REFUTATION_ARITY[self.strike_target]
        if len(self.refutations) != expected:
            raise SchemaViolation(
                f"{self.strike_target.value} 긋기의 반증 건수는 {expected}이어야 한다"
                f" (실제 {len(self.refutations)})"
            )

        sides = [r.side for r in self.refutations]
        if self.strike_target is StrikeTarget.BOTH and sorted(
            s or "" for s in sides
        ) != ["left", "right"]:
            raise SchemaViolation("both 긋기는 left/right 각 1건의 반증을 남겨야 한다")
        if self.strike_target in (StrikeTarget.LEFT, StrikeTarget.RIGHT) and sides != [
            self.strike_target.value
        ]:
            raise SchemaViolation(
                f"{self.strike_target.value} 긋기의 반증 side가 일치하지 않는다"
            )

    @property
    def refutation_count(self) -> int:
        return len(self.refutations)

    @property
    def counter_delta(self) -> int:
        """카운터 감소량 - pair는 -0, left/right는 -1, both는 -2."""
        return -len(self.refutations)

    @property
    def has_discriminating_power(self) -> bool:
        """pair 긋기는 축x장면 판별력-없음 이벤트다."""
        return self.strike_target is not StrikeTarget.PAIR

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "event_id": self.event_id,
            "seq": self.seq,
            "at": self.at,
            "session_id": self.session_id,
            "pair_id": self.pair_id,
            "axis": self.axis,
            "scene_id": self.scene_id,
            "strike_target": self.strike_target.value,
            "refutations": [r.to_dict() for r in self.refutations],
            "counter_delta": self.counter_delta,
            "has_discriminating_power": self.has_discriminating_power,
        }


@dataclass(frozen=True, slots=True)
class Event:
    """긋기 외 이벤트의 공용 봉투 - 스키마 등록된 타입만 허용."""

    type: EventType
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_new_id)
    at: str = field(default_factory=_now)
    seq: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, EventType):
            raise SchemaViolation(f"등록되지 않은 이벤트 타입: {self.type!r}")
        if self.type is EventType.STRIKE:
            raise SchemaViolation("strike 이벤트는 StrikeEvent로 기록한다")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "event_id": self.event_id,
            "seq": self.seq,
            "at": self.at,
            "session_id": self.session_id,
            "payload": dict(self.payload),
        }


def strike(
    session_id: str,
    pair_id: str,
    axis: str,
    scene_id: str,
    target: StrikeTarget | str,
    refutations: tuple[Refutation, ...] | list[Refutation] = (),
    **kwargs: Any,
) -> StrikeEvent:
    """긋기 이벤트 생성자 - 알 수 없는 타깃은 스키마 위반으로 기각한다."""
    try:
        resolved = StrikeTarget(target)
    except ValueError as e:
        raise SchemaViolation(f"허용되지 않은 strike_target: {target!r}") from e
    return StrikeEvent(
        session_id=session_id,
        pair_id=pair_id,
        axis=axis,
        scene_id=scene_id,
        strike_target=resolved,
        refutations=tuple(refutations),
        **kwargs,
    )


class EventLog:
    """append-only 이벤트 스트림. 수정/삭제 경로가 없다."""

    __slots__ = ("_events",)

    def __init__(
        self, events: Iterable[StrikeEvent | Event] = ()
    ) -> None:
        self._events: list[StrikeEvent | Event] = []
        for event in events:
            self.append(event)

    @property
    def events(self) -> tuple[StrikeEvent | Event, ...]:
        return tuple(self._events)

    def append(self, event: StrikeEvent | Event) -> StrikeEvent | Event:
        etype = getattr(event, "type", None)
        if not isinstance(etype, EventType):
            raise SchemaViolation(f"등록되지 않은 이벤트 타입: {etype!r}")
        if not isinstance(event, (StrikeEvent, Event)):
            raise SchemaViolation(f"알 수 없는 이벤트 객체: {type(event).__name__}")
        stamped = replace(event, seq=len(self._events))
        self._events.append(stamped)
        return stamped

    def strikes(self) -> tuple[StrikeEvent, ...]:
        return tuple(e for e in self._events if isinstance(e, StrikeEvent))

    def refutations(self) -> tuple[Refutation, ...]:
        """1:N provenance를 평탄화한 전체 반증 목록."""
        return tuple(r for e in self.strikes() for r in e.refutations)

    def counter_delta(self) -> int:
        return sum(e.counter_delta for e in self.strikes())

    def to_jsonl_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(e.to_dict() for e in self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[StrikeEvent | Event]:
        return iter(tuple(self._events))

    def __getitem__(self, index: int) -> StrikeEvent | Event:
        return self._events[index]

    def __setitem__(self, index: int, value: Any) -> None:
        raise AppendOnlyViolation("이벤트 스트림은 append-only다 - 덮어쓸 수 없다")

    def __delitem__(self, index: int) -> None:
        raise AppendOnlyViolation("이벤트 스트림은 append-only다 - 삭제할 수 없다")
