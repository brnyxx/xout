"""레거시 ~/.claude/popper -> ~/.claude/xout 이관을 검증한다.

디렉토리 이동, 산출물 파일명 교체, receipt가 증명한 @import 한 줄의
경로 갱신, 그리고 이관 후 rollback이 여전히 성립하는지까지 본다.
증명이 어긋난 CLAUDE.md는 바이트 하나도 건드리지 않아야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from xout.compiler import SETTINGS_JSON, XOUT_MD
from xout.conflict import ConsentLedger
from xout.migrate import migrate_legacy_home
from xout.writer import ACTIVATION_RECEIPT, IMPORT_ADDED, IMPORT_REMOVED, OwnedWriter

CLAUDE_MD_BODY = "# 사용자 규칙\n\n- 기존 본문\n"


def _seed_legacy(tmp_path: Path, *, enable: bool = False) -> tuple[Path, Path]:
    claude_dir = tmp_path / ".claude"
    legacy = claude_dir / "popper"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "POPPER.md").write_text("# xout Rules\n", encoding="utf-8")
    (legacy / "settings.popper.json").write_text("{}\n", encoding="utf-8")
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text(CLAUDE_MD_BODY, encoding="utf-8")
    (claude_dir / "settings.json").write_text("{}\n", encoding="utf-8")
    if enable:
        writer = OwnedWriter(
            base_dir=legacy,
            claude_md_path=claude_md,
            live_settings_path=claude_dir / "settings.json",
        )
        permission = ConsentLedger().grant_import_permission(str(claude_md))
        outcome = writer.ensure_import(permission)
        assert outcome.reason == IMPORT_ADDED
    return claude_dir, claude_md


def test_moves_legacy_home_and_renames_outputs(tmp_path: Path) -> None:
    claude_dir, _ = _seed_legacy(tmp_path)
    result = migrate_legacy_home(claude_dir)
    assert result.moved
    assert result.reason == "migrated"
    assert not (claude_dir / "popper").exists()
    current = claude_dir / "xout"
    assert (current / XOUT_MD).is_file()
    assert (current / SETTINGS_JSON).is_file()
    assert not (current / "POPPER.md").exists()
    assert not (current / "settings.popper.json").exists()
    assert (current / "sessions").is_dir()
    assert set(result.renamed_files) == {XOUT_MD, SETTINGS_JSON}


def test_noop_when_current_home_exists(tmp_path: Path) -> None:
    claude_dir, _ = _seed_legacy(tmp_path)
    (claude_dir / "xout").mkdir()
    result = migrate_legacy_home(claude_dir)
    assert not result.moved
    assert result.reason == "not-needed"
    assert (claude_dir / "popper").is_dir()


def test_noop_without_legacy_home(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    result = migrate_legacy_home(claude_dir)
    assert not result.moved


def test_rewrites_owned_import_and_rollback_still_holds(tmp_path: Path) -> None:
    claude_dir, claude_md = _seed_legacy(tmp_path, enable=True)
    result = migrate_legacy_home(claude_dir)
    assert result.moved
    assert result.import_rewritten
    current = claude_dir / "xout"
    body = claude_md.read_text(encoding="utf-8")
    assert "/popper/" not in body
    assert f"/xout/{XOUT_MD}" in body
    receipt = json.loads(
        (current / ACTIVATION_RECEIPT).read_text(encoding="utf-8")
    )
    assert "/xout/" in receipt["line"]
    writer = OwnedWriter(
        base_dir=current,
        claude_md_path=claude_md,
        live_settings_path=claude_dir / "settings.json",
    )
    outcome = writer.remove_import()
    assert outcome.reason == IMPORT_REMOVED
    assert claude_md.read_text(encoding="utf-8") == CLAUDE_MD_BODY


def test_leaves_drifted_claude_md_untouched(tmp_path: Path) -> None:
    claude_dir, claude_md = _seed_legacy(tmp_path, enable=True)
    drifted = "# 다른 서문\n" + claude_md.read_text(encoding="utf-8")
    claude_md.write_text(drifted, encoding="utf-8")
    result = migrate_legacy_home(claude_dir)
    assert result.moved
    assert not result.import_rewritten
    assert claude_md.read_text(encoding="utf-8") == drifted
