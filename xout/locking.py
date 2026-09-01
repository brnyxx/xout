"""Windows/macOS/Linux에서 동작하는 재진입 가능 프로세스 파일 잠금."""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import BinaryIO

LOCK_TIMEOUT_SECONDS = 60.0
LOCK_RETRY_SECONDS = 0.05


class LockTimeout(TimeoutError):
    """정해진 시간 안에 프로세스 잠금을 얻지 못했다."""


class ProcessFileLock:
    """프로세스 간 advisory lock과 프로세스 내 RLock을 결합한다."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self._thread_lock = RLock()
        self._depth = 0
        self._stream: BinaryIO | None = None

    def __enter__(self) -> ProcessFileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> None:
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stream = self.path.open("a+b")
                try:
                    _acquire_os_lock(stream, self.path, self.timeout)
                except BaseException:
                    stream.close()
                    raise
                self._stream = stream
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._depth <= 0:
            raise RuntimeError("획득하지 않은 파일 잠금을 해제할 수 없다")
        try:
            self._depth -= 1
            if self._depth == 0:
                stream = self._stream
                self._stream = None
                if stream is not None:
                    try:
                        _release_os_lock(stream)
                    finally:
                        stream.close()
        finally:
            self._thread_lock.release()


def _acquire_os_lock(stream: BinaryIO, path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                _windows_lock(stream)
            else:
                _posix_lock(stream)
            return
        except (BlockingIOError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise LockTimeout(f"파일 잠금 대기 시간 초과: {path}") from exc
            time.sleep(LOCK_RETRY_SECONDS)


def _posix_lock(stream: BinaryIO) -> None:
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _windows_lock(stream: BinaryIO) -> None:
    import msvcrt

    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)


def _release_os_lock(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


_registry: dict[Path, ProcessFileLock] = {}
_registry_guard = Lock()


def lock_for_path(path: Path | str) -> ProcessFileLock:
    """정규화된 잠금 파일마다 프로세스 내 인스턴스를 하나만 만든다."""
    lock_path = Path(path).expanduser().resolve()
    with _registry_guard:
        lock = _registry.get(lock_path)
        if lock is None:
            lock = ProcessFileLock(lock_path)
            _registry[lock_path] = lock
        return lock


def base_lock(base_dir: Path | str) -> ProcessFileLock:
    """이벤트와 파생 산출물 전체를 직렬화하는 소유 디렉토리 잠금."""
    base = Path(base_dir).expanduser().resolve()
    return lock_for_path(base.parent / f".{base.name}.popper.lock")


def target_lock(target: Path | str) -> ProcessFileLock:
    """소유 디렉토리 밖 단일 사용자 파일을 보호하는 형제 잠금."""
    path = Path(target).expanduser().resolve()
    return lock_for_path(path.with_name(f".{path.name}.popper.lock"))


def base_runtime_lock(
    base_dir: Path | str, *, timeout: float = 0.1
) -> ProcessFileLock:
    """한 소유 디렉토리의 admission 판정과 서버 수명주기를 직렬화한다."""
    base = Path(base_dir).expanduser().resolve()
    return ProcessFileLock(
        base.parent / f".{base.name}.runtime.lock",
        timeout=timeout,
    )
