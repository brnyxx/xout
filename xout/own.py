"""사용자가 자기 말로 적은 줄 - 8축 카탈로그 바깥, 측정하지 않는다.

카탈로그는 얼려 두지만 사람마다 싫어하는 행동은 남는다. 이 모듈은 그 한 문장을
append-only 스트림에 담고(own_line_added), 무르는 것도 새 이벤트로만 처리한다
(own_line_dropped). 지금 남아 있는 줄은 두 이벤트의 순수 fold다: 추가된 순서
그대로, tombstone이 가리킨 id만 빠진다. 어떤 경로도 이벤트를 고치거나 지우지
않는다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from xout.events import Event, EventType

#: 줄 이벤트가 사는 세션 - session_start가 없으므로 완주 판정에서 제외되지 않는다.
OWN_LINE_SESSION_ID = "own-lines"

MIN_LENGTH = 1
MAX_LENGTH = 240


class OwnLineError(ValueError):
    """사용자가 적은 줄이 계약을 어겼다 - code는 CLI 문안 키다."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True, slots=True)
class OwnLine:
    """살아 있는 줄 하나 - fold의 결과물이며 저장되지 않는다."""

    line_id: str
    text: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.line_id, "text": self.text, "created_at": self.created_at}


def normalize(text: str) -> str:
    """중복 판정용 정규형 - 대소문자와 공백 폭 차이는 같은 줄로 본다."""
    return " ".join(text.split()).casefold()


def clean(text: str) -> str:
    """저장할 본문 - 앞뒤 공백만 턴다. 빈 줄, 여러 줄, 길이 초과는 여기서 기각한다."""
    body = text.strip()
    if len(body) < MIN_LENGTH:
        raise OwnLineError("empty")
    if len(body.splitlines()) > 1:
        raise OwnLineError("multiline")
    if len(body) > MAX_LENGTH:
        raise OwnLineError("too_long", str(len(body)))
    return body


def fold_own_lines(events: Iterable[Any]) -> tuple[OwnLine, ...]:
    """추가된 줄에서 tombstone이 가리킨 줄을 뺀 나머지 - 삽입 순서 그대로."""
    order: list[str] = []
    live: dict[str, OwnLine] = {}
    for event in events:
        etype = getattr(event, "type", None)
        payload = getattr(event, "payload", None) or {}
        if etype is EventType.OWN_LINE_ADDED:
            line_id = str(payload.get("id", ""))
            text = str(payload.get("text", ""))
            if not line_id or not text or line_id in live:
                continue
            order.append(line_id)
            live[line_id] = OwnLine(
                line_id=line_id,
                text=text,
                created_at=str(payload.get("created_at", "")),
            )
        elif etype is EventType.OWN_LINE_DROPPED:
            live.pop(str(payload.get("id", "")), None)
    return tuple(live[line_id] for line_id in order if line_id in live)


def _new_line_id(existing: Sequence[OwnLine]) -> str:
    taken = {line.line_id for line in existing}
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in taken:
            return candidate


def added_event(
    text: str,
    existing: Sequence[OwnLine] = (),
    *,
    session_id: str = OWN_LINE_SESSION_ID,
    now: str | None = None,
) -> Event:
    """줄 추가 이벤트 - 같은 정규형이 이미 살아 있으면 기각한다."""
    body = clean(text)
    key = normalize(body)
    if any(normalize(line.text) == key for line in existing):
        raise OwnLineError("duplicate", body)
    stamp = now if now is not None else datetime.now(timezone.utc).isoformat()
    return Event(
        type=EventType.OWN_LINE_ADDED,
        session_id=session_id,
        payload={"id": _new_line_id(existing), "text": body, "created_at": stamp},
    )


def dropped_event(
    line_id: str,
    existing: Sequence[OwnLine],
    *,
    session_id: str = OWN_LINE_SESSION_ID,
) -> tuple[Event, OwnLine]:
    """줄 회수 tombstone - 살아 있는 id만 가리킬 수 있다."""
    match = next((line for line in existing if line.line_id == line_id), None)
    if match is None:
        raise OwnLineError("unknown_id", line_id)
    return (
        Event(
            type=EventType.OWN_LINE_DROPPED,
            session_id=session_id,
            payload={"id": match.line_id},
        ),
        match,
    )
