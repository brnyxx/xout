"""프로세스 간 이벤트/착지 직렬화와 원자 파일 교체 회귀 테스트."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from xout.atomic import atomic_write_bytes
from xout.cli import main
from xout.compiler import MANIFEST_JSON, OUTPUT_FILES, write_outputs
from xout.events import Event, EventType, StrikeTarget, strike
from xout.locking import LockTimeout, ProcessFileLock, base_runtime_lock
from xout.store import EventStore
from xout.web.state import ColdOpenSession


def _append_worker(base: str, worker: int, count: int, barrier) -> None:
    store = EventStore(base)
    barrier.wait()
    for index in range(count):
        store.append(
            Event(
                type=EventType.SESSION_START,
                session_id="shared-session",
                event_id=f"worker-{worker}-{index}",
                payload={"worker": worker},
            )
        )


def _atomic_writer(path: str, barrier, finished) -> None:
    target = Path(path)
    first = b"A" * 262_144
    second = b"B" * 262_144
    barrier.wait()
    for index in range(40):
        atomic_write_bytes(target, first if index % 2 == 0 else second)
    finished.set()


def _read_after_transient_share_release(path: Path) -> bytes:
    deadline = time.monotonic() + 5
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.001)


def _landing_worker(base: str, session_id: str, target: str, barrier) -> None:
    store = EventStore(base)
    session = ColdOpenSession(
        session_id=session_id,
        store=store,
        land_dir=base,
        history=store.load_completed(),
    )
    barrier.wait()
    for _ in range(session.snapshot().slots_total):
        session.strike(target)
    if session.snapshot().landing.status != "landed":
        raise RuntimeError(f"착지 실패: {session.snapshot().landing}")


def _hold_lock(path: str, ready, release) -> None:
    with ProcessFileLock(Path(path)):
        ready.set()
        release.wait(10)


def _hold_base_runtime(base: str, ready, release) -> None:
    with base_runtime_lock(base, timeout=5):
        ready.set()
        release.wait(10)


def _spawn_context():
    return multiprocessing.get_context("spawn")


def test_concurrent_append_to_one_session_never_loses_or_merges_lines(
    tmp_path: Path,
) -> None:
    context = _spawn_context()
    workers = 4
    count = 40
    barrier = context.Barrier(workers)
    processes = [
        context.Process(
            target=_append_worker, args=(str(tmp_path), worker, count, barrier)
        )
        for worker in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0

    events = EventStore(tmp_path).load_session("shared-session")
    assert len(events) == workers * count
    assert len({event.event_id for event in events}) == workers * count


def test_atomic_replace_never_exposes_partial_content(tmp_path: Path) -> None:
    context = _spawn_context()
    target = tmp_path / "atomic.bin"
    allowed = {b"A" * 262_144, b"B" * 262_144}
    atomic_write_bytes(target, next(iter(allowed)))
    barrier = context.Barrier(2)
    finished = context.Event()
    process = context.Process(
        target=_atomic_writer, args=(str(target), barrier, finished)
    )
    process.start()
    barrier.wait()
    observations = 0
    deadline = time.monotonic() + 30
    while not finished.is_set() or process.is_alive():
        if not process.is_alive() and not finished.is_set():
            break
        assert time.monotonic() < deadline
        assert _read_after_transient_share_release(target) in allowed
        observations += 1
        time.sleep(0.001)
    process.join(30)
    assert process.exitcode == 0
    assert observations > 0
    assert _read_after_transient_share_release(target) in allowed
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_lock_timeout_is_explicit_instead_of_hanging(tmp_path: Path) -> None:
    context = _spawn_context()
    path = tmp_path / "held.lock"
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(path), ready, release))
    process.start()
    assert ready.wait(10)
    try:
        with pytest.raises(LockTimeout):
            with ProcessFileLock(path, timeout=0.1):
                pass
    finally:
        release.set()
        process.join(30)
    assert process.exitcode == 0


def test_base_runtime_admission_precedes_session_creation(tmp_path: Path) -> None:
    context = _spawn_context()
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_base_runtime,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    assert ready.wait(10)
    try:
        assert main(["open", "--base-dir", str(tmp_path), "--no-browser", "--new"]) == 1
        assert EventStore(tmp_path).session_ids() == ()
    finally:
        release.set()
        process.join(30)
    assert process.exitcode == 0


def test_completed_stream_excludes_other_in_progress_sessions(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    store.append(
        Event(
            type=EventType.SESSION_START,
            session_id="in-progress",
            payload={"profile": "product"},
        )
    )
    store.append(
        strike(
            "in-progress",
            "pair",
            "autonomy",
            "scene",
            StrikeTarget.PAIR,
            (),
        )
    )
    stable = Event(
        type=EventType.PREREG_SEALED,
        session_id="prereg",
        payload={"digest": "sealed"},
    )
    store.append(stable)

    assert store.load_completed() == (stable,)


def test_concurrent_sessions_finish_with_one_cumulative_landing(tmp_path: Path) -> None:
    context = _spawn_context()
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_landing_worker,
            args=(str(tmp_path), "concurrent-left", "left", barrier),
        ),
        context.Process(
            target=_landing_worker,
            args=(str(tmp_path), "concurrent-right", "right", barrier),
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0

    before = {name: (tmp_path / name).read_bytes() for name in OUTPUT_FILES}
    manifest = json.loads((tmp_path / MANIFEST_JSON).read_text(encoding="utf-8"))
    write_outputs(
        EventStore(tmp_path).load_completed(),
        base_dir=tmp_path,
        session_id=manifest["session_id"],
        now=manifest["generated_at"],
    )
    after = {name: (tmp_path / name).read_bytes() for name in OUTPUT_FILES}
    assert after == before
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".tmp")]
