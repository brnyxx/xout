"""가설 공간 카운터 fold 테스트.

두 양이 분리되어 있는지, arity가 (b)에만 적용되는지, replay가 결정적인지 본다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from xout.counter import (
    CounterViolation,
    DEFAULT_CATALOG,
    INITIAL_COMBINATIONS,
    TOTAL_PAIRS,
    eliminated_pairs,
    fold,
    remaining_combinations,
)
from xout.events import (
    Event,
    EventLog,
    EventType,
    Refutation,
    StrikeTarget,
    strike,
)

SESSION = "sess-counter"
AUTONOMY = "autonomy"
VERBOSITY = "verbosity"
LANGUAGE = "response_language"


def _ref(axis: str, value: str, side: str = "left") -> Refutation:
    return Refutation(
        axis=axis,
        value=value,
        fragment_id=f"frag-{axis}-{value}",
        side=side,
    )


def _strike(axis, target, refutations=(), pair_id="pair-1", scene_id="scene-1"):
    return strike(
        session_id=SESSION,
        pair_id=pair_id,
        axis=axis,
        scene_id=scene_id,
        target=target,
        refutations=refutations,
    )


def _event(event_type: EventType, **payload) -> Event:
    return Event(type=event_type, session_id=SESSION, payload=dict(payload))


def _wipe_axis(axis: str, keep_last: bool = True):
    values = DEFAULT_CATALOG[axis]
    targets = values if not keep_last else values[:2]
    return [
        _strike(axis, StrikeTarget.LEFT, (_ref(axis, value),), pair_id=f"p-{axis}-{i}")
        for i, value in enumerate(targets)
    ]


def test_catalog_is_eight_axes_by_three_values():
    assert len(DEFAULT_CATALOG) == 8
    assert all(len(v) == 3 for v in DEFAULT_CATALOG.values())
    assert INITIAL_COMBINATIONS == 3**8 == 6561
    assert TOTAL_PAIRS == 24


def test_empty_stream_yields_initial_counters():
    state = fold([])
    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects == ()


def test_first_arity_one_refutation_worked_example():
    event = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    state = fold([event])

    assert state.remaining_combinations == 4374
    assert state.eliminated_pairs == 1

    effect = state.effects[0]
    assert effect.arity == 1
    assert effect.counter_delta == -1
    assert effect.eliminated_delta == 1


def test_arity_is_not_the_product_decrement():
    event = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    state = fold([event])

    product_drop = 6561 - state.remaining_combinations
    assert product_drop == 2187
    assert state.effects[0].arity == 1
    assert product_drop != state.effects[0].arity


def test_both_target_removes_two_pairs_on_one_axis():
    event = _strike(
        AUTONOMY,
        StrikeTarget.BOTH,
        (
            _ref(AUTONOMY, "ask_first", side="left"),
            _ref(AUTONOMY, "act_then_report", side="right"),
        ),
    )
    state = fold([event])

    assert state.remaining_combinations == 3**7
    assert state.eliminated_pairs == 2
    assert state.effects[0].arity == 2
    assert state.axis(AUTONOMY).discrimination == "complete"


def test_both_target_across_two_axes():
    event = _strike(
        AUTONOMY,
        StrikeTarget.BOTH,
        (
            _ref(AUTONOMY, "ask_first", side="left"),
            _ref(VERBOSITY, "explanatory", side="right"),
        ),
    )
    state = fold([event])

    assert state.remaining_combinations == 2916
    assert state.eliminated_pairs == 2


def test_pair_strike_is_arity_zero():
    state = fold([_strike(AUTONOMY, StrikeTarget.PAIR)])

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects[0].arity == 0


def test_duplicate_refutation_is_idempotent():
    first = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    again = _strike(
        AUTONOMY,
        StrikeTarget.LEFT,
        (_ref(AUTONOMY, "ask_first"),),
        pair_id="pair-2",
    )
    state = fold([first, again])

    assert state.remaining_combinations == 4374
    assert state.eliminated_pairs == 1
    assert state.effects[1].arity == 0


def test_last_survivor_strike_is_isolated_to_arity_zero():
    events = _wipe_axis(AUTONOMY)
    last = DEFAULT_CATALOG[AUTONOMY][2]
    events.append(
        _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, last),), pair_id="p-last")
    )
    state = fold(events)

    assert state.remaining_combinations == 3**7
    assert state.eliminated_pairs == 2
    assert state.effects[-1].arity == 0
    assert state.effects[-1].contradiction_axes == (AUTONOMY,)
    assert state.axis(AUTONOMY).contested is True
    assert state.contradiction_axes == (AUTONOMY,)


def test_product_never_collapses_to_zero():
    events = []
    for axis, values in DEFAULT_CATALOG.items():
        for index, value in enumerate(values):
            events.append(
                _strike(
                    axis,
                    StrikeTarget.LEFT,
                    (_ref(axis, value),),
                    pair_id=f"p-{axis}-{index}",
                )
            )
    state = fold(events)

    assert state.remaining_combinations == 1
    assert state.eliminated_pairs == 16
    assert all(len(a.surviving) == 1 for a in state.axes)


def test_undo_tombstone_restores_both_counters():
    struck = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    undo = _event(EventType.UNDO_TOMBSTONE, target_event_id=struck.event_id)
    state = fold([struck, undo])

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects[-1].restored == 1
    assert state.effects[-1].eliminated_delta == -1


def test_repeated_undo_is_idempotent():
    struck = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    undo = _event(EventType.UNDO_TOMBSTONE, target_event_id=struck.event_id)
    again = _event(EventType.UNDO_TOMBSTONE, target_event_id=struck.event_id)
    state = fold([struck, undo, again])

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects[-1].restored == 0


def test_undo_by_explicit_pairs():
    struck = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    undo = _event(EventType.UNDO_TOMBSTONE, pairs=[[AUTONOMY, "ask_first"]])
    state = fold([struck, undo])

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0


def test_revive_raises_product_and_demotes_axis():
    events = _wipe_axis(AUTONOMY)
    events.append(_event(EventType.REVIVE, axis=AUTONOMY, value="ask_first"))
    state = fold(events)

    assert state.remaining_combinations == 4374
    assert state.eliminated_pairs == 1
    assert state.axis(AUTONOMY).revived is True
    assert state.axis(AUTONOMY).discrimination == "partial"
    assert state.axis(AUTONOMY).effective_discrimination == "untested"


def test_contradiction_restores_axis_for_retest():
    events = _wipe_axis(AUTONOMY)
    last = DEFAULT_CATALOG[AUTONOMY][2]
    events.append(
        _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, last),), pair_id="p-last")
    )
    events.append(_event(EventType.CONTRADICTION, axis=AUTONOMY))
    state = fold(events)

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.axis(AUTONOMY).contested is True
    assert state.axis(AUTONOMY).effective_discrimination == "untested"
    assert state.effects[-1].restored == 2


def test_inert_events_do_not_touch_counters():
    events = [
        _event(EventType.SESSION_START, profile="product"),
        _event(EventType.PROBE_SHOWN, pair_id="mirror-1"),
        _event(EventType.PROBE_RESULT, result="flip"),
        _event(EventType.SESSION_VALIDATED),
    ]
    state = fold(events)

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects == ()


def test_probe_events_stay_inert_between_strikes():
    events = [
        _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),)),
        _event(EventType.PROBE_SHOWN, pair_id="mirror-1"),
        _event(EventType.PROBE_RESULT, result="consistent"),
    ]
    state = fold(events)

    assert state.remaining_combinations == 4374
    assert state.eliminated_pairs == 1
    assert len(state.effects) == 1


def test_replay_is_deterministic():
    events = [
        _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),)),
        _strike(
            VERBOSITY,
            StrikeTarget.BOTH,
            (
                _ref(VERBOSITY, "terse", side="left"),
                _ref(VERBOSITY, "explanatory", side="right"),
            ),
            pair_id="pair-2",
        ),
        _event(EventType.PROBE_SHOWN, pair_id="mirror-1"),
        _strike(LANGUAGE, StrikeTarget.PAIR, pair_id="pair-3"),
        _strike(
            AUTONOMY,
            StrikeTarget.RIGHT,
            (_ref(AUTONOMY, "ask_first", side="right"),),
            pair_id="pair-4",
        ),
        _event(EventType.REVIVE, axis=VERBOSITY, value="terse"),
    ]

    first = fold(events)
    second = fold(events)
    third = fold(list(events))

    assert first == second == third
    assert first.remaining_combinations == second.remaining_combinations == 2916
    assert first.eliminated_pairs == second.eliminated_pairs == 2
    assert first.arities == second.arities == (1, 2, 0, 0, 0)


def test_counters_stay_in_declared_ranges_across_long_stream():
    events = []
    for axis, values in DEFAULT_CATALOG.items():
        for index, value in enumerate(values):
            events.append(
                _strike(
                    axis,
                    StrikeTarget.LEFT,
                    (_ref(axis, value),),
                    pair_id=f"p-{axis}-{index}",
                )
            )
            events.append(_strike(axis, StrikeTarget.PAIR, pair_id=f"pp-{axis}-{index}"))

    state = fold(events)
    for effect in state.effects:
        assert 0 <= effect.eliminated_pairs <= TOTAL_PAIRS
        assert effect.remaining_combinations >= 1
        assert effect.arity in (0, 1, 2)


def test_product_decreases_monotonically_under_strike_only():
    events = []
    for axis, values in DEFAULT_CATALOG.items():
        events.append(
            _strike(
                axis,
                StrikeTarget.LEFT,
                (_ref(axis, values[0]),),
                pair_id=f"p-{axis}",
            )
        )
    state = fold(events)

    products = [e.remaining_combinations for e in state.effects]
    assert products == sorted(products, reverse=True)
    assert products[-1] == 2**8
    assert state.eliminated_pairs == 8


def test_unknown_axis_is_rejected():
    bogus = _strike("not_an_axis", StrikeTarget.LEFT, (_ref("not_an_axis", "x"),))
    with pytest.raises(CounterViolation):
        fold([bogus])


def test_unknown_value_is_rejected():
    bogus = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "not_a_value"),))
    with pytest.raises(CounterViolation):
        fold([bogus])


def test_skeleton_span_contributes_arity_zero():
    @dataclass(frozen=True, slots=True)
    class SkeletonRefutation(Refutation):
        span_kind: str = "skeleton"

    skeleton = SkeletonRefutation(
        axis=AUTONOMY,
        value="ask_first",
        fragment_id="frag-skeleton",
        side="left",
    )
    state = fold([_strike(AUTONOMY, StrikeTarget.LEFT, (skeleton,))])

    assert state.remaining_combinations == 6561
    assert state.eliminated_pairs == 0
    assert state.effects[0].arity == 0


def test_fold_consumes_event_log_container():
    log = EventLog()
    log.append(_strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),)))
    log.append(_event(EventType.PROBE_SHOWN, pair_id="mirror-1"))
    log.append(
        _strike(
            VERBOSITY,
            StrikeTarget.LEFT,
            (_ref(VERBOSITY, "terse"),),
            pair_id="pair-2",
        )
    )

    state = fold(log)
    assert state.remaining_combinations == 2916
    assert state.eliminated_pairs == 2


def test_event_log_counter_delta_is_the_additive_quantity_only():
    log = EventLog()
    first = _strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))
    log.append(first)
    log.append(
        _strike(
            AUTONOMY,
            StrikeTarget.LEFT,
            (_ref(AUTONOMY, "ask_first"),),
            pair_id="pair-2",
        )
    )

    state = fold(log)
    assert log.counter_delta() == -2
    assert state.eliminated_pairs == 1
    assert state.remaining_combinations == 4374


def test_axis_state_discrimination_ladder():
    state = fold([])
    assert state.axis(AUTONOMY).discrimination == "untested"

    state = fold([_strike(AUTONOMY, StrikeTarget.LEFT, (_ref(AUTONOMY, "ask_first"),))])
    assert state.axis(AUTONOMY).discrimination == "partial"

    state = fold(_wipe_axis(AUTONOMY))
    assert state.axis(AUTONOMY).discrimination == "complete"
    assert state.axis(AUTONOMY).surviving == (DEFAULT_CATALOG[AUTONOMY][2],)
    assert state.axis(AUTONOMY).eliminated == DEFAULT_CATALOG[AUTONOMY][:2]


def test_convenience_helpers_match_fold():
    events = _wipe_axis(AUTONOMY)
    assert remaining_combinations(events) == 3**7
    assert eliminated_pairs(events) == 2


def test_custom_catalog_is_supported():
    catalog = {"a": ("x", "y", "z"), "b": ("x", "y", "z")}
    event = _strike("a", StrikeTarget.LEFT, (_ref("a", "x"),))
    state = fold([event], catalog)

    assert state.remaining_combinations == 6
    assert state.eliminated_pairs == 1


def test_unknown_axis_lookup_on_state_raises():
    state = fold([])
    with pytest.raises(CounterViolation):
        state.axis("nope")
