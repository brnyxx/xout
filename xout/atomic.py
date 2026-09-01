"""같은 디렉토리 임시 파일과 ``os.replace``를 쓰는 원자 파일 교체."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

WINDOWS_REPLACE_TIMEOUT_SECONDS = 5.0
WINDOWS_REPLACE_RETRY_SECONDS = 0.01


def _sync_directory(path: Path) -> None:
    """지원하는 플랫폼에서는 디렉토리 엔트리까지 내구성 있게 동기화한다."""
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _replace(temporary: Path, target: Path) -> None:
    deadline = time.monotonic() + WINDOWS_REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(WINDOWS_REPLACE_RETRY_SECONDS)


def atomic_write_bytes(target: Path | str, data: bytes) -> Path:
    """독자가 이전 또는 새 바이트만 보도록 파일 하나를 원자 교체한다."""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(target: Path | str, text: str) -> Path:
    """UTF-8 텍스트를 원자 교체한다."""
    return atomic_write_bytes(target, text.encode("utf-8"))
