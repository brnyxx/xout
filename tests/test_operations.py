"""doctor, activation, export, backup의 실사용 운영 계약."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import xout.backup as backup_module
from xout.backup import BACKUP_MANIFEST, create_backup, inspect_backup
from xout.cli import _activation_state, main
from xout.doctor import run_doctor
from xout.exporter import EXPORT_FORMATS, render_export, write_export
from xout.store import EventStore
from xout.web.state import ColdOpenSession
from xout.writer import OwnedWriter


def _land(base: Path, session_id: str = "operations") -> EventStore:
    store = EventStore(base)
    session = ColdOpenSession(session_id=session_id, store=store, land_dir=base)
    for _ in range(session.snapshot().slots_total):
        session.strike("left")
    assert session.snapshot().landing.status == "landed"
    return store


def test_doctor_is_healthy_for_empty_writable_install(tmp_path: Path) -> None:
    report = run_doctor(tmp_path / "new-data")
    assert report.healthy
    assert {check.name for check in report.checks} >= {
        "python",
        "package_resources",
        "ground_truth_seal",
        "data_directory",
        "event_replay",
        "landed_outputs",
        "loopback_server",
    }


def test_doctor_reports_corrupt_event_stream_without_repairing_it(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    broken = sessions / "broken.jsonl"
    body = '{"type":"session_start"}\nnot-json\n'
    broken.write_text(body, encoding="utf-8")

    report = run_doctor(tmp_path)

    assert not report.healthy
    replay = next(check for check in report.checks if check.name == "event_replay")
    assert replay.status == "error"
    assert "JSONL" in replay.evidence
    assert broken.read_text(encoding="utf-8") == body


def test_doctor_rejects_partial_landing(tmp_path: Path) -> None:
    base = tmp_path / "data"
    _land(base)
    (base / "manifest.json").unlink()

    report = run_doctor(base)

    assert not report.healthy
    outputs = next(check for check in report.checks if check.name == "landed_outputs")
    assert outputs.status == "error"
    assert "partial landing" in outputs.evidence


def test_activation_truth_distinguishes_inactive_active_and_drift(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    base = tmp_path / "data"
    base.mkdir()
    (base / "XOUT.md").write_text("# rules\n", encoding="utf-8")
    writer = OwnedWriter(base_dir=base)
    claude_md = claude / "CLAUDE.md"
    claude_md.write_text("# User\n", encoding="utf-8")

    assert _activation_state(base)["status"] == "inactive"
    claude_md.write_text(f"# User\n{writer.import_line()}\n", encoding="utf-8")
    assert _activation_state(base)["status"] == "active"
    (base / "XOUT.md").unlink()
    assert _activation_state(base)["status"] == "import-drift"
    (base / "XOUT.md").write_text("# rules\n", encoding="utf-8")
    claude_md.write_text("# User\n@/old/popper/XOUT.md\n", encoding="utf-8")
    assert _activation_state(base)["status"] == "import-drift"


def test_all_export_formats_are_deterministic_and_explicit(tmp_path: Path) -> None:
    store = _land(tmp_path / "data")
    events = store.load_completed()
    rendered = {
        format_name: render_export(events, format_name)
        for format_name in EXPORT_FORMATS
    }

    assert rendered["markdown"].startswith("# xout Rules")
    assert rendered["agents"].startswith("# Agent Instructions")
    assert rendered["claude"].startswith("# Claude Instructions")
    assert json.loads(rendered["json"])["artifact"] == "popper_rules_export"
    target = write_export(tmp_path / "AGENTS.md", rendered["agents"])
    assert target.read_text(encoding="utf-8") == rendered["agents"]


def test_backup_round_trip_inspection_and_tamper_detection(tmp_path: Path) -> None:
    base = tmp_path / "data"
    _land(base)
    archive = tmp_path / "popper-backup.zip"
    result = create_backup(base, archive)

    assert result.path == archive
    assert result.checksum_path.is_file()
    healthy = inspect_backup(archive)
    assert healthy.healthy
    assert healthy.session_count == 1
    assert healthy.file_count > 3

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith("XOUT.md"):
                data += b"tampered"
            target.writestr(info, data)
    broken = inspect_backup(tampered)
    assert not broken.healthy
    assert any("checksum mismatch" in error for error in broken.errors)


def test_cli_json_surfaces_are_machine_readable(tmp_path: Path, capsys) -> None:
    assert main(["doctor", "--base-dir", str(tmp_path), "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["artifact"] == "popper_doctor"

    assert main(["sessions", "--base-dir", str(tmp_path), "--json"]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert sessions == {"artifact": "popper_sessions", "sessions": []}

    assert main(["status", "--base-dir", str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    assert status["artifact"] == "popper_status"
    assert captured.err == ""


def test_backup_manifest_is_not_listed_as_payload(tmp_path: Path) -> None:
    archive = tmp_path / "empty.zip"
    create_backup(tmp_path / "empty-data", archive)
    with zipfile.ZipFile(archive) as backup:
        manifest = json.loads(backup.read(BACKUP_MANIFEST))
    assert BACKUP_MANIFEST not in manifest["files"]


def test_backup_inspection_rejects_malformed_manifest_shapes(tmp_path: Path) -> None:
    for index, manifest in enumerate(([], {"schema_version": "one"})):
        archive = tmp_path / f"malformed-{index}.zip"
        with zipfile.ZipFile(archive, "w") as backup:
            backup.writestr(BACKUP_MANIFEST, json.dumps(manifest))
        report = inspect_backup(archive)
        assert not report.healthy
        assert report.errors


def test_backup_inspection_reports_unsupported_compression(tmp_path: Path) -> None:
    archive = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(archive, "w") as backup:
        backup.writestr(BACKUP_MANIFEST, "{}")
    body = bytearray(archive.read_bytes())
    local = body.index(b"PK\x03\x04")
    central = body.index(b"PK\x01\x02")
    body[local + 8 : local + 10] = (99).to_bytes(2, "little")
    body[central + 10 : central + 12] = (99).to_bytes(2, "little")
    archive.write_bytes(body)

    report = inspect_backup(archive)

    assert not report.healthy
    assert any("archive unreadable" in error for error in report.errors)


def test_backup_producer_enforces_the_inspector_limits(
    tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "data"
    base.mkdir()
    (base / "one").write_text("1", encoding="utf-8")
    (base / "two").write_text("2", encoding="utf-8")
    target = tmp_path / "limited.zip"
    monkeypatch.setattr(backup_module, "MAX_BACKUP_FILES", 2)

    with pytest.raises(ValueError, match="파일 수"):
        create_backup(base, target)
    assert not target.exists()

    monkeypatch.setattr(backup_module, "MAX_BACKUP_FILES", 10_000)
    monkeypatch.setattr(backup_module, "MAX_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(ValueError, match="크기"):
        create_backup(base, target)
    assert not target.exists()
