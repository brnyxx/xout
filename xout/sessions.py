"""저장된 세션의 bounded 목록, 상세 상태, 재개 후보 판정."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from xout.events import Event, EventType, StrikeEvent
from xout.session import PROFILE_RECHECK, load_session_specs

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_VOIDED = "voided"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    profile: str
    status: str
    started_at: str
    updated_at: str
    slots_used: int
    slots_total: int
    event_count: int
    last_axis: str | None
    voided_reason: str | None

    @property
    def resumable(self) -> bool:
        return self.status == STATUS_IN_PROGRESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "slots_used": self.slots_used,
            "slots_total": self.slots_total,
            "event_count": self.event_count,
            "last_axis": self.last_axis,
            "voided_reason": self.voided_reason,
            "resumable": self.resumable,
        }


def summarize_session(events: Sequence[StrikeEvent | Event]) -> SessionSummary:
    if not events:
        raise ValueError("빈 이벤트 스트림은 세션으로 요약할 수 없다")
    session_ids = {event.session_id for event in events}
    if len(session_ids) != 1:
        raise ValueError("한 세션 요약에 여러 session_id가 들어왔다")
    opening = next(
        (
            event
            for event in events
            if isinstance(event, Event) and event.type is EventType.SESSION_START
        ),
        None,
    )
    if opening is None:
        raise ValueError("session_start가 없는 세션은 요약할 수 없다")

    profile = str(
        opening.payload.get("profile") or opening.payload.get("session_kind") or "unknown"
    )
    terminal = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, Event)
            and event.type in (EventType.SESSION_VALIDATED, EventType.SESSION_VOIDED)
        ),
        None,
    )
    status = STATUS_IN_PROGRESS
    reason: str | None = None
    if terminal is not None and terminal.type is EventType.SESSION_VOIDED:
        status = STATUS_VOIDED
        reason = str(terminal.payload.get("reason") or "unknown")
    elif terminal is not None:
        status = STATUS_COMPLETE

    if profile == PROFILE_RECHECK:
        slots_total = int(opening.payload.get("recheck_budget", 0))
    else:
        raw_spec = opening.payload.get("session_spec")
        if isinstance(raw_spec, dict):
            discriminative = raw_spec.get("discriminative_slots")
            probe_slots = raw_spec.get("probe_slots")
            slots_total = (
                discriminative + len(probe_slots)
                if isinstance(discriminative, int)
                and not isinstance(discriminative, bool)
                and isinstance(probe_slots, list)
                else 0
            )
        else:
            spec = load_session_specs().get(profile)
            slots_total = spec.total_slots if spec is not None else 0
    slots_used = sum(
        1
        for event in events
        if isinstance(event, StrikeEvent)
        or (isinstance(event, Event) and event.type is EventType.PROBE_SHOWN)
    )
    last_axis = next(
        (
            event.axis
            for event in reversed(events)
            if isinstance(event, StrikeEvent)
        ),
        None,
    )
    ordered = sorted(events, key=lambda event: (event.at, event.seq or -1, event.event_id))
    return SessionSummary(
        session_id=next(iter(session_ids)),
        profile=profile,
        status=status,
        started_at=opening.at,
        updated_at=ordered[-1].at,
        slots_used=slots_used,
        slots_total=slots_total,
        event_count=len(events),
        last_axis=last_axis,
        voided_reason=reason,
    )


def summarize_sessions(
    events: Iterable[StrikeEvent | Event], *, limit: int | None = None
) -> tuple[SessionSummary, ...]:
    by_session: dict[str, list[StrikeEvent | Event]] = {}
    for event in events:
        by_session.setdefault(event.session_id, []).append(event)
    summaries: list[SessionSummary] = []
    for session_events in by_session.values():
        if not any(
            isinstance(event, Event) and event.type is EventType.SESSION_START
            for event in session_events
        ):
            continue
        summaries.append(summarize_session(session_events))
    summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
    if limit is not None:
        summaries = summaries[: max(limit, 0)]
    return tuple(summaries)


def latest_resumable(
    events: Iterable[StrikeEvent | Event], session_id: str | None = None
) -> SessionSummary | None:
    summaries = summarize_sessions(events)
    for summary in summaries:
        if not summary.resumable:
            continue
        if session_id is None or summary.session_id == session_id:
            return summary
    return None
