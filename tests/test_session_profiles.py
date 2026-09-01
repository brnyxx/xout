"""AC9 - 세션 프로파일이 봉인 수치대로 강제된다.

- product: cap 15 + 프로브 0개. 프로브 이벤트 등장 시 판정 fold가 스트림 무효 처리.
- validation: cap 15 = 판별 13 + 슬롯 9/13의 미러 프로브 2개(결정론적 선정,
  컴파일 불활성, terminal).
- 완전 판별 통과 축 < 5이면 session_voided(reason=axis_shortfall) 방출.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xout.counter import DEFAULT_CATALOG, fold as fold_counter
from xout.events import Event, EventType, Refutation, StrikeEvent, StrikeTarget, strike
from xout.session import (
    PROFILE_PRODUCT,
    PROFILE_VALIDATION,
    REASON_PROBE_IN_PRODUCT,
    REASON_PROBE_MISSING,
    REASON_PROBE_PAIR_MISMATCH,
    REASON_PROBE_REDRAWN,
    REASON_PROBE_RESULT_INVALID,
    REASON_SLOT_OVERRUN,
    SessionSpec,
    SessionViolation,
    VOID_REASON_AXIS_SHORTFALL,
    fold_session,
    load_session_specs,
    probe_result,
    probe_shown,
    select_probe_pairs,
)

SESSION = "sess-1"
AXES = list(DEFAULT_CATALOG)

PRODUCT_SPEC = SessionSpec(
    profile=PROFILE_PRODUCT,
    discriminative_slots=15,
    probe_slots=(),
    required_full_axes=5,
)
VALIDATION_SPEC = SessionSpec(
    profile=PROFILE_VALIDATION,
    discriminative_slots=13,
    probe_slots=(9, 13),
    required_full_axes=5,
)
SPECS = {PROFILE_PRODUCT: PRODUCT_SPEC, PROFILE_VALIDATION: VALIDATION_SPEC}


def _session_start(profile: str) -> Event:
    return Event(
        type=EventType.SESSION_START,
        session_id=SESSION,
        payload={"profile": profile},
    )


def _both(axis: str, pair_id: str) -> StrikeEvent:
    values = DEFAULT_CATALOG[axis]
    return strike(
        session_id=SESSION,
        pair_id=pair_id,
        axis=axis,
        scene_id="scene-1",
        target=StrikeTarget.BOTH,
        refutations=(
            Refutation(axis=axis, value=values[0], fragment_id=f"{pair_id}:L", side="left"),
            Refutation(axis=axis, value=values[1], fragment_id=f"{pair_id}:R", side="right"),
        ),
    )


def _pair(axis: str, pair_id: str) -> StrikeEvent:
    return strike(
        session_id=SESSION,
        pair_id=pair_id,
        axis=axis,
        scene_id="scene-1",
        target=StrikeTarget.PAIR,
    )


def _strikes(count: int, complete_axes: int) -> list:
    """앞쪽 complete_axes개는 both(축 완전 판별), 나머지는 pair(무판별)."""
    out = []
    for i in range(count):
        if i < complete_axes:
            out.append(_both(AXES[i], f"pair-{i}"))
        else:
            out.append(_pair(AXES[i % len(AXES)], f"pair-{i}"))
    return out


def _product_stream(complete_axes: int = 5, total: int = 15) -> list:
    return [_session_start(PROFILE_PRODUCT), *_strikes(total, complete_axes)]


def _validation_stream(complete_axes: int = 5, probe9: str = "pair-0", probe13: str = "pair-1") -> list:
    """슬롯 1-8 스트라이크, 9 프로브, 10-12 스트라이크, 13 프로브, 14-15 스트라이크."""
    s = _strikes(13, complete_axes)
    return [
        _session_start(PROFILE_VALIDATION),
        *s[0:8],
        probe_shown(SESSION, 9, probe9),
        probe_result(SESSION, 9, probe9, "consistent"),
        *s[8:11],
        probe_shown(SESSION, 13, probe13),
        probe_result(SESSION, 13, probe13, "flip"),
        *s[11:13],
    ]


# ---------------------------------------------------------------- 봉인 수치 로드


class TestLoadSessionSpecs:
    def test_reads_sealed_document_numbers(self):
        specs = load_session_specs()
        product = specs[PROFILE_PRODUCT]
        validation = specs[PROFILE_VALIDATION]
        assert product.discriminative_slots == 15
        assert product.probe_slots == ()
        assert product.total_slots == 15
        assert validation.discriminative_slots == 13
        assert validation.probe_slots == (9, 13)
        assert validation.total_slots == 15
        assert product.required_full_axes == 5
        assert validation.required_full_axes == 5

    def test_specs_mapping_is_readonly(self):
        specs = load_session_specs()
        with pytest.raises(TypeError):
            specs["hacked"] = PRODUCT_SPEC

    def test_honors_injected_document(self, tmp_path: Path):
        doc = {
            "document": {
                "session_slot_layout": {
                    "product": {"discriminative_slots": 7, "probe_slots": []},
                    "validation": {"discriminative_slots": 3, "probe_slots": [2, 4]},
                    "recheck": {"discriminative_slots_min": 5, "probe_slots": []},
                },
                "frozen_parameters": {
                    "custom_axis_requirement": {"value": 2, "unit": "axes"}
                },
            }
        }
        path = tmp_path / "prereg.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        specs = load_session_specs(path)
        assert specs["product"].discriminative_slots == 7
        assert specs["validation"].probe_slots == (2, 4)
        assert specs["validation"].total_slots == 5
        assert specs["validation"].required_full_axes == 2
        assert "recheck" not in specs

    def test_missing_file_falls_back_to_sealed_defaults(self, tmp_path: Path):
        specs = load_session_specs(tmp_path / "absent.json")
        assert specs[PROFILE_PRODUCT].total_slots == 15
        assert specs[PROFILE_VALIDATION].probe_slots == (9, 13)
        assert specs[PROFILE_VALIDATION].required_full_axes == 5


# ---------------------------------------------------------------- product 프로파일


class TestProductProfile:
    def test_cap_15_enforced(self):
        judgment = fold_session(_product_stream(), specs=SPECS)
        assert judgment.profile == PROFILE_PRODUCT
        assert judgment.slots_used == 15
        assert judgment.complete is True
        assert judgment.stream_valid is True
        assert judgment.probes == ()
        assert judgment.voided is None

    def test_16th_strike_is_overrun(self):
        judgment = fold_session(_product_stream(total=16), specs=SPECS)
        assert REASON_SLOT_OVERRUN in judgment.reasons
        assert judgment.complete is False
        assert judgment.stream_valid is False

    def test_probe_event_voids_product_stream(self):
        stream = _product_stream()
        stream.insert(5, probe_shown(SESSION, 5, "pair-0"))
        judgment = fold_session(stream, specs=SPECS)
        assert REASON_PROBE_IN_PRODUCT in judgment.reasons
        assert judgment.stream_valid is False

    def test_probe_result_alone_also_voids_product_stream(self):
        stream = _product_stream()
        stream.append(
            Event(
                type=EventType.PROBE_RESULT,
                session_id=SESSION,
                payload={"result": "flip"},
            )
        )
        judgment = fold_session(stream, specs=SPECS)
        assert REASON_PROBE_IN_PRODUCT in judgment.reasons
        assert judgment.stream_valid is False


# ---------------------------------------------------------------- validation 프로파일


class TestValidationProfile:
    def test_full_session_is_valid_and_complete(self):
        judgment = fold_session(_validation_stream(), specs=SPECS)
        assert judgment.stream_valid is True
        assert judgment.complete is True
        assert judgment.slots_used == 15
        assert [p.position for p in judgment.probes] == [9, 13]
        assert all(p.mirrored for p in judgment.probes)
        assert all(p.matches_selection for p in judgment.probes)
        assert all(p.resolved for p in judgment.probes)
        assert judgment.voided is None

    def test_probes_missing_invalidates(self):
        # 프로브 없이 판별 스트라이크 15개만으로 채운 validation 스트림.
        stream = [_session_start(PROFILE_VALIDATION), *_strikes(15, 5)]
        judgment = fold_session(stream, specs=SPECS)
        assert REASON_PROBE_MISSING in judgment.reasons
        assert judgment.stream_valid is False

    def test_probe_pair_mismatch_flagged(self):
        judgment = fold_session(
            _validation_stream(probe9="pair-7"), specs=SPECS
        )
        assert REASON_PROBE_PAIR_MISMATCH in judgment.reasons
        assert judgment.probes[0].matches_selection is False

    def test_probe_records_original_pair_and_mirror_flag(self):
        judgment = fold_session(_validation_stream(), specs=SPECS)
        first = judgment.probes[0]
        assert first.pair_id == "pair-0"
        assert first.expected_pair_id == "pair-0"
        assert first.mirrored is True

    def test_invalid_probe_result_value_flagged(self):
        stream = _validation_stream()
        # 슬롯 9의 정상 결과를 허용 밖 값으로 교체.
        stream[10] = Event(
            type=EventType.PROBE_RESULT,
            session_id=SESSION,
            payload={"slot": 9, "pair_id": "pair-0", "result": "maybe"},
        )
        judgment = fold_session(stream, specs=SPECS)
        assert REASON_PROBE_RESULT_INVALID in judgment.reasons

    def test_probe_result_builder_rejects_out_of_domain(self):
        with pytest.raises(SessionViolation):
            probe_result(SESSION, 9, "pair-0", "approve")


# ---------------------------------------------------------------- 결정론적 프로브 선정


class TestDeterministicProbeSelection:
    def test_same_prefix_same_selection(self):
        stream = _validation_stream()
        first = dict(select_probe_pairs(stream, VALIDATION_SPEC))
        second = dict(select_probe_pairs(stream, VALIDATION_SPEC))
        assert first == second == {9: "pair-0", 13: "pair-1"}

    def test_rebuilt_equal_prefix_selects_same_pairs(self):
        # 이벤트 객체를 새로 만들어도(uuid 상이) 같은 prefix면 같은 선정.
        first = dict(select_probe_pairs(_validation_stream(), VALIDATION_SPEC))
        second = dict(select_probe_pairs(_validation_stream(), VALIDATION_SPEC))
        assert first == second

    def test_selection_uses_first_two_first_half_discriminative_pairs(self):
        # 전반부: pair(무판별) 2개 뒤에 both(판별) 2개 배치.
        strikes = [
            _pair(AXES[0], "p-0"),
            _pair(AXES[1], "p-1"),
            _both(AXES[2], "p-2"),
            _both(AXES[3], "p-3"),
            *[_pair(AXES[i % 8], f"p-{i}") for i in range(4, 13)],
        ]
        stream = [_session_start(PROFILE_VALIDATION), *strikes[0:8]]
        chosen = select_probe_pairs(stream, VALIDATION_SPEC)
        assert chosen[9] == "p-2"
        assert chosen[13] == "p-3"

    def test_first_half_shortfall_fills_from_immediately_preceding_pair(self):
        # 전반부 판별쌍 1개뿐 - 슬롯 13은 직전 판별쌍(슬롯 12)의 미러로 채운다.
        strikes = [
            _both(AXES[0], "d-0"),
            *[_pair(AXES[i % 8], f"p-{i}") for i in range(1, 8)],
            _pair(AXES[0], "p-8"),
            _pair(AXES[1], "p-9"),
            _both(AXES[1], "d-1"),
        ]
        stream = [
            _session_start(PROFILE_VALIDATION),
            *strikes[0:8],
            probe_shown(SESSION, 9, "d-0"),
            probe_result(SESSION, 9, "d-0", "consistent"),
            *strikes[8:11],
        ]
        chosen = select_probe_pairs(stream, VALIDATION_SPEC)
        assert chosen[9] == "d-0"
        assert chosen[13] == "d-1"

    def test_fold_replay_is_deterministic(self):
        stream = _validation_stream()
        assert fold_session(stream, specs=SPECS) == fold_session(stream, specs=SPECS)


# ---------------------------------------------------------------- terminal + 컴파일 불활성


class TestProbeTerminalAndInert:
    def test_probe_redraw_attempt_invalidates(self):
        stream = _validation_stream()
        stream.append(probe_shown(SESSION, 9, "pair-0"))
        stream.append(probe_result(SESSION, 9, "pair-0", "consistent"))
        judgment = fold_session(stream, specs=SPECS)
        assert REASON_PROBE_REDRAWN in judgment.reasons
        assert judgment.stream_valid is False

    def test_probe_events_inert_in_compile_fold(self):
        with_probes = _validation_stream()
        without_probes = [
            e
            for e in with_probes
            if getattr(e, "type", None)
            not in (EventType.PROBE_SHOWN, EventType.PROBE_RESULT)
        ]
        folded_with = fold_counter(with_probes)
        folded_without = fold_counter(without_probes)
        assert folded_with.remaining_combinations == folded_without.remaining_combinations
        assert folded_with.eliminated_pairs == folded_without.eliminated_pairs
        assert folded_with.axes == folded_without.axes

    def test_probe_events_inert_in_axis_discrimination(self):
        judgment = fold_session(_validation_stream(), specs=SPECS)
        assert sorted(judgment.fully_discriminated_axes) == sorted(AXES[0:5])


# ---------------------------------------------------------------- 축 하한 미달 방출


class TestAxisShortfallVoiding:
    def test_four_full_axes_emits_session_voided(self):
        judgment = fold_session(
            _validation_stream(complete_axes=4), specs=SPECS
        )
        assert judgment.complete is True
        assert judgment.stream_valid is True
        assert len(judgment.fully_discriminated_axes) == 4
        assert judgment.voided is not None
        assert judgment.voided.reason == VOID_REASON_AXIS_SHORTFALL
        event = judgment.voided.to_event()
        assert event.type is EventType.SESSION_VOIDED
        assert event.payload["reason"] == "axis_shortfall"
        assert event.session_id == SESSION

    def test_no_auto_extension_on_shortfall(self):
        # 방출 후에도 슬롯 총량은 15 그대로 - 연장 슬롯이 생기지 않는다.
        judgment = fold_session(
            _validation_stream(complete_axes=4), specs=SPECS
        )
        assert judgment.slots_used == 15
        extended = _validation_stream(complete_axes=4) + [_pair(AXES[7], "extra")]
        overrun = fold_session(extended, specs=SPECS)
        assert REASON_SLOT_OVERRUN in overrun.reasons

    def test_five_full_axes_not_voided(self):
        judgment = fold_session(
            _validation_stream(complete_axes=5), specs=SPECS
        )
        assert judgment.voided is None

    def test_shortfall_applies_to_product_sessions_too(self):
        judgment = fold_session(_product_stream(complete_axes=4), specs=SPECS)
        assert judgment.voided is not None
        assert judgment.voided.reason == VOID_REASON_AXIS_SHORTFALL

    def test_injected_spec_controls_the_requirement(self, tmp_path: Path):
        # 수치가 하드코딩돼 있지 않음을 주입으로 확인: 하한 4면 통과축 4개는 유효.
        doc = {
            "document": {
                "session_slot_layout": {
                    "product": {"discriminative_slots": 15, "probe_slots": []},
                    "validation": {"discriminative_slots": 13, "probe_slots": [9, 13]},
                },
                "frozen_parameters": {"req": {"value": 4, "unit": "axes"}},
            }
        }
        path = tmp_path / "prereg.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        specs = load_session_specs(path)
        judgment = fold_session(_validation_stream(complete_axes=4), specs=specs)
        assert judgment.voided is None
