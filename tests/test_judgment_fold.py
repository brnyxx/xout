"""AC11 - 도구 자기반증 판정이 append-only 이벤트에 대한 순수 fold로만 재현되는가."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xout.events import Event, EventType
from xout.judgment import (
    CATALOG_REVISED_VERSION,
    JudgmentViolation,
    acknowledge,
    emit_condition_met,
    fold_judgment,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PREREG_PATH = REPO_ROOT / "docs" / "prereg" / "prereg_sealed.json"
PAYLOAD = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
DOC = PAYLOAD["document"]

# 판정 영향 수치는 봉인 문서에서만 온다 - 테스트는 그 값을 검증하는 코드다.
N_SESSIONS = DOC["frozen_parameters"]["validation_session_count_n_val"]["value"]
N_INSTANCES = DOC["frozen_parameters"]["cumulative_discriminative_instance_floor"]["value"]

SID = "session-judgment"


def seal(event_id: str = "seal-1", **overrides) -> Event:
    payload = {
        "catalog_version": DOC["catalog_version"],
        "digest": PAYLOAD["seal"]["digest"],
        "required_valid_sessions": N_SESSIONS,
        "required_discriminative_instances": N_INSTANCES,
    }
    payload.update(overrides)
    return Event(
        type=EventType.PREREG_SEALED, session_id=SID, payload=payload, event_id=event_id
    )


def validated(
    instances: int, correct: int, mis: int, event_id: str = "val-1"
) -> Event:
    return Event(
        type=EventType.SESSION_VALIDATED,
        session_id=SID,
        payload={
            "discriminative_instances": instances,
            "correct_restorations": correct,
            "mis_restorations": mis,
        },
        event_id=event_id,
    )


def voided(instances: int, correct: int, mis: int, event_id: str = "void-1") -> Event:
    return Event(
        type=EventType.SESSION_VOIDED,
        session_id=SID,
        payload={
            "reason": "axis_shortfall",
            "discriminative_instances": instances,
            "correct_restorations": correct,
            "mis_restorations": mis,
        },
        event_id=event_id,
    )


def revision(crosswalk: str | None, event_id: str = "rev-1") -> Event:
    payload: dict = {"to_version": CATALOG_REVISED_VERSION}
    if crosswalk is not None:
        payload["crosswalk"] = crosswalk
    return Event(
        type=EventType.CATALOG_REVISION_CONSUMED,
        session_id=SID,
        payload=payload,
        event_id=event_id,
    )


def condition_met_event(event_id: str = "met-1") -> Event:
    return Event(
        type=EventType.REFUTATION_CONDITION_MET,
        session_id=SID,
        payload={},
        event_id=event_id,
    )


def ack(event_id: str = "ack-1", actor: str = "human-operator") -> Event:
    return Event(
        type=EventType.REFUTATION_ACKNOWLEDGED,
        session_id=SID,
        payload={"actor": actor},
        event_id=event_id,
    )


def refuting_stream() -> list[Event]:
    """세션 2개 - 인스턴스 합 6, 오복원 4 >= 정복원 2로 조건이 성립하는 증거."""
    return [
        seal(),
        validated(3, 1, 2, event_id="val-1"),
        validated(3, 1, 2, event_id="val-2"),
    ]


# --- 조건 미충족 -------------------------------------------------------------


def test_condition_not_met_with_single_session() -> None:
    state = fold_judgment([seal(), validated(6, 2, 4)])
    assert state.valid_sessions == 1
    assert state.discriminative_instances == 6
    assert state.mis_restorations >= state.correct_restorations
    assert not state.condition_met
    assert not state.core_refutation_confirmed


def test_condition_not_met_below_instance_requirement() -> None:
    state = fold_judgment(
        [seal(), validated(3, 1, 2, event_id="val-1"), validated(2, 0, 2, event_id="val-2")]
    )
    assert state.valid_sessions == 2
    assert state.discriminative_instances == 5
    assert not state.condition_met


def test_condition_not_met_when_correct_exceeds_mis() -> None:
    state = fold_judgment(
        [seal(), validated(3, 2, 1, event_id="val-1"), validated(3, 2, 1, event_id="val-2")]
    )
    assert state.correct_restorations > state.mis_restorations
    assert not state.condition_met


def test_no_evidence_admitted_before_seal() -> None:
    state = fold_judgment([validated(6, 2, 4), seal()])
    assert state.valid_sessions == 0
    assert state.discriminative_instances == 0
    assert not state.condition_met
    assert any("봉인 이전" in r.reason for r in state.rejected)


# --- 조건 충족과 인간 게이트 -------------------------------------------------


def test_condition_met_when_all_requirements_hold() -> None:
    state = fold_judgment(refuting_stream())
    assert state.catalog_frozen
    assert state.basis is not None
    assert state.basis.required_valid_sessions == N_SESSIONS
    assert state.basis.required_discriminative_instances == N_INSTANCES
    assert state.condition_met


def test_condition_met_on_tie_between_mis_and_correct() -> None:
    # 동률 포함 - 오복원 >= 정복원.
    state = fold_judgment(
        [seal(), validated(3, 1, 1, event_id="val-1"), validated(3, 1, 1, event_id="val-2")]
    )
    assert state.mis_restorations == state.correct_restorations
    assert state.condition_met


def test_missing_restoration_evidence_does_not_form_a_zero_zero_tie() -> None:
    state = fold_judgment(
        [
            seal(),
            validated(3, 0, 0, event_id="val-1"),
            validated(3, 0, 0, event_id="val-2"),
        ]
    )
    assert not state.condition_met


def test_confirmation_requires_human_acknowledged() -> None:
    events = refuting_stream()
    state = fold_judgment(events)
    assert state.condition_met
    assert not state.acknowledged
    assert not state.core_refutation_confirmed

    # 기계 방출 이벤트가 스트림에 실려도 인간 게이트 없이는 확정이 아니다.
    with_machine = events + [condition_met_event()]
    state = fold_judgment(with_machine)
    assert state.supported_condition_events == ("met-1",)
    assert not state.core_refutation_confirmed

    acked = with_machine + [ack("ack-1")]
    state = fold_judgment(acked)
    assert state.acknowledged_event_ids == ("ack-1",)
    assert state.core_refutation_confirmed


def test_ack_before_condition_is_ineffective() -> None:
    events = [seal(), ack("ack-early")]
    events += [validated(3, 1, 2, event_id="val-1"), validated(3, 1, 2, event_id="val-2")]
    state = fold_judgment(events)
    assert state.condition_met
    assert state.acknowledged_event_ids == ()
    assert not state.core_refutation_confirmed
    assert any(r.event_id == "ack-early" and "효력 없음" in r.reason for r in state.rejected)


def test_machine_emission_alone_creates_no_condition() -> None:
    state = fold_judgment([seal(), validated(3, 1, 2), condition_met_event()])
    assert not state.condition_met
    assert state.supported_condition_events == ()
    assert any(
        r.event_id == "met-1" and r.event_type == "refutation_condition_met"
        for r in state.rejected
    )


def test_emit_condition_met_is_gated_by_derived_state() -> None:
    unmet = fold_judgment([seal(), validated(3, 1, 2)])
    assert emit_condition_met(unmet, SID) is None

    met = fold_judgment(refuting_stream())
    event = emit_condition_met(met, SID)
    assert event is not None
    assert event.type is EventType.REFUTATION_CONDITION_MET
    assert event.payload["mis_restorations"] >= event.payload["correct_restorations"]


def test_acknowledge_requires_actor() -> None:
    with pytest.raises(JudgmentViolation):
        acknowledge(SID, actor="")

    event = acknowledge(SID, actor="human-operator")
    assert event.type is EventType.REFUTATION_ACKNOWLEDGED
    assert event.payload["actor"] == "human-operator"


# --- 카탈로그 개정 -----------------------------------------------------------


def test_second_revision_rejected() -> None:
    events = refuting_stream() + [
        revision("a" * 64, event_id="rev-1"),
        revision("b" * 64, event_id="rev-2"),
    ]
    state = fold_judgment(events)
    assert state.revision is not None
    assert state.revision.event_id == "rev-1"
    assert state.revision.crosswalk == "a" * 64
    assert any(r.event_id == "rev-2" and "두 번째" in r.reason for r in state.rejected)


def test_revision_without_crosswalk_rejected() -> None:
    state = fold_judgment(refuting_stream() + [revision(None, event_id="rev-none")])
    assert state.revision is None
    assert state.catalog_version == DOC["catalog_version"]
    assert any(r.event_id == "rev-none" and "crosswalk" in r.reason for r in state.rejected)

    # 빈 문자열 해시도 미동봉과 같다 - 거부된 개정은 소진 한도를 쓰지 않는다.
    state = fold_judgment(
        refuting_stream()
        + [revision("", event_id="rev-empty"), revision("a" * 64, event_id="rev-ok")]
    )
    assert state.revision is not None
    assert state.revision.event_id == "rev-ok"


def test_revision_carries_counters_over_without_reset() -> None:
    events = [
        seal(),
        validated(3, 1, 2, event_id="val-1"),
        revision("a" * 64),
        validated(3, 1, 2, event_id="val-2"),
    ]
    state = fold_judgment(events)
    assert state.revision_consumed
    assert state.catalog_version == CATALOG_REVISED_VERSION
    # 개정 전 집계(세션 1, 인스턴스 3)가 승계되어 개정 후 합산으로 조건이 성립한다.
    assert state.valid_sessions == 2
    assert state.discriminative_instances == 6
    assert state.condition_met


# --- VOID 세션 불산입 --------------------------------------------------------


def test_void_session_instances_count_for_neither_side() -> None:
    events = [seal(), validated(3, 1, 2), voided(3, 1, 2)]
    state = fold_judgment(events)
    assert state.valid_sessions == 1
    assert state.voided_sessions == 1
    assert state.discriminative_instances == 3
    assert state.correct_restorations == 1
    assert state.mis_restorations == 2
    assert not state.condition_met

    # VOID 세션만으로는 어떤 판정도 성립하지 않는다.
    only_void = fold_judgment([seal(), voided(6, 1, 5, event_id="void-a")])
    assert only_void.discriminative_instances == 0
    assert not only_void.condition_met


def test_non_validation_profiles_do_not_count_as_validation_evidence(caplog) -> None:
    caplog.set_level("WARNING", logger="xout.judgment")
    product = validated(N_INSTANCES, 0, 0, event_id="product")
    product.payload["profile"] = "product"
    recheck = validated(N_INSTANCES, 0, 0, event_id="recheck")
    recheck.payload["profile"] = "recheck"

    state = fold_judgment([seal(), product, recheck])

    assert state.valid_sessions == 0
    assert state.discriminative_instances == 0
    assert len(state.rejected) == 2
    assert not caplog.records


# --- 저장 금지와 replay 결정성 ----------------------------------------------


def full_stream() -> list[Event]:
    return (
        refuting_stream()
        + [revision("a" * 64)]
        + [condition_met_event(), ack("ack-1")]
    )


def test_replay_is_deterministic() -> None:
    events = full_stream()
    first = fold_judgment(events)
    second = fold_judgment(list(events))
    assert first == second
    assert first.core_refutation_confirmed
    assert second.core_refutation_confirmed


def test_independent_rebuilds_fold_to_the_same_state() -> None:
    # 저장된 판정 상태가 없다 - 동일 내용의 스트림을 새로 만들어도 결과가 같다.
    assert fold_judgment(full_stream()) == fold_judgment(full_stream())


def test_reseal_is_rejected_and_first_basis_kept() -> None:
    state = fold_judgment(
        [seal(event_id="seal-1"), seal(event_id="seal-2", required_valid_sessions=1)]
    )
    assert state.basis is not None
    assert state.basis.event_id == "seal-1"
    assert any(r.event_id == "seal-2" and "재봉인" in r.reason for r in state.rejected)
