"""사용자 프롬프트 채굴 → 페어 맥락 → reconcile → savepoint 계약."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import MANIFEST_JSON
from xout.mine import mine, user_rule_files
from xout.savepoint import SavepointError, create, list_savepoints, restore
from xout.state import ColdOpenSession
from xout.store import EventStore

AUTONOMY_LINES = {
    "ask_first": "Always ask for approval before editing.",
    "propose_then_act": "Write the plan first, then act.",
    "act_then_report": "Act first, then report.",
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "rules").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    return fake_home


@pytest.fixture
def landed(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    session = ColdOpenSession(store=EventStore(base), land_dir=base, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    return base


def _autonomy(base: Path) -> tuple[str, str | None]:
    manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
    entry = next(r for r in manifest["rules"] if r["axis"] == "autonomy")
    return entry["value"], entry.get("irreversible_value")


def test_user_level_rules_are_mined_by_default_and_deduplicated(home: Path, tmp_path: Path) -> None:
    (home / ".claude" / "CLAUDE.md").write_text("# global\nAlways ask for approval before editing.\n", encoding="utf-8")
    (home / ".claude" / "rules" / "team.md").write_text("Act first, then report.\n", encoding="utf-8")
    (home / ".claude" / "rules" / "notes.txt").write_text("Act first, then report.\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    assert [p.name for p in user_rule_files(home)] == ["CLAUDE.md", "team.md"]
    observations = mine([project], include_user=True)
    assert [(o.path, o.value) for o in observations] == [
        ("~/.claude/CLAUDE.md", "ask_first"),
        ("~/.claude/rules/team.md", "act_then_report"),
    ]
    assert all(Path(o.abs_path).is_absolute() for o in observations)
    assert mine([project]) == []
    # 루트가 ~/.claude 자체면 같은 파일을 두 번 읽지 않는다
    twice = mine([home / ".claude"], include_user=True)
    assert len(twice) == 2


def test_pair_json_carries_existing_observations_for_the_axis(capsys, home: Path, tmp_path: Path, monkeypatch) -> None:
    (home / ".claude" / "CLAUDE.md").write_text("Always ask for approval before editing.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["pair", "--base-dir", str(tmp_path / "b"), "--lang", "en"]) == 0
    payload = json.loads(capsys.readouterr().out)
    pair = payload["pair"]
    assert "mined" in pair
    if pair["axis"] == "autonomy":
        assert pair["mined"][0]["value"] == "ask_first"
    else:
        assert pair["mined"] == []


def test_reconcile_reports_duplicates_and_conflicts_and_writes_a_patch(
    capsys, home: Path, landed: Path, tmp_path: Path
) -> None:
    value, irreversible = _autonomy(landed)
    other = next(v for v in AUTONOMY_LINES if v not in {value, irreversible})
    (home / ".claude" / "CLAUDE.md").write_text(
        f"# global\n{AUTONOMY_LINES[value]}\nkeep me\n{AUTONOMY_LINES[other]}\n", encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()
    assert main(["reconcile", str(project), "--base-dir", str(landed), "--lang", "en", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(d["value"], d["line"]) for d in payload["duplicates"]] == [(value, 2)]
    assert [(c["observed_value"], c["line"]) for c in payload["conflicts"]] == [(other, 4)]
    patch = Path(payload["patch_path"])
    assert patch.is_file() and patch.parent == landed / "reconcile"
    text = patch.read_text(encoding="utf-8")
    assert f"-{AUTONOMY_LINES[value]}" in text and AUTONOMY_LINES[other] not in [
        l[1:] for l in text.splitlines() if l.startswith("-")
    ]
    # 보고만 했다 - 원본은 그대로
    assert (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8").count("\n") == 4


def test_reconcile_apply_needs_grant_then_removes_duplicates_behind_a_savepoint(
    capsys, home: Path, landed: Path, tmp_path: Path
) -> None:
    value, irreversible = _autonomy(landed)
    other = next(v for v in AUTONOMY_LINES if v not in {value, irreversible})
    target = home / ".claude" / "CLAUDE.md"
    original = f"# global\n{AUTONOMY_LINES[value]}\nkeep me\n{AUTONOMY_LINES[other]}\n"
    target.write_text(original, encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    assert main(["reconcile", str(project), "--base-dir", str(landed), "--lang", "en", "--apply"]) == 1
    assert "needs --grant" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == original

    assert main(["reconcile", str(project), "--base-dir", str(landed), "--lang", "en", "--apply", "--grant", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed_files"] == [str(target.resolve())]
    assert target.read_text(encoding="utf-8") == f"# global\nkeep me\n{AUTONOMY_LINES[other]}\n"
    consent = (landed / "consent.jsonl").read_text(encoding="utf-8")
    assert "reconcile_apply_granted" in consent

    savepoint_id = payload["savepoint_id"]
    assert [p.savepoint_id for p in list_savepoints(landed)] == [savepoint_id]
    assert main(["savepoint", "restore", savepoint_id, "--base-dir", str(landed), "--lang", "en"]) == 0
    assert "restored" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == original


def test_savepoint_create_list_restore_roundtrip(tmp_path: Path) -> None:
    base = tmp_path / "base"
    a = tmp_path / "a.md"
    a.write_text("one\n", encoding="utf-8")
    missing = tmp_path / "missing.md"
    point = create(base, [a, missing], "test", now="2026-09-02T00:00:00+00:00")
    assert point.savepoint_id.startswith("sp-20260902T000000-")
    assert [(f.existed, Path(f.path).name) for f in point.files] == [(True, "a.md"), (False, "missing.md")]
    a.write_text("changed\n", encoding="utf-8")
    missing.write_text("appeared later\n", encoding="utf-8")
    results = restore(base, point.savepoint_id)
    assert [(Path(r.path).name, r.action) for r in results] == [("a.md", "restored"), ("missing.md", "left_in_place")]
    assert a.read_text(encoding="utf-8") == "one\n"
    assert missing.read_text(encoding="utf-8") == "appeared later\n", "스냅샷 당시 없던 파일은 지우지 않는다"
    assert [(Path(r.path).name, r.action) for r in restore(base, point.savepoint_id)] == [("a.md", "unchanged"), ("missing.md", "left_in_place")]
    assert [p.savepoint_id for p in list_savepoints(base)] == [point.savepoint_id]
    with pytest.raises(SavepointError):
        restore(base, "sp-nope")


def test_savepoint_cli_defaults_to_user_and_project_rule_files(capsys, home: Path, tmp_path: Path, monkeypatch) -> None:
    (home / ".claude" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("y\n", encoding="utf-8")
    monkeypatch.chdir(project)
    base = tmp_path / "base"
    assert main(["savepoint", "--base-dir", str(base), "--lang", "en", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(Path(f["path"]).name for f in payload["files"]) == ["AGENTS.md", "CLAUDE.md"]
    assert main(["savepoint", "list", "--base-dir", str(base), "--lang", "en"]) == 0
    assert payload["savepoint_id"] in capsys.readouterr().out
    assert main(["savepoint", "restore", "sp-nope", "--base-dir", str(base), "--lang", "en"]) == 1
