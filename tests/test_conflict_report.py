"""AC6 - 수기/컴파일 룰 충돌이 자동 해소 없이 나란히 표면화되고 재심 큐에 적재되는지 검증한다."""

from __future__ import annotations

import json

import pytest

from xout.compiler import CATALOG_VERSION, MANIFEST_JSON, write_outputs
from xout.conflict import (
    CONFLICT_CLASS,
    CONFLICT_REASON_PREFIX,
    CORROBORATION_GRADES,
    DISCRIMINATED,
    ID_SEPARATOR,
    UNRESOLVED,
    UNTESTED,
    VALUE_SOURCES,
    CompiledRule,
    ConflictEntry,
    ConflictReport,
    ConflictViolation,
    ConsentLedger,
    ConsentViolation,
    ManualRule,
    conflict_id,
    conflict_id_from_reason,
    core_denominator,
    detect_conflicts,
    excluded_from_core,
    is_falsification_target,
    parse_conflict_id,
)
from xout.recovery import RECHECK_CLASS_PRIORITY, RecheckEntry

NOW = "2026-08-28T00:00:00+00:00"
MANUAL_ID = "user-verbosity"


def manual_rule(
    axis: str = "verbosity",
    value: str = "explanatory",
    rule_id: str = MANUAL_ID,
) -> ManualRule:
    return ManualRule(
        rule_id=rule_id,
        axis=axis,
        value=value,
        text="변경의 배경과 대안까지 단계별로 설명한다.",
        source_path="~/.claude/CLAUDE.md",
    )


def compiled_rule(
    axis: str = "verbosity",
    value: str = "terse",
    grade: str = DISCRIMINATED,
    provenance: tuple[str, ...] = ("ev-strike-1",),
    source: str = "elicited",
) -> CompiledRule:
    return CompiledRule(
        rule_id=f"{CATALOG_VERSION}:{axis}:{value}",
        axis=axis,
        value=value,
        text="결론과 코드만 제시하고 부연 설명은 생략한다.",
        corroboration_grade=grade,
        value_source=source,
        strike_provenance=provenance,
    )


def opted_ledger(rule_id: str = MANUAL_ID) -> ConsentLedger:
    ledger = ConsentLedger()
    ledger.opt_in_manual_rule(rule_id)
    return ledger


@pytest.fixture()
def report() -> ConflictReport:
    return detect_conflicts(
        [manual_rule()], [compiled_rule()], CATALOG_VERSION, opted_ledger()
    )


# ---------------------------------------------------------------------------
# 충돌 리포트 - 양측 나란히, 자동 해소 없음
# ---------------------------------------------------------------------------


def test_conflict_row_surfaces_both_sides_side_by_side(report) -> None:
    assert len(report) == 1
    row = report.report_rows()[0]
    assert row["axis"] == "verbosity"
    assert row["resolution"] == UNRESOLVED
    sides = row["sides"]
    assert [side["side"] for side in sides] == ["manual", "compiled"]
    # 어느 쪽도 승자로 선택되지 않는다 - 두 값이 그대로 나란히 실린다.
    assert {side["value"] for side in sides} == {"explanatory", "terse"}


def test_manual_side_is_an_untested_prior(report) -> None:
    manual_side = report.report_rows()[0]["sides"][0]
    assert manual_side["corroboration_grade"] == UNTESTED
    assert manual_side["strike_provenance"] == []
    # 수기 룰은 긋기를 겪은 적이 없으므로 등급이 미시험에서 벗어날 수 없다.
    assert manual_rule().corroboration_grade == UNTESTED
    assert manual_rule().strike_provenance == ()


def test_compiled_side_carries_grade_source_and_strike_grounds(report) -> None:
    compiled_side = report.report_rows()[0]["sides"][1]
    assert compiled_side["corroboration_grade"] == DISCRIMINATED
    assert compiled_side["corroboration_grade"] in CORROBORATION_GRADES
    assert compiled_side["value_source"] == "elicited"
    assert compiled_side["value_source"] in VALUE_SOURCES
    assert compiled_side["strike_provenance"] == ["ev-strike-1"]


def test_write_time_resolution_is_impossible() -> None:
    manual = manual_rule()
    compiled = compiled_rule()
    cid = conflict_id(manual.axis, manual.rule_id, CATALOG_VERSION)
    for chosen in ("resolved", "manual_wins", "compiled_wins", ""):
        with pytest.raises(ConflictViolation):
            ConflictEntry(
                conflict_id=cid,
                axis=manual.axis,
                catalog_version=CATALOG_VERSION,
                manual=manual,
                compiled=compiled,
                resolution=chosen,
            )


def test_same_value_is_not_a_conflict() -> None:
    manual = manual_rule(value="terse")
    compiled = compiled_rule(value="terse")
    report = detect_conflicts(
        [manual], [compiled], CATALOG_VERSION, opted_ledger()
    )
    assert len(report) == 0
    with pytest.raises(ConflictViolation):
        ConflictEntry(
            conflict_id=conflict_id(manual.axis, manual.rule_id, CATALOG_VERSION),
            axis=manual.axis,
            catalog_version=CATALOG_VERSION,
            manual=manual,
            compiled=compiled,
        )


def test_axis_mismatch_between_sides_is_rejected() -> None:
    manual = manual_rule(axis="autonomy", value="ask_first")
    with pytest.raises(ConflictViolation):
        ConflictEntry(
            conflict_id=conflict_id("verbosity", manual.rule_id, CATALOG_VERSION),
            axis="verbosity",
            catalog_version=CATALOG_VERSION,
            manual=manual,
            compiled=compiled_rule(),
        )


def test_report_is_append_only_with_unique_conflict_ids(report) -> None:
    entry = report.entries[0]
    with pytest.raises(ConflictViolation):
        report.add(entry)
    with pytest.raises(ConflictViolation):
        report[0] = entry
    with pytest.raises(ConflictViolation):
        del report[0]
    assert len(report) == 1


# ---------------------------------------------------------------------------
# 수기 룰 opt-in - 등록 전에는 반증 대상이 아니다
# ---------------------------------------------------------------------------


def test_manual_rule_is_not_falsification_target_before_opt_in() -> None:
    ledger = ConsentLedger()
    assert not is_falsification_target(manual_rule(), ledger)
    report = detect_conflicts(
        [manual_rule()], [compiled_rule()], CATALOG_VERSION, ledger
    )
    # 값이 어긋나도 opt-in 전에는 충돌 자체가 생성되지 않는다.
    assert len(report) == 0


def test_opt_in_is_per_rule_not_global() -> None:
    ledger = opted_ledger(rule_id="another-rule")
    assert not is_falsification_target(manual_rule(), ledger)
    ledger.opt_in_manual_rule(MANUAL_ID)
    assert is_falsification_target(manual_rule(), ledger)
    report = detect_conflicts(
        [manual_rule()], [compiled_rule()], CATALOG_VERSION, ledger
    )
    assert len(report) == 1


def test_detect_conflicts_requires_a_consent_ledger() -> None:
    for bogus in (None, [], object()):
        with pytest.raises(ConsentViolation):
            detect_conflicts([manual_rule()], [compiled_rule()], CATALOG_VERSION, bogus)


def test_consent_ledger_is_append_only_and_fold_inert() -> None:
    ledger = opted_ledger()
    record = ledger.records[0]
    assert record.seq == 0
    assert record.fold_contribution == 0
    with pytest.raises(ConsentViolation):
        ledger[0] = record
    with pytest.raises(ConsentViolation):
        del ledger[0]
    assert len(ledger) == 1


# ---------------------------------------------------------------------------
# strike 근거 규율 - 근거 없는 등급/변경 금지
# ---------------------------------------------------------------------------


def test_compiled_rule_grades_require_matching_strike_grounds() -> None:
    with pytest.raises(ConflictViolation):
        compiled_rule(grade=DISCRIMINATED, provenance=())
    with pytest.raises(ConflictViolation):
        compiled_rule(grade=UNTESTED, provenance=("ev-1",))
    with pytest.raises(ConflictViolation):
        compiled_rule(grade="approved")
    with pytest.raises(ConflictViolation):
        compiled_rule(source="guessed")


def test_untested_compiled_rule_without_grounds_is_still_valid() -> None:
    rule = compiled_rule(grade=UNTESTED, provenance=(), source="mined-prior")
    assert rule.corroboration_grade == UNTESTED
    assert rule.strike_provenance == ()


# ---------------------------------------------------------------------------
# 재심 큐 적재
# ---------------------------------------------------------------------------


def test_recheck_queue_entry_via_recovery_factory(report) -> None:
    assert CONFLICT_CLASS == RECHECK_CLASS_PRIORITY[-1]
    entry = report.recheck_entries(RecheckEntry)[0]
    assert isinstance(entry, RecheckEntry)
    assert entry.klass == CONFLICT_CLASS
    assert entry.priority == RECHECK_CLASS_PRIORITY.index(CONFLICT_CLASS)
    assert entry.axis == "verbosity"
    assert entry.reason.startswith(CONFLICT_REASON_PREFIX)
    assert conflict_id_from_reason(entry.reason) == report.conflict_ids[0]


def test_three_surfaces_join_on_a_single_conflict_id(report) -> None:
    cid = report.conflict_ids[0]
    row = report.report_rows()[0]
    recheck = report.recheck_entries()[0]
    cell = report.correction_cells()[0]
    assert row["conflict_id"] == cid
    assert recheck["conflict_id"] == cid
    assert cell["conflict_id"] == cid
    # '교정' 셀은 코어 반증 지표 분모에서 빠진다.
    assert cell["in_core_denominator"] is False
    assert core_denominator([cell]) == ()
    assert excluded_from_core([cell]) == (cell,)


def test_conflict_id_is_deterministic_and_parseable() -> None:
    cid = conflict_id("verbosity", MANUAL_ID, CATALOG_VERSION)
    assert cid == conflict_id("verbosity", MANUAL_ID, CATALOG_VERSION)
    assert parse_conflict_id(cid) == ("verbosity", MANUAL_ID, CATALOG_VERSION)
    with pytest.raises(ConflictViolation):
        conflict_id(f"verbosity{ID_SEPARATOR}extra", MANUAL_ID, CATALOG_VERSION)
    with pytest.raises(ConflictViolation):
        conflict_id_from_reason("unstable:verbosity")


def test_one_compiled_rule_per_axis_is_enforced() -> None:
    with pytest.raises(ConflictViolation):
        detect_conflicts(
            [manual_rule()],
            [compiled_rule(), compiled_rule(value="balanced")],
            CATALOG_VERSION,
            opted_ledger(),
        )


def test_conflict_lands_in_manifest_recheck_queue(tmp_path, report) -> None:
    """충돌 리포트 행이 컴파일 산출물의 manifest 충돌 목록과 재심 큐에 그대로 적재된다."""
    write_outputs(
        (),
        base_dir=tmp_path,
        session_id="sess-conflict",
        now=NOW,
        conflicts=report.report_rows(),
    )
    manifest = json.loads((tmp_path / MANIFEST_JSON).read_text(encoding="utf-8"))

    landed = manifest["conflicts"][0]
    assert landed["conflict_id"] == report.conflict_ids[0]
    assert landed["resolution"] == UNRESOLVED
    assert [side["side"] for side in landed["sides"]] == ["manual", "compiled"]

    queue = manifest["recheck_queue"]
    conflict_entries = [e for e in queue if e["class"] == CONFLICT_CLASS]
    assert len(conflict_entries) == 1
    entry = conflict_entries[0]
    assert entry["conflict_id"] == report.conflict_ids[0]
    assert entry["priority"] == RECHECK_CLASS_PRIORITY.index(CONFLICT_CLASS)
    # 충돌 클래스는 unstable/untested-prior 클래스 뒤에 적재된다.
    assert queue[-1] is not None
    assert [e["class"] for e in queue][-1] == CONFLICT_CLASS
    assert all(
        RECHECK_CLASS_PRIORITY.index(a["class"]) <= RECHECK_CLASS_PRIORITY.index(b["class"])
        for a, b in zip(queue, queue[1:])
    )
