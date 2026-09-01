"""AC1 - 이벤트 로그가 strike-only append-only로 동작하는지 검증한다."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from xout.events import (
    POSITIVE_INTENT_LEXEMES,
    REFUTATION_ARITY,
    AppendOnlyViolation,
    Event,
    EventLog,
    EventType,
    Refutation,
    SchemaViolation,
    StrikeEvent,
    StrikeTarget,
    positive_intent_violations,
    strike,
)

SESSION = "sess-1"


def _left(axis: str = "autonomy", value: str = "ask-first") -> Refutation:
    return Refutation(axis=axis, value=value, fragment_id=f"{axis}:L", side="left")


def _right(axis: str = "autonomy", value: str = "act-freely") -> Refutation:
    return Refutation(axis=axis, value=value, fragment_id=f"{axis}:R", side="right")


def _strike(target, refutations=(), axis="autonomy", pair_id="pair-1"):
    return strike(
        session_id=SESSION,
        pair_id=pair_id,
        axis=axis,
        scene_id="scene-1",
        target=target,
        refutations=refutations,
    )


class FourTargetsRecordedTest(unittest.TestCase):
    """left/right/both/pair 네 타깃이 각각 기록된다."""

    def test_strike_target_enum_has_exactly_four_values(self) -> None:
        self.assertEqual(
            [t.value for t in StrikeTarget], ["left", "right", "both", "pair"]
        )

    def test_each_of_four_targets_is_recorded_in_order(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.LEFT, (_left(),)))
        log.append(_strike(StrikeTarget.RIGHT, (_right(),)))
        log.append(_strike(StrikeTarget.BOTH, (_left("verbosity"), _right("verbosity")), axis="verbosity"))
        log.append(_strike(StrikeTarget.PAIR, (), axis="scope"))

        self.assertEqual(len(log), 4)
        self.assertEqual(
            [e.strike_target for e in log.strikes()],
            [
                StrikeTarget.LEFT,
                StrikeTarget.RIGHT,
                StrikeTarget.BOTH,
                StrikeTarget.PAIR,
            ],
        )
        self.assertEqual([e.seq for e in log.strikes()], [0, 1, 2, 3])
        self.assertTrue(all(e.type is EventType.STRIKE for e in log.strikes()))

    def test_targets_accept_raw_string_form(self) -> None:
        log = EventLog()
        for target in ("left", "right", "both", "pair"):
            refutations = {
                "left": (_left(),),
                "right": (_right(),),
                "both": (_left(), _right()),
                "pair": (),
            }[target]
            log.append(_strike(target, refutations))
        self.assertEqual(
            [e.strike_target.value for e in log.strikes()],
            ["left", "right", "both", "pair"],
        )

    def test_unknown_target_is_rejected(self) -> None:
        for bogus in ("approve", "keep", "skip", "neither", ""):
            with self.subTest(target=bogus):
                with self.assertRaises(SchemaViolation):
                    _strike(bogus)


class OneToManyProvenanceTest(unittest.TestCase):
    """both는 한 이벤트에 반증 2건의 1:N provenance를 남긴다."""

    def test_both_carries_two_refutations_in_a_single_event(self) -> None:
        event = _strike(StrikeTarget.BOTH, (_left(), _right()))
        self.assertIsInstance(event, StrikeEvent)
        self.assertEqual(event.refutation_count, 2)
        self.assertEqual(len(event.refutations), 2)
        self.assertEqual(
            sorted(r.side for r in event.refutations), ["left", "right"]
        )
        self.assertEqual(
            sorted(r.fragment_id for r in event.refutations),
            ["autonomy:L", "autonomy:R"],
        )

    def test_both_is_one_event_not_two(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.BOTH, (_left(), _right())))
        self.assertEqual(len(log), 1)
        self.assertEqual(len(log.refutations()), 2)

    def test_counter_delta_is_variable_zero_one_two(self) -> None:
        cases = [
            (StrikeTarget.PAIR, (), 0),
            (StrikeTarget.LEFT, (_left(),), -1),
            (StrikeTarget.RIGHT, (_right(),), -1),
            (StrikeTarget.BOTH, (_left(), _right()), -2),
        ]
        for target, refutations, expected in cases:
            with self.subTest(target=target):
                self.assertEqual(_strike(target, refutations).counter_delta, expected)

    def test_log_counter_delta_folds_all_strikes(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.LEFT, (_left(),)))
        log.append(_strike(StrikeTarget.BOTH, (_left(), _right())))
        log.append(_strike(StrikeTarget.PAIR, ()))
        self.assertEqual(log.counter_delta(), -3)
        self.assertEqual(len(log.refutations()), 3)

    def test_refutation_arity_is_enforced_per_target(self) -> None:
        self.assertEqual(
            REFUTATION_ARITY,
            {
                StrikeTarget.LEFT: 1,
                StrikeTarget.RIGHT: 1,
                StrikeTarget.BOTH: 2,
                StrikeTarget.PAIR: 0,
            },
        )
        with self.assertRaises(SchemaViolation):
            _strike(StrikeTarget.BOTH, (_left(),))
        with self.assertRaises(SchemaViolation):
            _strike(StrikeTarget.LEFT, (_left(), _right()))
        with self.assertRaises(SchemaViolation):
            _strike(StrikeTarget.PAIR, (_left(),))

    def test_both_requires_one_refutation_per_side(self) -> None:
        with self.assertRaises(SchemaViolation):
            _strike(StrikeTarget.BOTH, (_left(), _left("autonomy", "other")))

    def test_side_must_match_single_sided_target(self) -> None:
        with self.assertRaises(SchemaViolation):
            _strike(StrikeTarget.LEFT, (_right(),))


class PairStrikeTest(unittest.TestCase):
    """pair 긋기는 축x장면 판별력-없음 이벤트로 기록된다."""

    def test_pair_strike_records_zero_refutations(self) -> None:
        event = _strike(StrikeTarget.PAIR, ())
        self.assertEqual(event.refutations, ())
        self.assertEqual(event.counter_delta, 0)
        self.assertFalse(event.has_discriminating_power)

    def test_non_pair_strikes_have_discriminating_power(self) -> None:
        for target, refutations in (
            (StrikeTarget.LEFT, (_left(),)),
            (StrikeTarget.RIGHT, (_right(),)),
            (StrikeTarget.BOTH, (_left(), _right())),
        ):
            with self.subTest(target=target):
                self.assertTrue(_strike(target, refutations).has_discriminating_power)

    def test_pair_strike_keeps_axis_and_scene_attribution(self) -> None:
        event = _strike(StrikeTarget.PAIR, (), axis="commit_style")
        self.assertEqual(event.axis, "commit_style")
        self.assertEqual(event.scene_id, "scene-1")


class NoApprovalEventTypeTest(unittest.TestCase):
    """승인/생존을 기록하는 이벤트 타입은 스키마에 존재하지 않는다."""

    def test_schema_has_no_positive_intent_event_type(self) -> None:
        self.assertEqual(positive_intent_violations(), ())

    def test_forbidden_names_are_absent_from_event_type(self) -> None:
        values = {t.value for t in EventType}
        names = {t.name for t in EventType}
        forbidden = {
            "approve",
            "approval",
            "approved",
            "accept",
            "accepted",
            "confirm",
            "confirmed",
            "confirmation",
            "survive",
            "survived",
            "survival",
            "keep",
            "endorse",
            "affirm",
            "agree",
            "select",
            "skip",
            "yes",
        }
        for word in forbidden:
            with self.subTest(word=word):
                self.assertNotIn(word, values)
                self.assertNotIn(word.upper(), names)
                self.assertFalse(
                    any(word in v.split("_") for v in values),
                    f"승인/생존 어휘가 이벤트 타입에 등장한다: {word}",
                )

    def test_forbidden_lexicon_is_non_empty_so_the_guard_can_bite(self) -> None:
        self.assertIn("approve", POSITIVE_INTENT_LEXEMES)
        self.assertIn("survive", POSITIVE_INTENT_LEXEMES)
        self.assertIn("confirm", POSITIVE_INTENT_LEXEMES)

    def test_only_strike_event_carries_user_input(self) -> None:
        user_input_types = {
            t for t in EventType if t.value in {"strike", "undo_tombstone"}
        }
        self.assertEqual(
            {t.value for t in user_input_types}, {"strike", "undo_tombstone"}
        )
        self.assertNotIn("survival", {t.value for t in EventType})

    def test_log_rejects_unregistered_event_type(self) -> None:
        class Fake:
            type = "approval"

        log = EventLog()
        with self.assertRaises(SchemaViolation):
            log.append(Fake())

    def test_event_envelope_rejects_unregistered_type(self) -> None:
        with self.assertRaises(SchemaViolation):
            Event(type="approve", session_id=SESSION)

    def test_survival_is_absence_of_a_strike_not_an_event(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.LEFT, (_left(),)))
        self.assertEqual(len(log), 1)
        self.assertEqual(
            {r.value for r in log.refutations()},
            {"ask-first"},
        )
        # 생존한 오른쪽 값은 어떤 이벤트로도 기록되지 않는다.
        self.assertEqual(
            [e for e in log.to_jsonl_records() if e["type"] != "strike"], []
        )


class AppendOnlyTest(unittest.TestCase):
    """이벤트 스트림은 append-only다."""

    def test_log_has_no_mutating_api(self) -> None:
        for name in (
            "pop",
            "remove",
            "clear",
            "insert",
            "extend",
            "sort",
            "reverse",
            "delete",
            "update",
            "replace",
            "truncate",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(EventLog, name), f"EventLog에 변경 API가 있다: {name}"
                )

    def test_item_assignment_and_deletion_are_refused(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.LEFT, (_left(),)))
        with self.assertRaises(AppendOnlyViolation):
            log[0] = _strike(StrikeTarget.RIGHT, (_right(),))
        with self.assertRaises(AppendOnlyViolation):
            del log[0]
        self.assertEqual(len(log), 1)

    def test_events_snapshot_is_an_immutable_copy(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.LEFT, (_left(),)))
        snapshot = log.events
        self.assertIsInstance(snapshot, tuple)
        log.append(_strike(StrikeTarget.PAIR, ()))
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(len(log.events), 2)

    def test_events_are_frozen_records(self) -> None:
        event = _strike(StrikeTarget.LEFT, (_left(),))
        with self.assertRaises(FrozenInstanceError):
            event.strike_target = StrikeTarget.RIGHT
        with self.assertRaises(FrozenInstanceError):
            event.refutations[0].axis = "verbosity"

    def test_appended_order_and_seq_are_stable(self) -> None:
        log = EventLog()
        log.append(Event(type=EventType.SESSION_START, session_id=SESSION))
        for _ in range(5):
            log.append(_strike(StrikeTarget.PAIR, ()))
        self.assertEqual([e.seq for e in log], list(range(6)))
        self.assertIs(log[0].type, EventType.SESSION_START)

    def test_replay_of_records_is_deterministic(self) -> None:
        log = EventLog()
        log.append(_strike(StrikeTarget.BOTH, (_left(), _right())))
        log.append(_strike(StrikeTarget.PAIR, ()))
        self.assertEqual(log.to_jsonl_records(), log.to_jsonl_records())
        self.assertEqual(
            [r["strike_target"] for r in log.to_jsonl_records()], ["both", "pair"]
        )


if __name__ == "__main__":
    unittest.main()
