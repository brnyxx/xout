"""외부 네트워크 없이 설치, 데이터, replay, 루프백 경계를 진단한다."""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from xout.compiler import OUTPUT_FILES, verify_outputs
from xout.fixtures import load_pack
from xout.scoring import (
    DEFAULT_GROUND_TRUTH_HASH_PATH,
    DEFAULT_GROUND_TRUTH_PATH,
    load_ground_truth,
)
from xout.events import Event, EventType
from xout.session import PROFILE_RECHECK, load_session_specs
from xout.sessions import summarize_sessions
from xout.store import EventStore

APP_VERSION_FALLBACK = "source"
SUPPORTED_PYTHON_MIN = (3, 10)
SUPPORTED_PYTHON_MAX = (3, 14)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    evidence: str
    remediation: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status in {"ok", "skip"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    version: str
    base_dir: str
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.healthy for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": "popper_doctor",
            "version": self.version,
            "base_dir": self.base_dir,
            "healthy": self.healthy,
            "checks": [check.to_dict() for check in self.checks],
        }


def app_version() -> str:
    try:
        return version("popper")
    except PackageNotFoundError:
        return APP_VERSION_FALLBACK


def _capture(
    name: str,
    action: Callable[[], str],
    remediation: str,
) -> DoctorCheck:
    try:
        return DoctorCheck(name=name, status="ok", evidence=action())
    except Exception as exc:
        return DoctorCheck(
            name=name,
            status="error",
            evidence=f"{type(exc).__name__}: {exc}",
            remediation=remediation,
        )


def _writable_evidence(base_dir: Path) -> str:
    candidate = base_dir
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists() or not candidate.is_dir():
        raise OSError(f"쓸 수 있는 상위 디렉토리를 찾지 못했다: {base_dir}")
    if not os.access(candidate, os.W_OK | os.X_OK):
        raise PermissionError(f"쓰기 권한이 없다: {candidate}")
    return f"writable parent={candidate}"


def _loopback_evidence() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return f"bind=127.0.0.1:{server.getsockname()[1]}"


def _output_evidence(base_dir: Path) -> str:
    existing = tuple(name for name in OUTPUT_FILES if (base_dir / name).is_file())
    if not existing:
        return "no landed outputs"
    missing = tuple(name for name in OUTPUT_FILES if name not in existing)
    if missing:
        raise ValueError(
            f"partial landing: present={','.join(existing)} missing={','.join(missing)}"
        )
    mismatches = verify_outputs(base_dir)
    if mismatches:
        raise ValueError(json.dumps(mismatches, ensure_ascii=False))
    return "content hashes match"


def run_doctor(base_dir: Path | str) -> DoctorReport:
    base = Path(base_dir).expanduser().resolve()
    current = sys.version_info[:2]
    supported = SUPPORTED_PYTHON_MIN <= current <= SUPPORTED_PYTHON_MAX
    python_check = DoctorCheck(
        name="python",
        status="ok" if supported else "error",
        evidence=f"{sys.version.split()[0]} ({sys.platform})",
        remediation=None
        if supported
        else "Python 3.10-3.14 중 하나로 Popper를 다시 설치해라.",
    )
    checks = [python_check]
    checks.append(
        _capture(
            "package_resources",
            lambda: (
                f"catalog={load_pack().catalog_version}, "
                f"profiles={','.join(sorted(load_session_specs()))}"
            ),
            "wheel/plugin을 다시 설치하고 popper doctor를 재실행해라.",
        )
    )
    checks.append(
        _capture(
            "ground_truth_seal",
            lambda: load_ground_truth(
                DEFAULT_GROUND_TRUTH_PATH,
                expected_file_hash=DEFAULT_GROUND_TRUTH_HASH_PATH.read_text(
                    encoding="utf-8"
                ).strip(),
            ).file_hash,
            "봉인 정답지가 포함된 공식 배포물을 다시 설치해라.",
        )
    )
    checks.append(
        _capture(
            "data_directory",
            lambda: _writable_evidence(base),
            "--base-dir에 쓰기 가능한 경로를 지정하거나 권한을 복구해라.",
        )
    )

    store = EventStore(base)
    replayed: tuple[Any, ...] = ()
    try:
        replayed = store.load_all()
        summaries = summarize_sessions(replayed)
        catalog_version = load_pack().catalog_version
        specs = load_session_specs()
        for summary in summaries:
            if not summary.resumable:
                continue
            opening = next(
                (
                    event
                    for event in replayed
                    if event.session_id == summary.session_id
                    and isinstance(event, Event)
                    and event.type is EventType.SESSION_START
                ),
                None,
            )
            if opening is None:
                raise ValueError(f"session_start 없음: {summary.session_id}")
            if opening.payload.get("fixture_catalog_version") != catalog_version:
                raise ValueError(
                    f"fixture catalog 불일치: {summary.session_id}"
                )
            if summary.profile != PROFILE_RECHECK:
                spec = specs.get(summary.profile)
                expected_spec = (
                    {
                        "discriminative_slots": spec.discriminative_slots,
                        "probe_slots": list(spec.probe_slots),
                        "required_full_axes": spec.required_full_axes,
                    }
                    if spec is not None
                    else None
                )
                if opening.payload.get("session_spec") != expected_spec:
                    raise ValueError(f"session spec 불일치: {summary.session_id}")
        stalled = [
            summary.session_id
            for summary in summaries
            if summary.resumable
            and summary.slots_total > 0
            and summary.slots_used >= summary.slots_total
        ]
        if stalled:
            raise ValueError(
                "terminal event 없이 slot cap에 도달한 세션: "
                + ",".join(stalled)
                + " (popper resume으로 finalize 가능)"
            )
        checks.append(
            DoctorCheck(
                name="event_replay",
                status="ok",
                evidence=f"events={len(replayed)}, sessions={len(summaries)}",
            )
        )
    except Exception as exc:
        checks.append(
            DoctorCheck(
                name="event_replay",
                status="error",
                evidence=f"{type(exc).__name__}: {exc}",
                remediation="sessions/의 보고된 손상 줄을 확인하고 백업에서 복구해라.",
            )
        )
    checks.append(
        _capture(
            "landed_outputs",
            lambda: _output_evidence(base),
            "popper land로 재검증하되 수기 편집은 먼저 별도 보존해라.",
        )
    )
    checks.append(
        _capture(
            "loopback_server",
            _loopback_evidence,
            "로컬 방화벽 또는 루프백 네트워크 설정을 확인해라.",
        )
    )
    return DoctorReport(version=app_version(), base_dir=str(base), checks=tuple(checks))
