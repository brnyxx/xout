"""오긋기 복구 채널 테스트 - 두 번째 동사 없이 명시 이벤트로만 복구되는가."""

from __future__ import annotations

import pytest

from xout.events import (
    Event,
    EventLog,
    EventType,
    Refutation,
    StrikeTarget,
    strike,
)
from xout.recovery import (
    DEFAULT_AXIS_CATALOG,
    RECHECK_CLASS_PRIORITY,
    RecheckEntry,
    RecoveryChannel,
    RecoveryViolation,
    build_recheck_queue,
    fold_recovery,
    revive,
    strike_key,
    undo_verbs,
)

AXIS = "자율성"
V1, V2, V3 = DEFAULT_AXIS_CATALOG[AXIS]
SID = "session-main"
RID = "session-recheck"
FULL_SPACE = 3 ** 8


def left_strike(value: str, fragment: str, pair_id: str = "pair-1"):
    return strike(
        SID,
        pair_id,
        AXIS,
        "scene-1",
        StrikeTarget.LEFT,
        (Refutation(axis=AXIS, value=value, fragment_id=fragment, side="left"),),
    )


def both_strike(fragment_left: str, fragment_right: str, pair_id: str = "pair-1"):
    return strike(
        SID,
        pair_id,
        AXIS,
        "scene-1",
        StrikeTarget.BOTH,
        (
            Refutation(axis=AXIS, value=V1, fragment_id=fragment_left, side="left"),
            Refutation(axis=AXIS, value=V2, fragment_id=fragment_right, side="right"),
        ),
    )


def pair_strike(pair_id: str = "pair-1"):
    return strike(SID, pair_id, AXIS, "scene-1", StrikeTarget.PAIR, ())


def session_start(session_id: str, kind: str) -> Event:
    return Event(
        type=EventType.SESSION_START,
        session_id=session_id,
        payload={"session_kind": kind, "profile": "product"},
    )


def session_end(session_id: str = SID) -> Event:
    return Event(type=EventType.SESSION_VALIDATED, session_id=session_id, payload={})


# --- 두 번째 동사 부재 -------------------------------------------------------


def test_strike_target_enum_stays_four_values() -> None:
    assert [t.value for t in StrikeTarget] == ["left", "right", "both", "pair"]


def test_only_verb_is_strike() -> None:
    assert undo_verbs() == ("strike",)


def test_no_cancel_or_confirm_event_channel() -> None:
    names = {t.value for t in EventType}
    assert not {n for n in names if "cancel" in n or "confirm" in n or "undo_button" in n}
    assert {c.value for c in RecoveryChannel} <= names


# --- undo_tombstone ----------------------------------------------------------


def test_identical_combination_restrike_is_read_as_tombstone() -> None:
    first = left_strike(V1, "frag-1")
    repeat = left_strike(V1, "frag-1")
    assert strike_key(first) == strike_key(repeat)
    assert strike_key(first) == ("pair-1", ("frag-1",), "left")

    state = fold_recovery([session_start(SID, "main"), first, repeat])

    assert len(state.tombstones) == 1
    tomb = state.tombstones[0]
    assert tomb.channel is RecoveryChannel.UNDO_TOMBSTONE
    assert tomb.strike_event_id == first.event_id
    assert tomb.voided_pairs == ((AXIS, V1),)


def test_tombstone_voids_original_refutation_provenance() -> None:
    first = left_strike(V1, "frag-1")
    before = fold_recovery([session_start(SID, "main"), first])
    after = fold_recovery([session_start(SID, "main"), first, left_strike(V1, "frag-1")])

    assert before.warranted_refutations == ((first.event_id, AXIS, V1),)
    assert after.warranted_refutations == ()
    assert after.axis_states[AXIS].refutation_count == 0
    assert after.pending_revive[0].strike_event_id == first.event_id


def test_tombstone_does_not_raise_counter_inside_session() -> None:
    events = [session_start(SID, "main"), left_strike(V1, "frag-1"), left_strike(V1, "frag-1")]
    state = fold_recovery(events)

    assert state.remaining_trace == (FULL_SPACE, FULL_SPACE // 3 * 2, FULL_SPACE // 3 * 2)
    assert state.remaining_hypotheses == 4374
    assert state.revive_indices == frozenset()


def test_third_identical_strike_reactivates_the_refutation() -> None:
    events = [
        session_start(SID, "main"),
        left_strike(V1, "frag-1"),
        left_strike(V1, "frag-1"),
        left_strike(V1, "frag-1"),
    ]
    state = fold_recovery(events)

    assert len(state.warranted_refutations) == 1
    assert state.pending_revive == ()


def test_explicit_tombstone_uses_the_same_recovery_dialect() -> None:
    first = left_strike(V1, "frag-1")
    tombstone = Event(
        type=EventType.UNDO_TOMBSTONE,
        session_id=SID,
        payload={"strike_event_id": first.event_id},
    )

    undone = fold_recovery([session_start(SID, "main"), first, tombstone])
    assert undone.warranted_refutations == ()
    assert undone.axis_states[AXIS].refutation_count == 0

    redone = fold_recovery(
        [
            session_start(SID, "main"),
            first,
            tombstone,
            left_strike(V1, "frag-1"),
        ]
    )
    assert len(redone.warranted_refutations) == 1
    assert redone.pending_revive == ()


def test_different_fragment_is_not_an_undo() -> None:
    events = [session_start(SID, "main"), left_strike(V1, "frag-1"), left_strike(V1, "frag-9")]
    state = fold_recovery(events)

    assert state.tombstones == ()
    assert state.axis_states[AXIS].surviving == (V2, V3)


def test_pair_strike_has_zero_arity_and_still_supports_tombstone() -> None:
    events = [session_start(SID, "main"), pair_strike(), pair_strike()]
    state = fold_recovery(events)

    assert state.remaining_hypotheses == FULL_SPACE
    assert state.tombstones[0].voided_pairs == ()


# --- contradiction -----------------------------------------------------------


def test_strike_on_last_surviving_value_derives_contradiction() -> None:
    wipe = both_strike("frag-l", "frag-r")
    last = left_strike(V3, "frag-last")
    state = fold_recovery([session_start(SID, "main"), wipe, last])

    assert len(state.contradictions) == 1
    record = state.contradictions[0]
    assert record.channel is RecoveryChannel.CONTRADICTION
    assert (record.axis, record.value, record.applied_arity) == (AXIS, V3, 0)
    assert record.to_event().type is EventType.CONTRADICTION


def test_contradiction_keeps_axis_alive_and_forces_retest() -> None:
    events = [session_start(SID, "main"), both_strike("frag-l", "frag-r"), left_strike(V3, "frag-last")]
    state = fold_recovery(events)
    axis_state = state.axis_states[AXIS]

    assert axis_state.surviving == (V3,)
    assert axis_state.contested is True
    assert axis_state.discrimination == "untested"
    assert state.contested_axes == (AXIS,)
    assert state.forced_retest_axes == (AXIS,)
    assert state.remaining_hypotheses == 3 ** 7
    assert all(len(s.surviving) >= 1 for s in state.axis_states.values())


def test_already_eliminated_value_is_idempotent() -> None:
    events = [
        session_start(SID, "main"),
        left_strike(V1, "frag-1"),
        left_strike(V1, "frag-2"),
    ]
    state = fold_recovery(events)

    assert state.contradictions == ()
    assert state.eliminated_pairs == 1
    assert state.remaining_hypotheses == 4374


def test_undoing_the_contradiction_strike_clears_contested() -> None:
    last = left_strike(V3, "frag-last")
    events = [
        session_start(SID, "main"),
        both_strike("frag-l", "frag-r"),
        last,
        left_strike(V3, "frag-last"),
    ]
    state = fold_recovery(events)

    assert state.axis_states[AXIS].contested is False


# --- revive ------------------------------------------------------------------


def test_revive_at_act_boundary_raises_the_product_counter() -> None:
    original = left_strike(V1, "frag-1")
    events = [
        session_start(SID, "main"),
        original,
        left_strike(V1, "frag-1"),
        session_end(),
        session_start(RID, "recheck"),
        revive(RID, original.event_id),
    ]
    state = fold_recovery(events)

    assert state.remaining_trace[-2] == 4374
    assert state.remaining_hypotheses == FULL_SPACE
    assert state.rejected_revives == ()
    assert state.revive_indices == frozenset({5})
    assert state.axis_states[AXIS].revived == (V1,)


def test_revive_requires_original_strike_reference() -> None:
    with pytest.raises(RecoveryViolation):
        revive(RID, "")


def test_unknown_strike_reference_is_rejected() -> None:
    events = [
        session_start(SID, "main"),
        left_strike(V1, "frag-1"),
        session_end(),
        session_start(RID, "recheck"),
        revive(RID, "not-a-strike"),
    ]
    state = fold_recovery(events)

    assert state.rejected_revives[0].reason == "원 strike 참조 없음"
    assert state.remaining_hypotheses == 4374


def test_revive_inside_main_session_is_rejected() -> None:
    original = left_strike(V1, "frag-1")
    state = fold_recovery([session_start(SID, "main"), original, revive(SID, original.event_id)])

    assert len(state.rejected_revives) == 1
    assert "막 경계" in state.rejected_revives[0].reason
    assert state.remaining_hypotheses == 4374
    assert state.revive_indices == frozenset()


def test_revive_after_a_strike_in_recheck_session_is_rejected() -> None:
    original = left_strike(V1, "frag-1")
    events = [
        session_start(SID, "main"),
        original,
        session_end(),
        session_start(RID, "recheck"),
        left_strike(V2, "frag-2"),
        revive(RID, original.event_id),
    ]
    state = fold_recovery(events)

    assert len(state.rejected_revives) == 1
    assert state.revive_indices == frozenset()


def test_counter_is_monotonic_except_at_revive() -> None:
    original = left_strike(V1, "frag-1")
    events = [
        session_start(SID, "main"),
        original,
        left_strike(V1, "frag-1"),
        left_strike(V2, "frag-2"),
        session_end(),
        session_start(RID, "recheck"),
        revive(RID, original.event_id),
    ]
    state = fold_recovery(events)
    trace = state.remaining_trace

    rises = [i for i in range(1, len(trace)) if trace[i] > trace[i - 1]]
    assert rises == sorted(state.revive_indices)
    assert len(rises) == 1


# --- 강등과 재심 큐 ----------------------------------------------------------


def test_revived_and_contested_values_are_demoted_to_untested() -> None:
    original = left_strike(V1, "frag-1")
    events = [
        session_start(SID, "main"),
        original,
        left_strike(V1, "frag-1"),
        session_end(),
        session_start(RID, "recheck"),
        revive(RID, original.event_id),
    ]
    state = fold_recovery(events)

    assert state.demotions[0].cause is RecoveryChannel.REVIVE
    assert state.axis_states[AXIS].discrimination == "untested"


def test_demoted_values_lead_the_untested_prior_class() -> None:
    events = [session_start(SID, "main"), both_strike("frag-l", "frag-r"), left_strike(V3, "frag-last")]
    state = fold_recovery(events)
    queue = build_recheck_queue(
        state,
        unstable=[RecheckEntry(axis="장황함", klass="unstable", reason="probe flip")],
        conflicts=[RecheckEntry(axis="응답언어", klass="conflict", reason="수기 룰 충돌")],
    )

    untested = [e for e in queue if e.klass == "untested-prior"]
    assert untested[0].axis == AXIS
    assert untested[0].value == V3
    assert untested[0].reason == "demoted:contradiction"
    assert all(e.reason == "mined-prior" for e in untested[1:])


def test_recheck_class_priority_is_invariant() -> None:
    events = [session_start(SID, "main"), both_strike("frag-l", "frag-r"), left_strike(V3, "frag-last")]
    state = fold_recovery(events)
    queue = build_recheck_queue(
        state,
        unstable=[RecheckEntry(axis="장황함", klass="unstable", reason="probe flip")],
        conflicts=[RecheckEntry(axis="응답언어", klass="conflict", reason="수기 룰 충돌")],
    )

    assert RECHECK_CLASS_PRIORITY == ("unstable", "untested-prior", "conflict")
    priorities = [e.priority for e in queue]
    assert priorities == sorted(priorities)
    assert queue[0].klass == "unstable"
    assert queue[-1].klass == "conflict"


def test_unknown_recheck_class_is_refused() -> None:
    with pytest.raises(RecoveryViolation):
        RecheckEntry(axis=AXIS, klass="approved", reason="x")


# --- fold 순수성 -------------------------------------------------------------


def test_fold_is_deterministic_on_replay() -> None:
    original = left_strike(V1, "frag-1")
    events = [
        session_start(SID, "main"),
        original,
        left_strike(V1, "frag-1"),
        left_strike(V2, "frag-2"),
        session_end(),
        session_start(RID, "recheck"),
        revive(RID, original.event_id),
    ]
    first = fold_recovery(events)
    second = fold_recovery(events)

    assert first.remaining_trace == second.remaining_trace
    assert first.remaining_hypotheses == second.remaining_hypotheses
    assert first.eliminated_pairs == second.eliminated_pairs
    assert first.demotions == second.demotions
    assert first.tombstones == second.tombstones


def test_fold_runs_over_an_append_only_event_log() -> None:
    log = EventLog()
    log.append(session_start(SID, "main"))
    original = log.append(left_strike(V1, "frag-1"))
    log.append(left_strike(V1, "frag-1"))
    log.append(session_end())
    log.append(session_start(RID, "recheck"))
    log.append(revive(RID, original.event_id))

    state = fold_recovery(log.events)

    assert state.remaining_hypotheses == FULL_SPACE
    assert len(state.tombstones) == 1
