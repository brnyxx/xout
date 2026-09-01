"""AC4 - XOUT.md에는 8축 실행 룰만 착지하고 manifest.json이 인식론 메타를 전담하는지 검증한다."""

from __future__ import annotations

import json

import pytest

from xout.compiler import (
    CATALOG_VERSION,
    EPISTEMIC_TOKENS,
    GRADE_DISCRIMINATED,
    GRADE_INDISCRIMINATE,
    GRADE_LABELS,
    GRADE_UNSTABLE,
    GRADE_UNTESTED,
    MANIFEST_JSON,
    OUTPUT_FILES,
    XOUT_MD,
    RECHECK_CLASS_PRIORITY,
    RULE_TEXT,
    SETTINGS_JSON,
    SOURCE_ELICITED,
    SOURCE_MINED_PRIOR,
    HashMismatch,
    compile_rules,
    content_hash,
    manifest_self_hash,
    mined_mode,
    render_popper_md,
    verify_outputs,
    write_outputs,
)
from xout.counter import DEFAULT_CATALOG
from xout.events import Event, EventType, Refutation, StrikeTarget, strike

SESSION = "sess-compile"
NOW = "2026-08-28T00:00:00+00:00"

# 혼합 스트림에서 아무 이벤트도 받지 않는 축들 - 반증 이력 0건.
UNTESTED_AXES = ("comment_doc", "response_language", "scope_adherence", "test_discipline")


def _refutation(axis: str, value: str, side: str) -> Refutation:
    return Refutation(axis=axis, value=value, fragment_id=f"{axis}:{side}", side=side)


def left_strike(axis: str, value: str):
    return strike(
        session_id=SESSION,
        pair_id=f"{axis}-pair",
        axis=axis,
        scene_id="scene-1",
        target=StrikeTarget.LEFT,
        refutations=(_refutation(axis, value, "left"),),
    )


def both_strike(axis: str, left_value: str, right_value: str):
    return strike(
        session_id=SESSION,
        pair_id=f"{axis}-pair",
        axis=axis,
        scene_id="scene-1",
        target=StrikeTarget.BOTH,
        refutations=(
            _refutation(axis, left_value, "left"),
            _refutation(axis, right_value, "right"),
        ),
    )


def pair_strike(axis: str):
    return strike(
        session_id=SESSION,
        pair_id=f"{axis}-pair",
        axis=axis,
        scene_id="scene-1",
        target=StrikeTarget.PAIR,
        refutations=(),
    )


def probe_flip(axis: str) -> Event:
    return Event(
        type=EventType.PROBE_RESULT,
        session_id=SESSION,
        payload={"axis": axis, "result": "flip"},
    )


def by_axis(rules):
    return {rule.axis: rule for rule in rules}


@pytest.fixture()
def discriminating_strike():
    """autonomy 축에서 두 값을 한 번에 반증해 생존값 1개(판별시험 통과)를 만든다."""
    return both_strike("autonomy", "ask_first", "propose_then_act")


@pytest.fixture()
def mixed_events(discriminating_strike):
    """4개 등급이 모두 나오는 혼합 스트림."""
    return (
        Event(type=EventType.SESSION_START, session_id=SESSION),
        discriminating_strike,
        left_strike("verbosity", "terse"),
        pair_strike("commit_style"),
        probe_flip("error_behavior"),
    )


# ---------------------------------------------------------------------------
# 8축 전부 방출 + 반증 0건 축의 untested-prior 방출
# ---------------------------------------------------------------------------


def test_compile_emits_all_eight_axes_even_with_zero_events() -> None:
    rules = compile_rules(())
    assert len(rules) == 8
    assert [rule.axis for rule in rules] == sorted(DEFAULT_CATALOG)
    for rule in rules:
        assert (rule.axis, rule.value) in RULE_TEXT
        assert rule.text == RULE_TEXT[(rule.axis, rule.value)]


def test_zero_history_axis_lands_mined_mode_as_untested_prior() -> None:
    rules = by_axis(compile_rules(()))
    for axis, values in DEFAULT_CATALOG.items():
        rule = rules[axis]
        assert rule.value == mined_mode(axis) == values[0]
        assert rule.corroboration_grade == GRADE_UNTESTED
        assert rule.value_source == SOURCE_MINED_PRIOR
        assert rule.provenance == ()


def test_mixed_events_still_emit_all_eight_axes(mixed_events) -> None:
    rules = compile_rules(mixed_events)
    assert [rule.axis for rule in rules] == sorted(DEFAULT_CATALOG)


# ---------------------------------------------------------------------------
# corroboration 등급 4값 + value_source 직교
# ---------------------------------------------------------------------------


def test_mixed_stream_yields_all_four_grades(mixed_events) -> None:
    rules = by_axis(compile_rules(mixed_events))
    assert rules["autonomy"].corroboration_grade == GRADE_DISCRIMINATED
    assert rules["verbosity"].corroboration_grade == GRADE_INDISCRIMINATE
    assert rules["commit_style"].corroboration_grade == GRADE_INDISCRIMINATE
    assert rules["error_behavior"].corroboration_grade == GRADE_UNSTABLE
    for axis in UNTESTED_AXES:
        assert rules[axis].corroboration_grade == GRADE_UNTESTED


def test_value_source_is_orthogonal_to_grade(mixed_events) -> None:
    rules = by_axis(compile_rules(mixed_events))
    # 반증으로 좁혀진 축은 elicited.
    assert rules["autonomy"].value_source == SOURCE_ELICITED
    assert rules["verbosity"].value_source == SOURCE_ELICITED
    # 같은 indiscriminate 등급이라도 pair 긋기만 받은 축은 mined-prior다 - 등급과 출처는 직교한다.
    assert rules["commit_style"].value_source == SOURCE_MINED_PRIOR
    assert rules["error_behavior"].value_source == SOURCE_MINED_PRIOR
    assert (
        rules["verbosity"].corroboration_grade
        == rules["commit_style"].corroboration_grade
    )
    assert rules["verbosity"].value_source != rules["commit_style"].value_source


def test_discriminated_rule_carries_strike_provenance(
    mixed_events, discriminating_strike
) -> None:
    rules = by_axis(compile_rules(mixed_events))
    assert rules["autonomy"].value == "act_then_report"
    # both 긋기는 한 이벤트가 1:N(2건) 반증을 남기므로 반증 건수만큼 근거가 남는다.
    assert rules["autonomy"].provenance == (discriminating_strike.event_id,) * 2
    for axis in UNTESTED_AXES:
        assert rules[axis].provenance == ()


def test_compile_is_a_pure_replay(mixed_events) -> None:
    assert compile_rules(mixed_events) == compile_rules(mixed_events)


# ---------------------------------------------------------------------------
# XOUT.md - 실행 가능한 룰만, 인식론 주석 0줄
# ---------------------------------------------------------------------------


def test_popper_md_contains_exactly_eight_executable_rules(mixed_events) -> None:
    rules = compile_rules(mixed_events)
    body = render_popper_md(rules)
    lines = body.splitlines()
    assert lines[0] == "# xout Rules"
    assert lines[1] == ""
    bullets = lines[2:]
    assert len(bullets) == 8
    executable_texts = set(RULE_TEXT.values())
    for bullet in bullets:
        assert bullet.startswith("- ")
        assert bullet[2:] in executable_texts
    assert body.endswith("\n")


def test_popper_md_has_zero_epistemic_annotation_lines(mixed_events) -> None:
    for stream in ((), mixed_events):
        body = render_popper_md(compile_rules(stream))
        lowered = body.lower()
        for token in EPISTEMIC_TOKENS:
            assert token.lower() not in lowered, f"인식론 어휘 유출: {token}"
        # 헤더/공백/불릿 외의 줄(주석 라인)은 존재하지 않는다.
        for line in body.splitlines()[2:]:
            assert line.startswith("- ")
        assert "<!--" not in body


# ---------------------------------------------------------------------------
# write_outputs 착지 + manifest 인식론 메타
# ---------------------------------------------------------------------------


def _landed(tmp_path, mixed_events):
    result = write_outputs(
        mixed_events, base_dir=tmp_path, session_id=SESSION, now=NOW
    )
    manifest = json.loads((tmp_path / MANIFEST_JSON).read_text(encoding="utf-8"))
    return result, manifest


def test_write_outputs_lands_exactly_three_files(tmp_path, mixed_events) -> None:
    result, _ = _landed(tmp_path, mixed_events)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(OUTPUT_FILES)
    assert result.base_dir == tmp_path
    assert tuple(p.name for p in result.written) == OUTPUT_FILES
    for path in result.written:
        assert path.parent == tmp_path


def test_manifest_records_grade_label_source_per_rule(tmp_path, mixed_events) -> None:
    _, manifest = _landed(tmp_path, mixed_events)
    entries = {entry["axis"]: entry for entry in manifest["rules"]}
    assert sorted(entries) == sorted(DEFAULT_CATALOG)
    for axis, entry in entries.items():
        assert entry["corroboration_grade"] in GRADE_LABELS
        assert entry["corroboration_label"] == GRADE_LABELS[entry["corroboration_grade"]]
        assert entry["value_source"] in (SOURCE_ELICITED, SOURCE_MINED_PRIOR)
        assert entry["catalog_version"] == CATALOG_VERSION
        assert entry["rule_id"] == f"{CATALOG_VERSION}:{axis}:{entry['value']}"
    assert entries["autonomy"]["corroboration_grade"] == GRADE_DISCRIMINATED
    assert entries["error_behavior"]["corroboration_grade"] == GRADE_UNSTABLE
    for axis in UNTESTED_AXES:
        assert entries[axis]["corroboration_grade"] == GRADE_UNTESTED
        assert entries[axis]["value_source"] == SOURCE_MINED_PRIOR


def test_manifest_records_content_hash_per_output(tmp_path, mixed_events) -> None:
    _, manifest = _landed(tmp_path, mixed_events)
    outputs = manifest["outputs"]
    for name in (XOUT_MD, SETTINGS_JSON):
        body = (tmp_path / name).read_text(encoding="utf-8")
        assert outputs[name]["content_hash"] == content_hash(body)
        assert outputs[name]["bytes"] == len(body.encode("utf-8"))
    # manifest 자기 해시는 자기 필드를 제외하고 재계산해도 일치한다.
    assert outputs[MANIFEST_JSON]["self_excluding"] is True
    assert outputs[MANIFEST_JSON]["content_hash"] == manifest_self_hash(manifest)


def test_manifest_reserves_scope_field(tmp_path, mixed_events) -> None:
    _, manifest = _landed(tmp_path, mixed_events)
    assert manifest["scope"] == "global"
    assert manifest["catalog_version"] == CATALOG_VERSION
    assert manifest["session_id"] == SESSION


def test_manifest_recheck_queue_orders_unstable_before_untested_prior(
    tmp_path, mixed_events
) -> None:
    _, manifest = _landed(tmp_path, mixed_events)
    queue = manifest["recheck_queue"]
    classes = [entry["class"] for entry in queue]
    assert classes == ["unstable"] + ["untested-prior"] * len(UNTESTED_AXES)
    assert queue[0]["axis"] == "error_behavior"
    assert sorted(entry["axis"] for entry in queue[1:]) == sorted(UNTESTED_AXES)
    for entry in queue:
        assert entry["priority"] == RECHECK_CLASS_PRIORITY.index(entry["class"])
    assert [entry["order"] for entry in queue] == list(range(len(queue)))


def test_settings_output_is_a_proposal_with_eight_rule_ids(
    tmp_path, mixed_events
) -> None:
    _, manifest = _landed(tmp_path, mixed_events)
    settings = json.loads((tmp_path / SETTINGS_JSON).read_text(encoding="utf-8"))
    assert settings["_popper"]["proposal_only"] is True
    assert settings["rule_ids"] == [entry["rule_id"] for entry in manifest["rules"]]
    assert len(settings["rule_ids"]) == 8


# ---------------------------------------------------------------------------
# content hash 대조 - silent overwrite 금지
# ---------------------------------------------------------------------------


def test_verify_outputs_is_clean_right_after_write(tmp_path, mixed_events) -> None:
    _landed(tmp_path, mixed_events)
    assert verify_outputs(tmp_path) == ()


def test_manual_edit_blocks_silent_overwrite(tmp_path, mixed_events) -> None:
    _landed(tmp_path, mixed_events)
    target = tmp_path / XOUT_MD
    target.write_text(
        target.read_text(encoding="utf-8") + "- 수기로 덧붙인 줄\n", encoding="utf-8"
    )
    records = verify_outputs(tmp_path)
    assert len(records) == 1
    assert records[0]["reason"] == "manual_edit"
    assert records[0]["path"] == str(target)
    with pytest.raises(HashMismatch):
        write_outputs(mixed_events, base_dir=tmp_path, session_id=SESSION, now=NOW)


def test_acknowledged_rewrite_records_the_mismatch(tmp_path, mixed_events) -> None:
    _landed(tmp_path, mixed_events)
    target = tmp_path / XOUT_MD
    target.write_text("# 임의 수정\n", encoding="utf-8")
    result = write_outputs(
        mixed_events,
        base_dir=tmp_path,
        session_id=SESSION,
        now=NOW,
        acknowledge_mismatch=True,
    )
    assert len(result.mismatches) == 1
    assert result.manifest["hash_mismatch_records"][0]["reason"] == "manual_edit"
    # 재착지 후에는 다시 깨끗해진다.
    assert verify_outputs(tmp_path) == ()


def test_missing_output_is_reported(tmp_path, mixed_events) -> None:
    _landed(tmp_path, mixed_events)
    (tmp_path / SETTINGS_JSON).unlink()
    records = verify_outputs(tmp_path)
    assert len(records) == 1
    assert records[0]["reason"] == "missing"


def test_unchanged_rewrite_needs_no_acknowledgement(tmp_path, mixed_events) -> None:
    _landed(tmp_path, mixed_events)
    result = write_outputs(
        mixed_events, base_dir=tmp_path, session_id=SESSION, now=NOW
    )
    assert result.mismatches == ()
    assert verify_outputs(tmp_path) == ()


def test_write_outputs_is_deterministic_for_the_same_stream(
    tmp_path, mixed_events
) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    write_outputs(mixed_events, base_dir=first, session_id=SESSION, now=NOW)
    write_outputs(mixed_events, base_dir=second, session_id=SESSION, now=NOW)
    for name in OUTPUT_FILES:
        assert (first / name).read_bytes() == (second / name).read_bytes()
