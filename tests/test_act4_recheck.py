"""AC13 - 4막 경량 재심: 7일 경과 배너, 수동 진입, 큐 전순서, 5-7긋기 예산, 부활값 강등."""

from __future__ import annotations

import ast
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

import xout.recheck as recheck_module
from xout.compiler import (
    MANIFEST_JSON,
    XOUT_MD,
    SETTINGS_JSON,
    build_manifest,
    compile_rules,
    manifest_self_hash,
    render_xout_md,
    render_settings,
)
from xout.conflict import (
    CompiledRule as ConflictCompiledRule,
    ConflictEntry,
    ManualRule,
    conflict_id,
)
from xout.events import Event, EventLog, EventType, Refutation, StrikeTarget, strike
from xout.recovery import (
    DEFAULT_AXIS_CATALOG,
    RECHECK_CLASS_PRIORITY,
    RecoveryChannel,
    fold_recovery,
    revive,
)
from xout.recheck import (
    DEFAULT_BUDGET,
    DUE_DAYS,
    MAX_BUDGET,
    MIN_BUDGET,
    RECHECK_SESSION_KIND,
    RecheckViolation,
    check_due,
    order_queue,
    plan_recheck_session,
    refresh_last_review,
    revived_demotions,
)
from xout.session import PROFILE_PRODUCT, PROFILE_RECHECK

SID = "sess-main"
RID = "sess-recheck"
LAST_REVIEW = "2026-08-01T00:00:00+00:00"
NOW_DUE = "2026-08-08T00:00:00+00:00"          # 정확히 7일 경과
NOW_NOT_DUE = "2026-08-07T23:00:00+00:00"      # 7일 미달

AXIS = "자율성"
V1, V2, V3 = DEFAULT_AXIS_CATALOG[AXIS]
FULL_SPACE = 3 ** 8


def _flip_probe(axis: str) -> Event:
    return Event(
        type=EventType.PROBE_RESULT,
        session_id=SID,
        payload={"slot": 1, "pair_id": f"{axis}-pair", "result": "flip", "axis": axis},
    )


def _conflict_row() -> dict:
    manual = ManualRule(rule_id="rule-9", axis="response_language", value="english", text="영어로 답한다")
    compiled = ConflictCompiledRule(
        rule_id="v1:response_language:korean",
        axis="response_language",
        value="korean",
        text="한국어로 답한다",
        corroboration_grade="untested",
        value_source="mined-prior",
    )
    entry = ConflictEntry(
        conflict_id=conflict_id("response_language", "rule-9", "v1"),
        axis="response_language",
        catalog_version="v1",
        manual=manual,
        compiled=compiled,
    )
    return entry.to_recheck_entry()


@pytest.fixture()
def manifest() -> dict:
    """실제 컴파일러 경로로 만든 manifest - 불안정 1 + untested-prior 7 + 충돌 1."""
    rules = compile_rules((_flip_probe("verbosity"),))
    documents = {XOUT_MD: render_xout_md(rules), SETTINGS_JSON: render_settings(rules)}
    return build_manifest(
        rules,
        documents=documents,
        session_id=SID,
        now=LAST_REVIEW,
        conflicts=(_conflict_row(),),
    )


def left_strike(value: str, fragment: str, pair_id: str = "pair-1"):
    return strike(
        SID,
        pair_id,
        AXIS,
        "scene-1",
        StrikeTarget.LEFT,
        (Refutation(axis=AXIS, value=value, fragment_id=fragment, side="left"),),
    )


def main_session_start() -> Event:
    return Event(
        type=EventType.SESSION_START,
        session_id=SID,
        payload={"session_kind": "main", "profile": PROFILE_PRODUCT},
    )


def session_end() -> Event:
    return Event(type=EventType.SESSION_VALIDATED, session_id=SID, payload={})


# --- 7일 경과 배너 ------------------------------------------------------------


def test_banner_shows_pending_count_after_seven_days(manifest: dict) -> None:
    banner = check_due(manifest, NOW_DUE)

    assert DUE_DAYS == 7
    assert banner.due is True
    assert banner.days_elapsed == pytest.approx(7.0)
    assert banner.pending == 9
    assert banner.text == "재심 대기 9건"
    assert banner.last_review == LAST_REVIEW


def test_banner_absent_before_seven_days(manifest: dict) -> None:
    banner = check_due(manifest, NOW_NOT_DUE)

    assert banner.due is False
    assert banner.text is None
    assert banner.pending == 9  # 대기 건수 계산은 배너 여부와 무관하다


def test_banner_absent_when_queue_is_empty_even_if_due(manifest: dict) -> None:
    empty = dict(manifest, recheck_queue=[])
    banner = check_due(empty, NOW_DUE)

    assert banner.due is True
    assert banner.pending == 0
    assert banner.text is None


def test_missing_last_review_never_raises_a_banner(manifest: dict) -> None:
    headless = {k: v for k, v in manifest.items() if k != "last_review"}
    banner = check_due(headless, NOW_DUE)

    assert banner.due is False
    assert banner.days_elapsed is None
    assert banner.text is None


# --- 수동 진입 경로 -----------------------------------------------------------


def test_manual_entry_is_open_regardless_of_banner(manifest: dict) -> None:
    assert check_due(manifest, NOW_NOT_DUE).due is False

    plan = plan_recheck_session(manifest, RID)

    assert plan.session_id == RID
    assert plan.session_kind == RECHECK_SESSION_KIND
    assert plan.budget == DEFAULT_BUDGET == 5
    assert len(plan.targets) == 5
    assert plan.pending_total == 9


def test_mini_session_reuses_act1_pair_ui_schema(manifest: dict) -> None:
    plan = plan_recheck_session(manifest, RID)

    assert isinstance(plan.opening, Event)
    assert plan.opening.type is EventType.SESSION_START
    assert plan.opening.payload["session_kind"] == "recheck"
    assert plan.opening.payload["profile"] == PROFILE_RECHECK
    assert plan.opening.payload["recheck_budget"] == DEFAULT_BUDGET
    assert plan.opening.payload["recheck_axes"]
    # 신규 UI 기계장치 없음 - recheck 전용 이벤트 타입은 존재하지 않는다.
    assert all("recheck" not in member.value for member in EventType)


# --- 큐 우선순위 전순서 -------------------------------------------------------


def test_queue_total_order_unstable_then_untested_then_conflict(manifest: dict) -> None:
    shuffled = list(reversed(manifest["recheck_queue"]))
    ordered = order_queue(shuffled)

    assert RECHECK_CLASS_PRIORITY == ("unstable", "untested-prior", "conflict")
    klasses = [t.klass for t in ordered]
    assert klasses[0] == "unstable"
    assert klasses[-1] == "conflict"
    priorities = [t.priority for t in ordered]
    assert priorities == sorted(priorities)
    assert klasses.count("unstable") == 1
    assert klasses.count("untested-prior") == 7
    assert klasses.count("conflict") == 1
    # 클래스 안에서는 manifest가 매긴 order를 유지한다.
    untested = [t for t in ordered if t.klass == "untested-prior"]
    assert [t.source_order for t in untested] == sorted(t.source_order for t in untested)
    assert ordered[0].axis == "verbosity"
    assert ordered[-1].conflict_id == "response_language::rule-9::v1"


def test_plan_targets_are_the_head_of_the_ordered_queue(manifest: dict) -> None:
    plan = plan_recheck_session(manifest, RID, budget=7)

    assert plan.targets == order_queue(manifest["recheck_queue"])[:7]
    assert plan.targets[0].klass == "unstable"


def test_unknown_recheck_class_is_refused() -> None:
    with pytest.raises(RecheckViolation):
        order_queue([{"class": "approved", "axis": "x"}])


# --- 5-7긋기 예산 -------------------------------------------------------------


def test_budget_is_forced_between_five_and_seven(manifest: dict) -> None:
    assert (MIN_BUDGET, MAX_BUDGET) == (5, 7)
    for bad in (0, 4, 8):
        with pytest.raises(RecheckViolation):
            plan_recheck_session(manifest, RID, budget=bad)

    for good in (5, 6, 7):
        plan = plan_recheck_session(manifest, RID, budget=good)
        assert plan.budget == good
        assert len(plan.targets) == good  # 대기 9건이므로 예산이 상한이다


# --- 부활값 미시험 강등 (recovery 의미론 재사용) ------------------------------


def test_revived_value_is_demoted_to_untested(manifest: dict) -> None:
    log = EventLog()
    log.append(main_session_start())
    original = log.append(left_strike(V1, "frag-1"))
    log.append(left_strike(V1, "frag-1"))  # 재긋기 - tombstone
    log.append(session_end())

    plan = plan_recheck_session(manifest, RID)
    log.append(plan.opening)  # 막 경계: 세션 종료 -> 4막 재심 세션 시작
    log.append(revive(RID, original.event_id))

    state = fold_recovery(log.events)

    assert state.rejected_revives == ()
    assert state.remaining_hypotheses == FULL_SPACE
    demoted = revived_demotions(state)
    assert len(demoted) == 1
    assert (demoted[0].axis, demoted[0].value) == (AXIS, V1)
    assert demoted[0].cause is RecoveryChannel.REVIVE
    assert state.axis_states[AXIS].discrimination == "untested"


# --- now 주입 결정성 ----------------------------------------------------------


def test_check_due_is_pure_and_deterministic_under_injected_now(manifest: dict) -> None:
    snapshot = copy.deepcopy(manifest)

    first = check_due(manifest, NOW_DUE)
    second = check_due(manifest, NOW_DUE)
    assert first == second
    assert manifest == snapshot  # 부수효과 없음

    # 주입 시각만이 판정을 바꾼다 - 벽시계와 무관하다.
    assert check_due(manifest, "2026-07-31T00:00:00+00:00").due is False
    assert check_due(manifest, datetime(2026, 9, 1)).due is True  # naive는 UTC로 간주
    source = Path(recheck_module.__file__).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("now", "utcnow", "today")
    ]
    assert calls == []  # datetime.now() 류 벽시계 호출 없음


# --- last_review 갱신은 새 manifest 착지 --------------------------------------


def test_refresh_last_review_lands_a_new_manifest(manifest: dict, tmp_path: Path) -> None:
    landed = refresh_last_review(manifest, "2026-08-08T12:00:00+00:00")

    assert landed is not manifest
    assert manifest["last_review"] == LAST_REVIEW  # 기존 아티팩트 불변
    assert landed["last_review"] == "2026-08-08T12:00:00+00:00"
    assert landed["outputs"][MANIFEST_JSON]["content_hash"] == manifest_self_hash(landed)
    assert (
        landed["outputs"][MANIFEST_JSON]["content_hash"]
        != manifest["outputs"][MANIFEST_JSON]["content_hash"]
    )
    # last_review와 자기 해시 외에는 동일한 착지물이다.
    assert {k: v for k, v in landed.items() if k not in ("last_review", "outputs")} == {
        k: v for k, v in manifest.items() if k not in ("last_review", "outputs")
    }

    target = tmp_path / MANIFEST_JSON
    target.write_text(json.dumps(landed, ensure_ascii=False), encoding="utf-8")
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    # 착지된 manifest가 다음 /popper 실행의 배너 판정을 이끈다.
    assert check_due(reloaded, "2026-08-10T00:00:00+00:00").due is False
    assert check_due(reloaded, "2026-08-15T12:00:00+00:00").due is True
