"""AC5 - 쓰기 권한 분리를 검증한다.

Popper는 소유 디렉토리 밖에 쓰지 않고, 사용자 CLAUDE.md 본문과 라이브
settings.json은 무변경이며, @import 한 줄은 허가 이벤트가 있을 때만
멱등 추가되고, content hash 불일치는 silent overwrite 대신 strike
신호로 반환된다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xout.compiler import MANIFEST_JSON, XOUT_MD, SETTINGS_JSON
from xout.conflict import ConsentLedger
from xout.writer import (
    ACTIVATION_RECEIPT,
    DETECT_MANUAL_EDIT,
    IMPORT_ADDED,
    IMPORT_ALREADY_PRESENT,
    IMPORT_INVALID_PERMISSION,
    IMPORT_NO_PERMISSION,
    IMPORT_NOT_OWNED,
    IMPORT_OWNERSHIP_DRIFT,
    IMPORT_REMOVED,
    IMPORT_SUBJECT_MISMATCH,
    IMPORT_TARGET_MISSING,
    MANUAL_EDIT_STRIKE,
    OwnedWriter,
    OwnershipViolation,
)

CLAUDE_MD_BODY = "# 사용자 규칙\n\n- 기존 본문은 Popper 소유가 아니다\n"
LIVE_SETTINGS_BODY = '{"model": "opus"}\n'
DOCS_V1 = {
    XOUT_MD: "# POPPER 실행 룰 v1\n",
    SETTINGS_JSON: '{"scope": "global"}\n',
}


def make_writer(tmp_path: Path) -> OwnedWriter:
    user_dir = tmp_path / "user" / ".claude"
    user_dir.mkdir(parents=True)
    claude_md = user_dir / "CLAUDE.md"
    claude_md.write_text(CLAUDE_MD_BODY, encoding="utf-8")
    live_settings = user_dir / "settings.json"
    live_settings.write_text(LIVE_SETTINGS_BODY, encoding="utf-8")
    return OwnedWriter(
        base_dir=tmp_path / "owned" / "popper",
        claude_md_path=claude_md,
        live_settings_path=live_settings,
    )


def grant(writer: OwnedWriter):
    return ConsentLedger().grant_import_permission(str(writer.claude_md_path))


# ── (1) 소유 디렉토리 밖 쓰기 차단 ──────────────────────────────────


def test_write_outside_owned_dir_is_blocked(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    outside = tmp_path / "escape.md"
    with pytest.raises(OwnershipViolation):
        writer.write_file(outside, "탈출 시도")
    with pytest.raises(OwnershipViolation):
        writer.write_file("../escape.md", "상대경로 탈출 시도")
    assert not outside.exists()
    assert not (tmp_path / "owned" / "escape.md").exists()


def test_write_outputs_rejects_names_outside_contract(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    with pytest.raises(OwnershipViolation):
        writer.write_outputs({"CLAUDE.md": "소유 계약 밖 산출물"})
    assert not writer.path("CLAUDE.md").exists()


def test_protected_user_files_blocked_even_inside_base_dir(tmp_path: Path) -> None:
    base = tmp_path / "claude"
    base.mkdir()
    claude_md = base / "CLAUDE.md"
    claude_md.write_text(CLAUDE_MD_BODY, encoding="utf-8")
    live_settings = base / "settings.json"
    live_settings.write_text(LIVE_SETTINGS_BODY, encoding="utf-8")
    writer = OwnedWriter(
        base_dir=base, claude_md_path=claude_md, live_settings_path=live_settings
    )
    with pytest.raises(OwnershipViolation):
        writer.write_file("settings.json", "{}")
    with pytest.raises(OwnershipViolation):
        writer.write_file("CLAUDE.md", "덮어쓰기 시도")
    assert claude_md.read_text(encoding="utf-8") == CLAUDE_MD_BODY
    assert live_settings.read_text(encoding="utf-8") == LIVE_SETTINGS_BODY


# ── (2) 허가 없으면 CLAUDE.md 무변경 ────────────────────────────────


def test_no_permission_leaves_claude_md_bytes_identical(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    before = writer.claude_md_path.read_bytes()
    outcome = writer.ensure_import()
    assert outcome.changed is False
    assert outcome.reason == IMPORT_NO_PERMISSION
    assert writer.claude_md_path.read_bytes() == before


def test_non_import_consent_is_rejected_without_touching_bytes(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    before = writer.claude_md_path.read_bytes()
    opted_in = ConsentLedger().opt_in_manual_rule("rule-commit_style")
    outcome = writer.ensure_import(opted_in)
    assert outcome.changed is False
    assert outcome.reason == IMPORT_INVALID_PERMISSION
    assert writer.claude_md_path.read_bytes() == before


def test_permission_for_other_target_is_rejected(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    before = writer.claude_md_path.read_bytes()
    other = ConsentLedger().grant_import_permission(
        str(tmp_path / "other" / "CLAUDE.md")
    )
    outcome = writer.ensure_import(other)
    assert outcome.changed is False
    assert outcome.reason == IMPORT_SUBJECT_MISMATCH
    assert writer.claude_md_path.read_bytes() == before


# ── (3) 허가 시 @import 한 줄만 추가 + 멱등 ────────────────────────


def test_permission_adds_exactly_one_line_and_is_idempotent(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    claude_md = writer.claude_md_path
    before = claude_md.read_bytes()

    first = writer.ensure_import(grant(writer))
    assert first.changed is True
    assert first.reason == IMPORT_ADDED
    after = claude_md.read_bytes()
    # 그 한 줄 외 어떤 바이트도 추가/변경되지 않는다
    newline = b"\r\n" if b"\r\n" in before else b"\n"
    assert after == before + first.line.encode("utf-8") + newline

    second = writer.ensure_import(grant(writer))
    assert second.changed is False
    assert second.reason == IMPORT_ALREADY_PRESENT
    assert claude_md.read_bytes() == after


def test_explicit_permission_creates_missing_claude_md_with_only_import(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fresh-home" / ".claude" / "CLAUDE.md"
    writer = OwnedWriter(
        base_dir=tmp_path / "owned" / "popper",
        claude_md_path=target,
    )
    without_permission = writer.ensure_import()
    assert without_permission.reason == IMPORT_TARGET_MISSING
    assert not target.exists()

    enabled = writer.ensure_import(grant(writer))
    assert enabled.changed is True
    assert enabled.reason == IMPORT_ADDED
    assert target.read_bytes() == (enabled.line + "\n").encode("utf-8")
    assert writer.path(ACTIVATION_RECEIPT).is_file()

    rollback = writer.remove_import()
    assert rollback.changed is True
    assert target.read_bytes() == b""
    assert not writer.path(ACTIVATION_RECEIPT).exists()


def test_preexisting_matching_import_is_never_claimed_or_removed(
    tmp_path: Path,
) -> None:
    writer = make_writer(tmp_path)
    line = writer.import_line()
    writer.claude_md_path.write_text(line + "\n", encoding="utf-8")
    before = writer.claude_md_path.read_bytes()

    enabled = writer.ensure_import(grant(writer))
    assert enabled.reason == IMPORT_ALREADY_PRESENT
    assert not writer.path(ACTIVATION_RECEIPT).exists()
    rollback = writer.remove_import()
    assert rollback.reason == IMPORT_NOT_OWNED
    assert rollback.changed is False
    assert writer.claude_md_path.read_bytes() == before


def test_rollback_removes_owned_occurrence_and_preserves_later_duplicate(
    tmp_path: Path,
) -> None:
    writer = make_writer(tmp_path)
    before = writer.claude_md_path.read_bytes()
    enabled = writer.ensure_import(grant(writer))
    newline = b"\r\n" if b"\r\n" in before else b"\n"
    duplicate = enabled.line.encode("utf-8") + newline
    with writer.claude_md_path.open("ab") as stream:
        stream.write(duplicate)

    rollback = writer.remove_import()
    assert rollback.reason == IMPORT_REMOVED
    assert writer.claude_md_path.read_bytes() == before + duplicate


def test_crlf_file_round_trips_without_line_ending_drift(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    before = CLAUDE_MD_BODY.replace("\n", "\r\n").encode("utf-8")
    writer.claude_md_path.write_bytes(before)

    enabled = writer.ensure_import(grant(writer))
    assert writer.claude_md_path.read_bytes() == (
        before + enabled.line.encode("utf-8") + b"\r\n"
    )
    assert writer.remove_import().reason == IMPORT_REMOVED
    assert writer.claude_md_path.read_bytes() == before


def test_rollback_fails_closed_when_prefix_ownership_has_drifted(
    tmp_path: Path,
) -> None:
    writer = make_writer(tmp_path)
    enabled = writer.ensure_import(grant(writer))
    data = writer.claude_md_path.read_bytes()
    writer.claude_md_path.write_bytes(b"!" + data[1:])

    rollback = writer.remove_import()
    assert rollback.reason == IMPORT_OWNERSHIP_DRIFT
    assert rollback.changed is False
    assert enabled.line.encode("utf-8") in writer.claude_md_path.read_bytes()


def test_prepared_receipt_never_claims_a_matching_line(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    enabled = writer.ensure_import(grant(writer))
    receipt = json.loads(writer.path(ACTIVATION_RECEIPT).read_text(encoding="utf-8"))
    receipt["state"] = "prepared"
    writer.path(ACTIVATION_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")

    rollback = writer.remove_import()
    assert rollback.reason == IMPORT_OWNERSHIP_DRIFT
    assert enabled.line.encode("utf-8") in writer.claude_md_path.read_bytes()


def test_removing_import_line_restores_original_bytes(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    claude_md = writer.claude_md_path
    before = claude_md.read_bytes()

    assert writer.ensure_import(grant(writer)).changed is True
    rollback = writer.remove_import()
    assert rollback.changed is True
    assert rollback.reason == IMPORT_REMOVED
    # @import 한 줄 제거가 전체 롤백 지점이다
    assert claude_md.read_bytes() == before


# ── (4) hash 불일치 시 silent overwrite 금지 + strike 신호 ─────────


def test_manual_edit_blocks_overwrite_and_returns_strike_signal(
    tmp_path: Path,
) -> None:
    writer = make_writer(tmp_path)
    first = writer.write_outputs(DOCS_V1, now="2026-08-28T00:00:00+00:00")
    assert first.blocked is False
    assert {path.name for path in first.written} == {
        XOUT_MD,
        SETTINGS_JSON,
        MANIFEST_JSON,
    }
    manifest = json.loads(writer.path(MANIFEST_JSON).read_text(encoding="utf-8"))
    assert set(manifest["outputs"]) == {XOUT_MD, SETTINGS_JSON}

    # 수기 편집이 없으면 재쓰기는 통과한다
    assert writer.write_outputs(DOCS_V1).blocked is False

    edited = "# POPPER 실행 룰 v1\n\n사용자가 직접 고친 줄\n"
    writer.path(XOUT_MD).write_text(edited, encoding="utf-8")
    manifest_before = writer.path(MANIFEST_JSON).read_bytes()

    blocked = writer.write_outputs(
        {XOUT_MD: "# POPPER 실행 룰 v2\n", SETTINGS_JSON: "{}\n"}
    )
    assert blocked.blocked is True
    assert blocked.written == ()
    detection = blocked.detections[0]
    assert detection.signal == MANUAL_EDIT_STRIKE
    assert detection.reason == DETECT_MANUAL_EDIT
    assert detection.recorded_hash != detection.actual_hash
    assert detection.to_dict()["signal"] == MANUAL_EDIT_STRIKE
    # silent overwrite 없음 - 수기 편집본과 manifest가 그대로다
    assert writer.path(XOUT_MD).read_text(encoding="utf-8") == edited
    assert writer.path(MANIFEST_JSON).read_bytes() == manifest_before


# ── (5) 라이브 settings.json 무변경 ────────────────────────────────


def test_live_settings_json_is_never_written(tmp_path: Path) -> None:
    writer = make_writer(tmp_path)
    live_settings = writer.live_settings_path
    before = live_settings.read_bytes()

    writer.write_outputs(DOCS_V1)
    writer.ensure_import(grant(writer))

    assert live_settings.read_bytes() == before
    # 제안 파일은 소유 디렉토리에만 생긴다
    assert writer.path(SETTINGS_JSON).is_file()
    with pytest.raises(OwnershipViolation):
        writer.write_file(live_settings, "{}")
    assert live_settings.read_bytes() == before
