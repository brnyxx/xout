"""append-only JSONL 스토어 검증 - 직렬화 왕복, 세션 순서, 손상 거부."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from xout.counter import fold
from xout.events import Event, EventType, Refutation, StrikeEvent, StrikeTarget
from xout.store import EventStore, StoreViolation, event_from_record, event_sort_key


def sample_strike(session_id: str = "s1") -> StrikeEvent:
    return StrikeEvent(
        session_id=session_id,
        pair_id="scene:autonomy:a|b",
        axis="autonomy",
        scene_id="scene",
        strike_target=StrikeTarget.LEFT,
        refutations=(
            Refutation(
                axis="autonomy", value="ask_first", fragment_id="f1", side="left"
            ),
        ),
    )


class RoundTripTest(unittest.TestCase):
    def test_strike_event_round_trips_through_the_record(self) -> None:
        original = sample_strike()
        restored = event_from_record(original.to_dict())
        self.assertIsInstance(restored, StrikeEvent)
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_envelope_event_round_trips(self) -> None:
        original = Event(
            type=EventType.SESSION_START, session_id="s1", payload={"profile": "product"}
        )
        restored = event_from_record(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(StoreViolation):
            event_from_record({"type": "approval", "session_id": "s1"})

    def test_derived_fields_in_the_record_are_ignored(self) -> None:
        record = sample_strike().to_dict()
        record["counter_delta"] = 99
        restored = event_from_record(record)
        self.assertEqual(restored.counter_delta, -1)

    def test_equal_timestamps_have_a_total_cross_session_order(self) -> None:
        at = "2026-01-01T00:00:00+00:00"
        events = (
            Event(
                type=EventType.SESSION_START,
                session_id="z-session",
                event_id="z-0",
                at=at,
                seq=0,
            ),
            Event(
                type=EventType.SESSION_START,
                session_id="a-session",
                event_id="a-1",
                at=at,
                seq=1,
            ),
            Event(
                type=EventType.SESSION_START,
                session_id="a-session",
                event_id="a-0",
                at=at,
                seq=0,
            ),
        )
        ordered = sorted(events, key=event_sort_key)
        self.assertEqual([event.event_id for event in ordered], ["a-0", "a-1", "z-0"])


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(Path(self.tmp.name))

    def test_append_then_load_reproduces_the_stream(self) -> None:
        events = [
            Event(
                type=EventType.SESSION_START,
                session_id="s1",
                payload={"profile": "product"},
            ),
            sample_strike(),
        ]
        for event in events:
            self.store.append(event)
        loaded = self.store.load_session("s1")
        self.assertEqual(
            [e.to_dict() for e in loaded], [e.to_dict() for e in events]
        )
        self.assertEqual(
            fold(loaded).remaining_combinations,
            fold(events).remaining_combinations,
        )

    def test_append_is_append_only_on_disk(self) -> None:
        self.store.append(sample_strike())
        path = self.store.session_path("s1")
        before = path.read_text(encoding="utf-8")
        self.store.append(
            Event(type=EventType.SESSION_VALIDATED, session_id="s1", payload={})
        )
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before), "기존 줄이 변형됐다")

    def test_missing_session_is_an_empty_stream(self) -> None:
        self.assertEqual(self.store.load_session("ghost"), ())

    def test_corrupted_line_is_rejected_loudly(self) -> None:
        self.store.append(sample_strike())
        path = self.store.session_path("s1")
        path.write_text(
            path.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8"
        )
        with self.assertRaises(StoreViolation):
            self.store.load_session("s1")

    def test_partial_tail_fails_stop_with_location(self) -> None:
        self.store.append(sample_strike())
        path = self.store.session_path("s1")
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"strike","event_id"')
        with self.assertRaisesRegex(StoreViolation, r"s1\.jsonl:2"):
            self.store.load_session("s1")

    def test_mtime_does_not_change_logical_event_order(self) -> None:
        first = Event(
            type=EventType.SESSION_START,
            session_id="a",
            at="2024-01-01T00:00:00+00:00",
            payload={},
        )
        second = Event(
            type=EventType.SESSION_START,
            session_id="b",
            at="2024-01-02T00:00:00+00:00",
            payload={},
        )
        self.store.append(first)
        self.store.append(second)
        os.utime(self.store.session_path("a"), (9999999999, 9999999999))
        self.assertEqual(
            [event.session_id for event in self.store.load_all()], ["a", "b"]
        )

    def test_session_ids_follow_write_order(self) -> None:
        self.store.append(sample_strike("a"))
        self.store.append(sample_strike("b"))
        self.assertEqual(set(self.store.session_ids()), {"a", "b"})
        merged = self.store.load_all()
        self.assertEqual(len(merged), 2)

    def test_path_traversal_session_id_is_rejected(self) -> None:
        for bad in ("../evil", "a/b", ".hidden", ""):
            with self.subTest(session_id=bad):
                with self.assertRaises(StoreViolation):
                    self.store.session_path(bad)


if __name__ == "__main__":
    unittest.main()
