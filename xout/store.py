"""이벤트 영속화 - 소유 디렉토리 안의 append-only JSONL 스토어.

세션의 단일 진실원은 append-only 이벤트 스트림이다. 이 모듈은 그 스트림을
~/.claude/popper/sessions/<session_id>.jsonl 한 파일에 한 줄 한 이벤트로
내려앉히고, 같은 파일을 다시 읽어 동일한 이벤트 열로 복원한다.

- 쓰기는 append 모드만 쓴다. 덮어쓰기/삭제 경로는 없다.
- 파생 상태(카운터/룰/판정)는 절대 저장하지 않는다 - 항상 replay로 파생한다.
- 직렬화는 events.to_dict() 그대로다. 복원 시 파생 필드(counter_delta 등)는
  버리고 스키마 필드만 되살린다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from xout.compiler import default_base_dir
from xout.events import (
    Event,
    EventType,
    Refutation,
    SchemaViolation,
    StrikeEvent,
    StrikeTarget,
)
from xout.locking import base_lock

logger = logging.getLogger(__name__)

SESSIONS_DIR = "sessions"
EVENT_FILE_SUFFIX = ".jsonl"


def event_sort_key(event: StrikeEvent | Event) -> tuple[str, str, int, str]:
    """동일 시각의 교차 세션 이벤트까지 결정적으로 정렬한다."""
    sequence = event.seq if isinstance(event.seq, int) else -1
    return event.at, event.session_id, sequence, event.event_id


class StoreViolation(RuntimeError):
    """스토어 계약 위반 - 손상된 레코드 또는 잘못된 세션 식별자."""


def _refutation_from(record: Mapping[str, Any]) -> Refutation:
    return Refutation(
        axis=str(record.get("axis", "")),
        value=str(record.get("value", "")),
        fragment_id=str(record.get("fragment_id", "")),
        side=record.get("side"),
    )


def event_from_record(record: Mapping[str, Any]) -> StrikeEvent | Event:
    """JSONL 레코드 한 줄을 이벤트 객체로 복원한다."""
    if not isinstance(record, Mapping):
        raise StoreViolation(f"이벤트 레코드는 매핑이어야 한다: {type(record).__name__}")
    raw_type = record.get("type")
    try:
        etype = EventType(str(raw_type))
    except ValueError as e:
        raise StoreViolation(f"등록되지 않은 이벤트 타입 레코드: {raw_type!r}") from e

    common: dict[str, Any] = {
        "session_id": str(record.get("session_id", "")),
        "event_id": str(record.get("event_id", "")),
        "at": str(record.get("at", "")),
        "seq": record.get("seq") if isinstance(record.get("seq"), int) else None,
    }
    try:
        if etype is EventType.STRIKE:
            return StrikeEvent(
                pair_id=str(record.get("pair_id", "")),
                axis=str(record.get("axis", "")),
                scene_id=str(record.get("scene_id", "")),
                strike_target=StrikeTarget(str(record.get("strike_target"))),
                refutations=tuple(
                    _refutation_from(r) for r in record.get("refutations", ())
                ),
                **common,
            )
        payload = record.get("payload", {})
        if not isinstance(payload, Mapping):
            raise StoreViolation(f"payload는 매핑이어야 한다: {type(payload).__name__}")
        return Event(type=etype, payload=dict(payload), **common)
    except (SchemaViolation, ValueError) as e:
        raise StoreViolation(f"이벤트 레코드를 복원하지 못했다: {e}") from e


def _validate_session_id(session_id: str) -> str:
    if not session_id or any(ch in session_id for ch in "/\\") or session_id.startswith("."):
        raise StoreViolation(f"세션 식별자가 파일 이름으로 부적합하다: {session_id!r}")
    return session_id


class EventStore:
    """append-only JSONL 세션 스토어 - 소유 디렉토리 밖에는 쓰지 않는다."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        root = Path(base_dir) if base_dir is not None else default_base_dir()
        self.base_dir = root
        self.sessions_dir = root / SESSIONS_DIR
        self.lock = base_lock(root)

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{_validate_session_id(session_id)}{EVENT_FILE_SUFFIX}"

    def append(self, event: StrikeEvent | Event) -> Path:
        """이벤트 한 건을 해당 세션 파일 끝에 덧붙인다 - 유일한 쓰기 경로."""
        with self.lock:
            path = self.session_path(event.session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return path

    def _read(self, path: Path) -> Iterator[StrikeEvent | Event]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise StoreViolation(f"세션 파일을 읽지 못했다: {path}") from e
        lines = raw.splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                if number == len(lines) and not raw.endswith(("\n", "\r")):
                    raise StoreViolation(
                        f"{path.name}:{number} 부분 쓰기 꼬리 감지"
                    ) from e
                raise StoreViolation(f"{path.name}:{number} JSONL 파싱 실패") from e
            yield event_from_record(record)

    def load_session(self, session_id: str) -> tuple[StrikeEvent | Event, ...]:
        """세션 하나의 이벤트 열 - 파일이 없으면 빈 스트림이다."""
        with self.lock:
            path = self.session_path(session_id)
            if not path.exists():
                return ()
            return tuple(self._read(path))

    def session_ids(self) -> tuple[str, ...]:
        """저장된 세션 식별자 - 파일시스템 메타데이터에 의존하지 않는 순서."""
        with self.lock:
            if not self.sessions_dir.is_dir():
                return ()
            files = [
                path
                for path in self.sessions_dir.iterdir()
                if path.is_file() and path.suffix == EVENT_FILE_SUFFIX
            ]
            files.sort(key=lambda p: p.name)
            return tuple(path.stem for path in files)

    def load_all(self) -> tuple[StrikeEvent | Event, ...]:
        """전 세션 이벤트를 세션 기록 순서로 이어 붙인 누적 스트림."""
        with self.lock:
            events: list[StrikeEvent | Event] = []
            for session_id in self.session_ids():
                events.extend(self.load_session(session_id))
            # 이벤트의 논리적 기록 시각이 파일 mtime보다 우선한다.
            events.sort(key=event_sort_key)
            return tuple(events)

    def load_completed(self) -> tuple[StrikeEvent | Event, ...]:
        """완료 이벤트가 없는 진행 중 세션을 제외한 안정된 누적 스트림."""
        with self.lock:
            events = self.load_all()
            started = {
                event.session_id
                for event in events
                if isinstance(event, Event) and event.type is EventType.SESSION_START
            }
            completed = {
                event.session_id
                for event in events
                if isinstance(event, Event)
                and event.type
                in (EventType.SESSION_VALIDATED, EventType.SESSION_VOIDED)
            }
            return tuple(
                event
                for event in events
                if event.session_id not in started or event.session_id in completed
            )
