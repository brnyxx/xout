"""xout 소유 데이터의 원자 ZIP snapshot과 읽기 전용 무결성 검사."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xout.atomic import atomic_write_bytes, atomic_write_text
from xout.doctor import app_version
from xout.locking import base_lock
from xout.sessions import summarize_sessions
from xout.store import EventStore

BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST = "backup.json"
MAX_BACKUP_FILES = 10_000
MAX_UNCOMPRESSED_BYTES = 134_217_728


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit_errors(member_count: int, uncompressed_bytes: int) -> tuple[str, ...]:
    errors: list[str] = []
    if member_count > MAX_BACKUP_FILES:
        errors.append(f"too many archive members: {member_count}")
    if uncompressed_bytes > MAX_UNCOMPRESSED_BYTES:
        errors.append(f"archive expands beyond limit: {uncompressed_bytes}")
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    checksum_path: Path
    checksum: str
    file_count: int
    session_count: int


@dataclass(frozen=True, slots=True)
class BackupInspection:
    path: Path
    healthy: bool
    schema_version: int
    created_at: str
    app_version: str
    file_count: int
    session_count: int
    latest_session_at: str | None
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": "popper_backup_inspection",
            "path": str(self.path),
            "healthy": self.healthy,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "file_count": self.file_count,
            "session_count": self.session_count,
            "latest_session_at": self.latest_session_at,
            "errors": list(self.errors),
        }


def create_backup(base_dir: Path | str, destination: Path | str) -> BackupResult:
    base = Path(base_dir).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target == base or base in target.parents:
        raise ValueError("백업 파일은 xout 소유 데이터 디렉토리 밖에 둬야 한다")
    if target.suffix.lower() != ".zip":
        raise ValueError("백업 파일 확장자는 .zip이어야 한다")

    with base_lock(base):
        store = EventStore(base)
        summaries = summarize_sessions(store.load_all())
        files: dict[str, bytes] = {}
        total_bytes = 0
        if base.exists():
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(base).as_posix()
                if path.name.endswith(".tmp") or relative.startswith(".locks/"):
                    continue
                if len(files) + 2 > MAX_BACKUP_FILES:
                    raise ValueError(
                        f"백업 파일 수가 한도({MAX_BACKUP_FILES - 1})를 넘는다"
                    )
                size = path.stat().st_size
                if total_bytes + size > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"백업 원본 크기가 한도({MAX_UNCOMPRESSED_BYTES} bytes)를 넘는다"
                    )
                data = path.read_bytes()
                files[relative] = data
                total_bytes += len(data)
        manifest = {
            "artifact": "popper_backup",
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": _now(),
            "app_version": app_version(),
            "files": {
                name: {"bytes": len(data), "sha256": _sha256(data)}
                for name, data in files.items()
            },
            "sessions": [summary.to_dict() for summary in summaries],
        }
        manifest_body = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        limit_errors = _limit_errors(
            len(files) + 1, total_bytes + len(manifest_body)
        )
        if limit_errors:
            raise ValueError("; ".join(limit_errors))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr(BACKUP_MANIFEST, manifest_body)
        payload = buffer.getvalue()
        atomic_write_bytes(target, payload)
        checksum = _sha256(payload)
        checksum_path = target.with_suffix(target.suffix + ".sha256")
        atomic_write_text(checksum_path, f"{checksum}  {target.name}\n")
        return BackupResult(
            path=target,
            checksum_path=checksum_path,
            checksum=checksum,
            file_count=len(files),
            session_count=len(summaries),
        )


def inspect_backup(path: Path | str) -> BackupInspection:
    source = Path(path).expanduser().resolve()
    errors: list[str] = []
    schema_version = 0
    created_at = ""
    archive_version = ""
    sessions: list[dict[str, Any]] = []
    files: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            total_size = sum(info.file_size for info in archive.infolist())
            errors.extend(_limit_errors(len(names), total_size))
            if errors:
                raise zipfile.BadZipFile("; ".join(errors))
            if len(names) != len(set(names)):
                errors.append("duplicate archive member")
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                errors.append("unsafe archive path")
            try:
                decoded = json.loads(archive.read(BACKUP_MANIFEST))
            except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                errors.append(f"backup manifest unreadable: {exc}")
                decoded = {}
            if not isinstance(decoded, dict):
                errors.append("backup manifest must be an object")
                manifest: dict[str, Any] = {}
            else:
                manifest = decoded
            raw_schema_version = manifest.get("schema_version")
            if isinstance(raw_schema_version, int) and not isinstance(
                raw_schema_version, bool
            ):
                schema_version = raw_schema_version
            else:
                errors.append("backup schema_version must be an integer")
            raw_created_at = manifest.get("created_at")
            created_at = raw_created_at if isinstance(raw_created_at, str) else ""
            raw_app_version = manifest.get("app_version")
            archive_version = (
                raw_app_version if isinstance(raw_app_version, str) else ""
            )
            raw_sessions = manifest.get("sessions", [])
            if isinstance(raw_sessions, list):
                sessions = [
                    session for session in raw_sessions if isinstance(session, dict)
                ]
                if len(sessions) != len(raw_sessions):
                    errors.append("invalid session record")
            else:
                errors.append("backup sessions must be an array")
            raw_files = manifest.get("files", {})
            if isinstance(raw_files, dict):
                files = {
                    name: expected
                    for name, expected in raw_files.items()
                    if isinstance(name, str)
                }
                if len(files) != len(raw_files):
                    errors.append("invalid file name record")
            else:
                errors.append("backup files must be an object")
            if schema_version != BACKUP_SCHEMA_VERSION:
                errors.append(f"unsupported schema_version={schema_version}")
            for name, expected in files.items():
                if name == BACKUP_MANIFEST:
                    errors.append("backup manifest cannot list itself")
                    continue
                try:
                    data = archive.read(name)
                except KeyError:
                    errors.append(f"missing member: {name}")
                    continue
                if not isinstance(expected, dict):
                    errors.append(f"invalid file record: {name}")
                    continue
                if len(data) != expected.get("bytes") or _sha256(data) != expected.get(
                    "sha256"
                ):
                    errors.append(f"checksum mismatch: {name}")
            extras = set(names) - set(files) - {BACKUP_MANIFEST}
            if extras:
                errors.append(f"unrecorded members: {','.join(sorted(extras))}")
    except (NotImplementedError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        errors.append(f"archive unreadable: {exc}")

    latest = max(
        (str(session.get("updated_at")) for session in sessions if session.get("updated_at")),
        default=None,
    )
    return BackupInspection(
        path=source,
        healthy=not errors,
        schema_version=schema_version,
        created_at=created_at,
        app_version=archive_version,
        file_count=len(files),
        session_count=len(sessions),
        latest_session_at=latest,
        errors=tuple(errors),
    )
