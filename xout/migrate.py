"""레거시 ~/.claude/popper 데이터를 ~/.claude/xout으로 1회성 이관한다.

이관은 새 홈이 없고 레거시 홈이 있을 때만 일어난다. 디렉토리 이동과 산출물
파일명 교체는 xout 소유 데이터라 무조건 수행하고, 사용자 CLAUDE.md의 @import
한 줄은 activation receipt가 소유를 증명하는 바이트 그대로일 때만 새 경로로
바꿔 쓴다. 증명이 어긋나면 사용자 파일은 건드리지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .atomic import atomic_write_bytes, atomic_write_text
from .compiler import SETTINGS_JSON, XOUT_MD, _canonical
from .writer import ACTIVATION_RECEIPT, ACTIVATION_SCHEMA_VERSION, OwnedWriter

logger = logging.getLogger("xout")

LEGACY_HOME_NAME = "popper"
CURRENT_HOME_NAME = "xout"
LEGACY_OUTPUT_NAMES = {
    "POPPER.md": XOUT_MD,
    "settings.popper.json": SETTINGS_JSON,
}


@dataclass(frozen=True)
class MigrationResult:
    moved: bool = False
    renamed_files: tuple[str, ...] = ()
    import_rewritten: bool = False
    reason: str = "not-needed"


def migrate_legacy_home(claude_dir: Path | None = None) -> MigrationResult:
    """레거시 홈이 있으면 새 홈으로 옮긴다(멱등 - 새 홈이 있으면 no-op)."""
    claude_dir = (claude_dir if claude_dir is not None else Path.home() / ".claude").expanduser()
    legacy = claude_dir / LEGACY_HOME_NAME
    current = claude_dir / CURRENT_HOME_NAME
    if current.exists() or not legacy.is_dir():
        return MigrationResult()
    legacy.rename(current)
    renamed: list[str] = []
    for old_name, new_name in LEGACY_OUTPUT_NAMES.items():
        source = current / old_name
        if source.is_file() and not (current / new_name).exists():
            source.rename(current / new_name)
            renamed.append(new_name)
    rewritten = _rewrite_owned_import(current)
    logger.info("레거시 데이터 이관 완료: %s -> %s", legacy, current)
    return MigrationResult(True, tuple(renamed), rewritten, "migrated")


def _rewrite_owned_import(base_dir: Path) -> bool:
    """receipt가 증명한 레거시 @import 한 줄만 새 경로로 바꿔 쓴다."""
    receipt_path = base_dir / ACTIVATION_RECEIPT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    prefix_length = receipt.get("prefix_length")
    prefix_sha256 = receipt.get("prefix_sha256")
    if not (
        isinstance(receipt, dict)
        and receipt.get("artifact") == "popper_activation_receipt"
        and receipt.get("schema_version") == ACTIVATION_SCHEMA_VERSION
        and receipt.get("state") == "added"
        and isinstance(receipt.get("line"), str)
        and isinstance(receipt.get("target"), str)
        and isinstance(prefix_length, int)
        and not isinstance(prefix_length, bool)
        and prefix_length >= 0
        and isinstance(prefix_sha256, str)
        and isinstance(receipt.get("leading_newline"), bool)
        and receipt.get("newline") in {"lf", "crlf"}
    ):
        return False
    target = Path(receipt["target"])
    try:
        data = target.read_bytes()
    except OSError:
        return False
    newline = b"\r\n" if receipt["newline"] == "crlf" else b"\n"
    lead = newline if receipt["leading_newline"] else b""
    old_insertion = lead + receipt["line"].encode("utf-8") + newline
    end = prefix_length + len(old_insertion)
    if (
        prefix_length > len(data)
        or hashlib.sha256(data[:prefix_length]).hexdigest() != prefix_sha256
        or data[prefix_length:end] != old_insertion
    ):
        logger.warning("레거시 @import 소유 증명 불일치 - CLAUDE.md는 건드리지 않는다")
        return False
    new_line = OwnedWriter(base_dir=base_dir, claude_md_path=target).import_line()
    if new_line == receipt["line"]:
        return False
    new_insertion = lead + new_line.encode("utf-8") + newline
    atomic_write_bytes(target, data[:prefix_length] + new_insertion + data[end:])
    receipt["line"] = new_line
    atomic_write_text(receipt_path, _canonical(receipt))
    logger.info("소유 @import 경로 갱신: %s", new_line)
    return True
