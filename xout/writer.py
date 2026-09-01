"""AC5 - 쓰기 권한 분리 writer.

Popper는 자기 소유 디렉토리(~/.claude/popper/) 밖에 절대 쓰지 않는다.
사용자 CLAUDE.md 본문과 라이브 settings.json은 무변경이며, 유일한 예외인
@import 한 줄은 import_permission_granted 동의 레코드가 인자로 전달될 때만
파일 끝에 추가된다(멱등). manifest에 기록된 마지막 쓰기 content hash와
디스크 내용이 불일치하면(수기 편집) silent overwrite 대신 감지 신호
(최강 strike 신호)를 반환하고 쓰기를 전면 중단한다.

성공적으로 추가한 @import의 위치와 prefix hash는 activation receipt에 먼저
기록한다. 롤백은 receipt가 증명한 occurrence 하나만 제거하며, 기존 사용자
import나 prefix drift는 건드리지 않는다.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from xout.atomic import atomic_write_bytes, atomic_write_text
from xout.compiler import (
    MANIFEST_JSON,
    MANIFEST_VERSION,
    XOUT_MD,
    SETTINGS_JSON,
    content_hash,
    default_base_dir,
)
from xout.conflict import ConsentKind, ConsentRecord
from xout.locking import base_lock, target_lock

logger = logging.getLogger(__name__)

# manifest가 content hash를 기록/대조하는 산출물 (manifest.json 자신은 제외)
HASHED_OUTPUTS = (XOUT_MD, SETTINGS_JSON)

# 수기 편집 감지 신호 - 사용자가 규칙 본문을 직접 고쳤다는 최강 strike 신호
MANUAL_EDIT_STRIKE = "manual_edit_strike"

DETECT_MANUAL_EDIT = "manual_edit"
DETECT_MISSING = "missing"
DETECT_UNREADABLE_MANIFEST = "unreadable_manifest"

IMPORT_ADDED = "added"
IMPORT_REMOVED = "removed"
IMPORT_ALREADY_PRESENT = "already_present"
IMPORT_NOT_PRESENT = "not_present"
IMPORT_NO_PERMISSION = "permission_missing"
IMPORT_INVALID_PERMISSION = "invalid_permission"
IMPORT_SUBJECT_MISMATCH = "permission_subject_mismatch"
IMPORT_TARGET_MISSING = "claude_md_missing"
IMPORT_NOT_OWNED = "not_owned"
IMPORT_OWNERSHIP_DRIFT = "ownership_drift"

ACTIVATION_RECEIPT = "activation.json"
ACTIVATION_SCHEMA_VERSION = 1


class OwnershipViolation(RuntimeError):
    """소유 디렉토리 밖 또는 보호된 사용자 파일에 대한 쓰기 시도."""


def _now(now: str | None = None) -> str:
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class ManualEditDetection:
    """manifest 기록 해시와 디스크 내용의 불일치 - 수기 편집 감지 한 건."""

    path: str
    recorded_hash: str | None
    actual_hash: str | None
    reason: str
    signal: str = MANUAL_EDIT_STRIKE
    detected_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "path": self.path,
            "recorded_hash": self.recorded_hash,
            "actual_hash": self.actual_hash,
            "reason": self.reason,
            "detected_at": self.detected_at,
        }


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """write_outputs 반환값 - 착지 경로 또는 수기 편집 감지 신호."""

    base_dir: Path
    written: tuple[Path, ...] = ()
    detections: tuple[ManualEditDetection, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.detections)


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """ensure_import/remove_import 반환값."""

    path: Path
    line: str
    changed: bool
    reason: str


class OwnedWriter:
    """Popper 단독 소유 디렉토리 writer.

    모든 쓰기는 base_dir 내부로 강제되고, 사용자 CLAUDE.md와 라이브
    settings.json은 쓰기 대상에서 원천 차단된다. 유일한 사용자 파일 변경은
    ensure_import의 @import 한 줄 추가뿐이며 허가 레코드를 요구한다.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        claude_md_path: Path | None = None,
        live_settings_path: Path | None = None,
    ) -> None:
        home_claude = Path.home() / ".claude"
        self.base_dir = (
            base_dir if base_dir is not None else default_base_dir()
        ).resolve()
        self.claude_md_path = (
            claude_md_path if claude_md_path is not None else home_claude / "CLAUDE.md"
        ).resolve()
        self.live_settings_path = (
            live_settings_path
            if live_settings_path is not None
            else home_claude / "settings.json"
        ).resolve()

    def path(self, name: str) -> Path:
        return self.base_dir / name

    def import_line(self) -> str:
        """CLAUDE.md에 들어갈 @import 한 줄 - XOUT.md 착지 경로의 순수 함수."""
        target = self.base_dir / XOUT_MD
        try:
            rel = target.relative_to(Path.home())
        except ValueError:
            return f"@{target.as_posix()}"
        return f"@~/{rel.as_posix()}"

    def _guard(self, name: str | Path) -> Path:
        """쓰기 대상 경로를 소유 디렉토리 내부로 강제한다."""
        candidate = Path(name)
        target = candidate if candidate.is_absolute() else self.base_dir / candidate
        resolved = target.resolve()
        if resolved == self.base_dir or not resolved.is_relative_to(self.base_dir):
            raise OwnershipViolation(f"소유 디렉토리 밖 쓰기 거부: {resolved}")
        if resolved in (self.claude_md_path, self.live_settings_path):
            raise OwnershipViolation(f"보호된 사용자 파일 쓰기 거부: {resolved}")
        return resolved

    def write_file(self, name: str | Path, body: str) -> Path:
        """소유 디렉토리 내부에만 쓴다 - 밖이면 OwnershipViolation."""
        target = self._guard(name)
        with base_lock(self.base_dir):
            return atomic_write_text(target, body)

    def detect_manual_edits(self) -> tuple[ManualEditDetection, ...]:
        """manifest의 마지막 쓰기 해시와 디스크 내용을 대조한다."""
        manifest_path = self.base_dir / MANIFEST_JSON
        if not manifest_path.exists():
            return ()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("manifest 파싱 실패: %s", manifest_path, exc_info=True)
            return (
                ManualEditDetection(
                    path=str(manifest_path),
                    recorded_hash=None,
                    actual_hash=None,
                    reason=DETECT_UNREADABLE_MANIFEST,
                ),
            )

        detections: list[ManualEditDetection] = []
        outputs = manifest.get("outputs", {})
        if not isinstance(outputs, Mapping):
            outputs = {}
        for name in HASHED_OUTPUTS:
            entry = outputs.get(name)
            if not isinstance(entry, Mapping):
                continue
            recorded = entry.get("content_hash")
            if not recorded:
                continue
            target = self.base_dir / name
            if not target.exists():
                detections.append(
                    ManualEditDetection(
                        path=str(target),
                        recorded_hash=str(recorded),
                        actual_hash=None,
                        reason=DETECT_MISSING,
                    )
                )
                continue
            actual = content_hash(target.read_text(encoding="utf-8"))
            if actual != recorded:
                detections.append(
                    ManualEditDetection(
                        path=str(target),
                        recorded_hash=str(recorded),
                        actual_hash=actual,
                        reason=DETECT_MANUAL_EDIT,
                    )
                )
        return tuple(detections)

    def _write_outputs_unlocked(
        self,
        documents: Mapping[str, str],
        *,
        now: str | None = None,
    ) -> WriteOutcome:
        """산출물을 소유 디렉토리에 쓴다 - 수기 편집 감지 시 쓰기 전면 중단."""
        unknown = set(documents) - set(HASHED_OUTPUTS)
        if unknown:
            raise OwnershipViolation(f"소유 계약 밖 산출물 이름: {sorted(unknown)}")

        detections = self.detect_manual_edits()
        if detections:
            for detection in detections:
                logger.warning(
                    "수기 편집 감지 - silent overwrite 중단: %s (%s)",
                    detection.path,
                    detection.reason,
                )
            return WriteOutcome(base_dir=self.base_dir, detections=detections)

        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "generated_at": _now(now),
            "owned_dir": str(self.base_dir),
            "outputs": {
                name: {"content_hash": content_hash(body)}
                for name, body in documents.items()
            },
        }
        written: list[Path] = []
        for name, body in documents.items():
            written.append(self.write_file(name, body))
        written.append(self.write_file(MANIFEST_JSON, _canonical(manifest)))
        logger.info("popper 산출물 착지: %s", self.base_dir)
        return WriteOutcome(base_dir=self.base_dir, written=tuple(written))

    def write_outputs(
        self,
        documents: Mapping[str, str],
        *,
        now: str | None = None,
    ) -> WriteOutcome:
        """감지와 전체 산출물 교체를 하나의 프로세스 간 임계 구역에서 수행한다."""
        with base_lock(self.base_dir):
            return self._write_outputs_unlocked(documents, now=now)

    @property
    def activation_receipt_path(self) -> Path:
        return self.base_dir / ACTIVATION_RECEIPT

    def _write_activation_receipt_unlocked(self, receipt: Mapping[str, Any]) -> None:
        atomic_write_text(self.activation_receipt_path, _canonical(receipt))

    def _load_activation_receipt_unlocked(
        self,
    ) -> tuple[dict[str, Any] | None, str | None]:
        path = self.activation_receipt_path
        if not path.exists():
            return None, None
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("activation receipt를 읽지 못했다: %s", path)
            return None, IMPORT_OWNERSHIP_DRIFT
        if not isinstance(decoded, dict):
            return None, IMPORT_OWNERSHIP_DRIFT
        return decoded, None

    def _clear_activation_receipt_unlocked(self) -> None:
        path = self.activation_receipt_path
        try:
            path.unlink()
        except FileNotFoundError:
            return
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _prefix_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _ensure_import_unlocked(
        self, permission: ConsentRecord | None = None
    ) -> ImportOutcome:
        """@import 한 줄을 CLAUDE.md 끝에 추가한다(멱등).

        import_permission_granted 동의 레코드(subject=CLAUDE.md 경로)가 전달될
        때만 쓴다. 그 외 어떤 경우에도 사용자 파일 바이트를 건드리지 않는다.
        """
        line = self.import_line()
        target = self.claude_md_path
        encoded = line.encode("utf-8")

        def permission_error() -> str | None:
            if permission is None:
                return IMPORT_NO_PERMISSION
            if (
                not isinstance(permission, ConsentRecord)
                or permission.kind is not ConsentKind.IMPORT_PERMISSION_GRANTED
            ):
                logger.warning("import 허가가 아닌 레코드 거부: %r", permission)
                return IMPORT_INVALID_PERMISSION
            subject = Path(permission.subject).expanduser().resolve()
            if subject != target:
                logger.warning(
                    "import 허가 대상 불일치: 허가=%s, 대상=%s", subject, target
                )
                return IMPORT_SUBJECT_MISMATCH
            return None

        existed = target.exists()
        data = target.read_bytes() if existed else b""
        if encoded in data.splitlines():
            return ImportOutcome(
                path=target, line=line, changed=False, reason=IMPORT_ALREADY_PRESENT
            )
        reason = permission_error()
        if reason is not None:
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=(
                    IMPORT_TARGET_MISSING
                    if not existed and reason == IMPORT_NO_PERMISSION
                    else reason
                ),
            )

        newline = b"\r\n" if b"\r\n" in data else b"\n"
        separator = b"" if (not data or data.endswith((b"\n", b"\r"))) else newline
        receipt = {
            "artifact": "popper_activation_receipt",
            "schema_version": ACTIVATION_SCHEMA_VERSION,
            "state": "prepared",
            "target": str(target),
            "line": line,
            "created_file": not existed,
            "prefix_length": len(data),
            "prefix_sha256": self._prefix_hash(data),
            "leading_newline": bool(separator),
            "newline": "crlf" if newline == b"\r\n" else "lf",
        }
        self._write_activation_receipt_unlocked(receipt)
        if target.exists() != existed or (existed and target.read_bytes() != data):
            self._clear_activation_receipt_unlocked()
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=IMPORT_OWNERSHIP_DRIFT,
            )
        atomic_write_bytes(target, data + separator + encoded + newline)
        receipt["state"] = "added"
        self._write_activation_receipt_unlocked(receipt)
        logger.info("CLAUDE.md 소유 @import 추가: %s", target)
        return ImportOutcome(path=target, line=line, changed=True, reason=IMPORT_ADDED)

    def ensure_import(self, permission: ConsentRecord | None = None) -> ImportOutcome:
        """CLAUDE.md와 소유 receipt를 동일 잠금 순서 안에서 갱신한다."""
        with base_lock(self.base_dir):
            with target_lock(self.claude_md_path):
                return self._ensure_import_unlocked(permission)

    def _remove_import_unlocked(self) -> ImportOutcome:
        """@import 한 줄 제거 - 전체 롤백 지점. 그 한 줄만 걷어낸다."""
        line = self.import_line()
        target = self.claude_md_path
        encoded = line.encode("utf-8")
        receipt, receipt_error = self._load_activation_receipt_unlocked()
        if receipt_error is not None:
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=receipt_error,
            )
        if not target.exists():
            if receipt is not None:
                self._clear_activation_receipt_unlocked()
                reason = IMPORT_NOT_PRESENT
            else:
                reason = IMPORT_TARGET_MISSING
            return ImportOutcome(path=target, line=line, changed=False, reason=reason)

        data = target.read_bytes()
        if receipt is None:
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=(
                    IMPORT_NOT_OWNED
                    if encoded in data.splitlines()
                    else IMPORT_NOT_PRESENT
                ),
            )

        prefix_length = receipt.get("prefix_length")
        prefix_sha256 = receipt.get("prefix_sha256")
        leading_newline = receipt.get("leading_newline")
        newline_name = receipt.get("newline", "lf")
        valid_receipt = (
            receipt.get("artifact") == "popper_activation_receipt"
            and receipt.get("schema_version") == ACTIVATION_SCHEMA_VERSION
            and receipt.get("state") in {"prepared", "added"}
            and receipt.get("target") == str(target)
            and receipt.get("line") == line
            and isinstance(receipt.get("created_file"), bool)
            and isinstance(prefix_length, int)
            and not isinstance(prefix_length, bool)
            and prefix_length >= 0
            and isinstance(prefix_sha256, str)
            and len(prefix_sha256) == 64
            and all(character in "0123456789abcdef" for character in prefix_sha256)
            and isinstance(leading_newline, bool)
            and newline_name in {"lf", "crlf"}
        )
        if not valid_receipt:
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=IMPORT_OWNERSHIP_DRIFT,
            )
        if encoded not in data.splitlines():
            self._clear_activation_receipt_unlocked()
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=IMPORT_NOT_PRESENT,
            )
        if receipt.get("state") != "added":
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=IMPORT_OWNERSHIP_DRIFT,
            )

        newline = b"\r\n" if newline_name == "crlf" else b"\n"
        insertion = (newline if leading_newline else b"") + encoded + newline
        end = prefix_length + len(insertion)
        if (
            prefix_length > len(data)
            or self._prefix_hash(data[:prefix_length]) != prefix_sha256
            or data[prefix_length:end] != insertion
        ):
            return ImportOutcome(
                path=target,
                line=line,
                changed=False,
                reason=IMPORT_OWNERSHIP_DRIFT,
            )
        atomic_write_bytes(target, data[:prefix_length] + data[end:])
        self._clear_activation_receipt_unlocked()
        logger.info("CLAUDE.md @import 제거(롤백): %s", target)
        return ImportOutcome(
            path=target, line=line, changed=True, reason=IMPORT_REMOVED
        )

    def remove_import(self) -> ImportOutcome:
        """소유 receipt가 증명한 한 occurrence만 제거한다."""
        with base_lock(self.base_dir):
            with target_lock(self.claude_md_path):
                return self._remove_import_unlocked()
