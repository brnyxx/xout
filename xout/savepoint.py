"""세이브포인트 - xout이 소유 디렉토리 밖 파일을 건드리기 전에 남기는 되돌림 지점.

스냅샷은 `~/.claude/xout/savepoints/<id>/` 안에 원본 바이트 그대로 저장되고,
`restore`는 저장된 파일만 저장된 내용으로 되돌린다. 스냅샷 당시 없던 파일은
복원 때 지우지 않고 그대로 두어 보고만 한다 - 삭제는 되돌림이 아니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SAVEPOINT_DIR = "savepoints"
SAVEPOINT_MANIFEST = "savepoint.json"


class SavepointError(RuntimeError):
    """존재하지 않는 세이브포인트, 깨진 매니페스트 등."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """같은 디렉토리에 임시 파일을 쓰고 교체한다 - 중간 상태가 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class SavedFile:
    path: str
    existed: bool
    sha256: str | None
    stored_as: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "existed": self.existed,
            "sha256": self.sha256,
            "stored_as": self.stored_as,
        }


@dataclass(frozen=True, slots=True)
class Savepoint:
    savepoint_id: str
    created_at: str
    reason: str
    files: tuple[SavedFile, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "xout_savepoint",
            "savepoint_id": self.savepoint_id,
            "created_at": self.created_at,
            "reason": self.reason,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    path: str
    action: str  # restored | unchanged | left_in_place | missing_snapshot

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "action": self.action}


def _directory(base_dir: Path) -> Path:
    return base_dir / SAVEPOINT_DIR


def create(
    base_dir: Path,
    paths: Sequence[Path],
    reason: str,
    now: str | None = None,
) -> Savepoint:
    """주어진 파일들의 현재 바이트를 스냅샷한다 - 없는 파일도 '없었음'으로 기록."""
    created_at = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamp = created_at.replace(":", "").replace("-", "").split("+")[0]
    resolved = [Path(p).expanduser().resolve() for p in paths]
    digest = _sha256("\n".join(str(p) for p in resolved).encode("utf-8"))[:8]
    savepoint_id = f"sp-{stamp}-{digest}"
    directory = _directory(base_dir) / savepoint_id
    counter = 1
    while directory.exists():
        counter += 1
        directory = _directory(base_dir) / f"{savepoint_id}-{counter}"
    savepoint_id = directory.name
    files_dir = directory / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    saved: list[SavedFile] = []
    for index, path in enumerate(resolved):
        if path.is_file():
            payload = path.read_bytes()
            stored = f"{index:03d}-{path.name}"
            (files_dir / stored).write_bytes(payload)
            saved.append(SavedFile(str(path), True, _sha256(payload), stored))
        else:
            saved.append(SavedFile(str(path), False, None, None))
    savepoint = Savepoint(savepoint_id, created_at, reason, tuple(saved))
    atomic_write_bytes(
        directory / SAVEPOINT_MANIFEST,
        (json.dumps(savepoint.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return savepoint


def _load(directory: Path) -> Savepoint:
    try:
        document = json.loads((directory / SAVEPOINT_MANIFEST).read_text(encoding="utf-8"))
        files = tuple(
            SavedFile(f["path"], bool(f["existed"]), f.get("sha256"), f.get("stored_as"))
            for f in document["files"]
        )
        return Savepoint(document["savepoint_id"], document["created_at"], document.get("reason", ""), files)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SavepointError(f"broken savepoint: {directory.name}") from exc


def list_savepoints(base_dir: Path) -> list[Savepoint]:
    directory = _directory(base_dir)
    if not directory.is_dir():
        return []
    out: list[Savepoint] = []
    for child in sorted(directory.iterdir()):
        if (child / SAVEPOINT_MANIFEST).is_file():
            out.append(_load(child))
    return out


def load(base_dir: Path, savepoint_id: str) -> Savepoint:
    directory = _directory(base_dir) / savepoint_id
    if not (directory / SAVEPOINT_MANIFEST).is_file():
        raise SavepointError(f"unknown savepoint: {savepoint_id}")
    return _load(directory)


def restore(base_dir: Path, savepoint_id: str) -> list[RestoreResult]:
    """저장된 파일을 저장된 바이트로 되돌린다. 스냅샷 당시 없던 파일은 건드리지 않는다."""
    savepoint = load(base_dir, savepoint_id)
    files_dir = _directory(base_dir) / savepoint_id / "files"
    results: list[RestoreResult] = []
    for saved in savepoint.files:
        target = Path(saved.path)
        if not saved.existed:
            results.append(RestoreResult(saved.path, "left_in_place" if target.exists() else "unchanged"))
            continue
        source = files_dir / (saved.stored_as or "")
        if not source.is_file():
            results.append(RestoreResult(saved.path, "missing_snapshot"))
            continue
        payload = source.read_bytes()
        if _sha256(payload) != saved.sha256:
            raise SavepointError(f"snapshot hash mismatch: {saved.path}")
        if target.is_file() and target.read_bytes() == payload:
            results.append(RestoreResult(saved.path, "unchanged"))
            continue
        atomic_write_bytes(target, payload)
        results.append(RestoreResult(saved.path, "restored"))
    return results
