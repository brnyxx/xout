"""세션 요약과 중단 후 결정적 replay 재개 계약."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import xout.cli
from xout.cli import main
from xout.doctor import run_doctor
from xout.events import Event, EventType, EventLog, SchemaViolation, StrikeEvent
from xout.session import PROFILE_RECHECK
from xout.sessions import (
    STATUS_IN_PROGRESS,
    latest_resumable,
    summarize_session,
    summarize_sessions,
)
from xout.store import EventStore
from xout.state import ColdOpenSession, SessionComplete


def test_event_log_hydrates_without_changing_event_identity() -> None:
    original = Event(
        type=EventType.SESSION_START,
        session_id="hydrate",
        event_id="opening-id",
        at="2026-01-01T00:00:00+00:00",
        seq=99,
        payload={"profile": "product"},
    )
    log = EventLog([original])

    assert len(log.events) == 1
    assert log.events[0].event_id == original.event_id
    assert log.events[0].at == original.at
    assert log.events[0].seq == 0


def test_product_session_resumes_at_exact_next_slot_and_lands(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    first = ColdOpenSession(
        session_id="resume-product",
        store=store,
        land_dir=tmp_path,
    )
    for _ in range(4):
        first.strike("left")
    expected_pair = first.snapshot().pair.pair_id

    resumed = ColdOpenSession(
        session_id="resume-product",
        store=store,
        land_dir=tmp_path,
        history=store.load_completed(),
        resume_events=store.load_session("resume-product"),
    )

    assert resumed.snapshot().slots_used == 4
    assert resumed.snapshot().pair.pair_id == expected_pair
    for _ in range(11):
        resumed.strike("left")
    assert resumed.snapshot().session_complete
    assert resumed.snapshot().landing.status == "landed"
    events = store.load_session("resume-product")
    assert sum(
        isinstance(event, Event) and event.type is EventType.SESSION_START
        for event in events
    ) == 1
    assert sum(isinstance(event, StrikeEvent) for event in events) == 15


def test_resume_uses_sealed_repo_skin_not_current_filesystem(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "main.py").write_text("pass\n", encoding="utf-8")
    (repo_b / "index.ts").write_text("export {};\n", encoding="utf-8")
    store = EventStore(tmp_path / "data")
    first = ColdOpenSession(
        repo_root=repo_a,
        session_id="sealed-skin",
        store=store,
        land_dir=tmp_path / "data",
    )
    first.strike("left")
    events = store.load_session("sealed-skin")
    opening = events[0]
    assert isinstance(opening, Event)
    assert opening.payload["repo_skin"]["lang"] == "Python"

    resumed = ColdOpenSession(
        repo_root=repo_b,
        session_id="sealed-skin",
        store=store,
        land_dir=tmp_path / "data",
        resume_events=events,
    )
    snapshot = resumed.snapshot()
    assert "Python" in snapshot.pair.left_text
    assert "TypeScript" not in snapshot.pair.left_text

    broken_opening = replace(
        opening,
        payload={
            key: value
            for key, value in opening.payload.items()
            if key != "repo_skin"
        },
    )
    with pytest.raises(SchemaViolation, match="repo_skin"):
        ColdOpenSession(
            session_id="sealed-skin",
            store=store,
            land_dir=tmp_path / "data",
            resume_events=(broken_opening, *events[1:]),
        )
    wrong_catalog = replace(
        opening,
        payload={**opening.payload, "fixture_catalog_version": "future"},
    )
    with pytest.raises(SchemaViolation, match="fixture catalog"):
        ColdOpenSession(
            session_id="sealed-skin",
            store=store,
            land_dir=tmp_path / "data",
            resume_events=(wrong_catalog, *events[1:]),
        )
    wrong_pairs = replace(
        opening,
        payload={**opening.payload, "rendered_pairs_sha256": "sha256:tampered"},
    )
    with pytest.raises(SchemaViolation, match="rendered pair digest"):
        ColdOpenSession(
            session_id="sealed-skin",
            store=store,
            land_dir=tmp_path / "data",
            resume_events=(wrong_pairs, *events[1:]),
        )


def test_recheck_resume_uses_persisted_budget_and_axes(tmp_path: Path) -> None:
    manifest = {
        "recheck_queue": [
            {"axis": "autonomy", "class": "unstable", "rule_id": "a"},
            {"axis": "verbosity", "class": "unstable", "rule_id": "v"},
            {"axis": "comment_doc", "class": "untested-prior", "rule_id": "c"},
            {"axis": "commit_style", "class": "untested-prior", "rule_id": "d"},
            {"axis": "error_behavior", "class": "untested-prior", "rule_id": "e"},
            {"axis": "response_language", "class": "conflict", "conflict_id": "f"},
        ]
    }
    store = EventStore(tmp_path)
    first = ColdOpenSession(
        session_id="resume-recheck",
        profile=PROFILE_RECHECK,
        store=store,
        land_dir=tmp_path,
        recheck_manifest=manifest,
        recheck_budget=6,
    )
    first.strike("pair")
    first.strike("pair")

    resumed = ColdOpenSession(
        session_id="resume-recheck",
        profile=PROFILE_RECHECK,
        store=store,
        land_dir=tmp_path,
        history=store.load_completed(),
        resume_events=store.load_session("resume-recheck"),
    )
    assert resumed.snapshot().slots_used == 2
    assert resumed.snapshot().slots_total == 6


def test_completed_session_cannot_be_resumed(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(session_id="done", store=store, land_dir=tmp_path)
    for _ in range(session.snapshot().slots_total):
        session.strike("pair")

    with pytest.raises(SessionComplete):
        ColdOpenSession(
            session_id="done",
            store=store,
            land_dir=tmp_path,
            resume_events=store.load_session("done"),
        )


def test_full_slot_crash_window_finalizes_idempotently_on_resume(
    tmp_path: Path, monkeypatch
) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(session_id="crash-window", store=store, land_dir=tmp_path)
    for _ in range(14):
        session.strike("left")
    original_finalize = ColdOpenSession._finalize
    monkeypatch.setattr(ColdOpenSession, "_finalize", lambda self: None)
    session.strike("left")
    assert summarize_session(store.load_session("crash-window")).resumable
    assert not run_doctor(tmp_path).healthy
    monkeypatch.setattr(ColdOpenSession, "_finalize", original_finalize)

    resumed = ColdOpenSession(
        session_id="crash-window",
        store=store,
        land_dir=tmp_path,
        resume_events=store.load_session("crash-window"),
    )
    assert resumed.snapshot().session_complete
    assert resumed.snapshot().landing.status == "landed"
    events = store.load_session("crash-window")
    assert sum(
        isinstance(event, Event) and event.type is EventType.SESSION_VALIDATED
        for event in events
    ) == 1
    assert run_doctor(tmp_path).healthy


def test_cli_full_slot_recovery_does_not_leave_an_idle_server(
    tmp_path: Path, monkeypatch
) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(session_id="cli-crash", store=store, land_dir=tmp_path)
    for _ in range(14):
        session.strike("left")
    original_finalize = ColdOpenSession._finalize
    monkeypatch.setattr(ColdOpenSession, "_finalize", lambda self: None)
    session.strike("left")
    monkeypatch.setattr(ColdOpenSession, "_finalize", original_finalize)

    monkeypatch.setattr("builtins.input", lambda *args: "")
    assert (
        main(["resume", "cli-crash", "--base-dir", str(tmp_path)])
        == 0
    )
    assert not summarize_session(store.load_session("cli-crash")).resumable


def test_resume_rejects_non_contiguous_or_duplicate_events(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(session_id="corrupt", store=store, land_dir=tmp_path)
    session.strike("left")
    events = store.load_session("corrupt")

    with pytest.raises(SchemaViolation, match="seq 불연속"):
        ColdOpenSession(
            session_id="corrupt",
            store=store,
            land_dir=tmp_path,
            resume_events=(events[0], replace(events[1], seq=8)),
        )
    with pytest.raises(SchemaViolation, match="ID 중복"):
        ColdOpenSession(
            session_id="corrupt",
            store=store,
            land_dir=tmp_path,
            resume_events=(events[0], replace(events[1], event_id=events[0].event_id)),
        )


def test_summaries_are_recent_first_and_find_resumable(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    older = ColdOpenSession(session_id="older", store=store, land_dir=tmp_path)
    older.strike("pair")
    newer = ColdOpenSession(session_id="newer", store=store, land_dir=tmp_path)
    newer.strike("pair")
    summaries = summarize_sessions(store.load_all())

    assert {summary.status for summary in summaries} == {STATUS_IN_PROGRESS}
    assert {summary.session_id for summary in summaries} == {"older", "newer"}
    assert latest_resumable(store.load_all()).session_id in {"older", "newer"}
    assert summarize_session(store.load_session("older")).slots_used == 1

    for _ in range(14):
        older.strike("pair")
    assert (
        summarize_session(store.load_session("older")).status
        != STATUS_IN_PROGRESS
    )


def test_open_auto_resumes_one_product_session(tmp_path: Path, monkeypatch) -> None:
    store = EventStore(tmp_path)
    interrupted = ColdOpenSession(
        session_id="auto-resume",
        store=store,
        land_dir=tmp_path,
    )
    for _ in range(3):
        interrupted.strike("left")
    observed: dict[str, int | str] = {}

    def fake_serve(session, args):
        observed["session_id"] = session.session_id
        observed["slots_used"] = session.snapshot().slots_used
        return 0

    monkeypatch.setattr(xout.cli, "_launch", fake_serve)
    assert main(["open", "--base-dir", str(tmp_path)]) == 0
    assert observed == {"session_id": "auto-resume", "slots_used": 3}


def test_open_requires_explicit_choice_for_multiple_incomplete_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    store = EventStore(tmp_path)
    for session_id in ("one", "two"):
        session = ColdOpenSession(
            session_id=session_id,
            store=store,
            land_dir=tmp_path,
        )
        session.strike("pair")

    def must_not_launch(session, args):
        raise AssertionError("ambiguous open must not start a session loop")

    monkeypatch.setattr(xout.cli, "_launch", must_not_launch)
    assert main(["open", "--base-dir", str(tmp_path)]) == 1
