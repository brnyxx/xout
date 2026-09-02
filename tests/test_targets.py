"""소유 블록 활성화 계약 - 허가 없이는 안 쓰고, 정확히 그 블록만 되돌리고, 세이브포인트가 선행한다."""

from __future__ import annotations

from pathlib import Path

from xout.conflict import ConsentKind, ConsentRecord
from xout.savepoint import list_savepoints
from xout.targets import (
    BLOCK_ADDED,
    BLOCK_ALREADY_PRESENT,
    BLOCK_NO_PERMISSION,
    BLOCK_NOT_PRESENT,
    BLOCK_REMOVED,
    BLOCK_SUBJECT_MISMATCH,
    BLOCK_UPDATED,
    block_state,
    ensure_block,
    find_block,
    remove_block,
    render_block,
)

RULES = "# xout Rules\n\n- Act first.\n"


def _grant(path: Path) -> ConsentRecord:
    return ConsentRecord(kind=ConsentKind.IMPORT_PERMISSION_GRANTED, subject=str(path))


def test_block_requires_matching_permission(tmp_path: Path) -> None:
    base, target = tmp_path / "base", tmp_path / "AGENTS.md"
    target.write_text("# mine\n", encoding="utf-8")
    assert ensure_block(base, "codex", target, RULES, None).reason == BLOCK_NO_PERMISSION
    other = _grant(tmp_path / "elsewhere.md")
    assert ensure_block(base, "codex", target, RULES, other).reason == BLOCK_SUBJECT_MISMATCH
    assert target.read_text(encoding="utf-8") == "# mine\n"
    assert list_savepoints(base) == []


def test_block_add_update_remove_touches_only_the_block(tmp_path: Path) -> None:
    base, target = tmp_path / "base", tmp_path / "AGENTS.md"
    original = "# team rules\n\n- keep this\n"
    target.write_text(original, encoding="utf-8")
    grant = _grant(target)

    added = ensure_block(base, "codex", target, RULES, grant)
    assert added.reason == BLOCK_ADDED and added.changed and added.savepoint_id
    text = target.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert find_block(text) and "- Act first." in text
    assert block_state(base, "codex", target) == {
        "target_id": "codex", "path": str(target), "active": True, "receipt": True,
    }

    assert ensure_block(base, "codex", target, RULES, grant).reason == BLOCK_ALREADY_PRESENT

    updated = ensure_block(base, "codex", target, RULES + "- Then report.\n", grant)
    assert updated.reason == BLOCK_UPDATED
    text = target.read_text(encoding="utf-8")
    assert text.count("<!-- xout:begin") == 1 and "- Then report." in text
    assert text.startswith(original)

    removed = remove_block(base, "codex", target)
    assert removed.reason == BLOCK_REMOVED
    assert target.read_text(encoding="utf-8") == original
    assert block_state(base, "codex", target)["active"] is False
    assert remove_block(base, "codex", target).reason == BLOCK_NOT_PRESENT
    reasons = [p.reason for p in list_savepoints(base)]
    assert reasons == ["enable codex", "enable codex", "undo codex"]


def test_block_created_file_is_removed_again_on_undo(tmp_path: Path) -> None:
    base, target = tmp_path / "base", tmp_path / "GEMINI.md"
    assert ensure_block(base, "gemini", target, RULES, _grant(target)).reason == BLOCK_ADDED
    assert target.read_text(encoding="utf-8") == render_block(RULES) + "\n"
    assert remove_block(base, "gemini", target).reason == BLOCK_REMOVED
    assert not target.exists()


def test_block_survives_user_edits_around_it(tmp_path: Path) -> None:
    base, target = tmp_path / "base", tmp_path / "AGENTS.md"
    target.write_text("top\n", encoding="utf-8")
    ensure_block(base, "codex", target, RULES, _grant(target))
    target.write_text(target.read_text(encoding="utf-8") + "\nbottom note\n", encoding="utf-8")
    assert remove_block(base, "codex", target).reason == BLOCK_REMOVED
    assert target.read_text(encoding="utf-8") == "top\n\nbottom note\n"


def test_registry_entries_are_verified_user_level_files(tmp_path: Path) -> None:
    from xout.targets import MODE_BLOCK, MODE_IMPORT, REGISTRY, SCOPE_PROJECT, SCOPE_USER

    assert set(REGISTRY) == {"claude", "codex", "opencode", "gemini", "copilot", "pi", "omp", "gjc", "kiro", "agents"}
    for target in REGISTRY.values():
        assert target.verified and target.doc_url.startswith("https://")
        assert target.mode in (MODE_IMPORT, MODE_BLOCK)
        assert target.scope in (SCOPE_USER, SCOPE_PROJECT)
    assert REGISTRY["claude"].mode == MODE_IMPORT
    assert REGISTRY["agents"].scope == SCOPE_PROJECT
    home, project = tmp_path / "h", tmp_path / "p"
    assert REGISTRY["codex"].resolve(home, project) == (home / ".codex" / "AGENTS.md").resolve()
    assert REGISTRY["agents"].resolve(home, project) == (project / "AGENTS.md").resolve()


def test_enable_and_undo_block_targets_through_the_cli(capsys, tmp_path: Path, monkeypatch) -> None:
    import json

    from xout.cli import main
    from xout.state import ColdOpenSession
    from xout.store import EventStore

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(project)
    base = tmp_path / "base"
    session = ColdOpenSession(store=EventStore(base), land_dir=base, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)

    assert main(["enable", "--base-dir", str(base), "--target", "codex", "kiro", "agents"]) == 1
    assert not (home / ".codex" / "AGENTS.md").exists()

    assert main(["enable", "--base-dir", str(base), "--grant", "--target", "codex", "kiro", "agents"]) == 0
    codex = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert codex.count("<!-- xout:begin") == 1 and "# xout Rules" in codex
    kiro = (home / ".kiro" / "steering" / "xout.md").read_text(encoding="utf-8")
    assert kiro.startswith("---\ninclusion: always\n---\n")
    assert "<!-- xout:begin" in (project / "AGENTS.md").read_text(encoding="utf-8")
    assert main(["enable", "--base-dir", str(base), "--grant", "--target", "codex"]) == 0  # 멱등

    assert main(["targets", "--base-dir", str(base), "--json"]) == 0
    rows = {r["target_id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["codex"]["active"] and rows["kiro"]["active"] and rows["agents"]["active"]
    assert not rows["gemini"]["active"]

    assert main(["undo", "--base-dir", str(base), "--target", "codex", "kiro", "agents"]) == 0
    assert not (home / ".codex" / "AGENTS.md").exists(), "xout이 만든 파일은 되돌릴 때 사라진다"
    assert not (home / ".kiro" / "steering" / "xout.md").exists()
    assert not (project / "AGENTS.md").exists()
    assert main(["enable", "--base-dir", str(base), "--grant", "--target", "nope"]) == 1
