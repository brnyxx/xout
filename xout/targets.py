"""타깃 - 컴파일된 규칙을 어느 에이전트의 지시 파일에 어떻게 붙이는가.

규칙 8줄은 도구 중립 마크다운이다. 도구마다 다른 것은 "표준 지시 파일이
어디인가"와 "다른 파일을 끌어올 수 있는가"뿐이다.

- import 모드: 지시 파일이 다른 파일을 끌어올 수 있으면 한 줄만 넣는다
  (Claude Code의 `@path`). 기존 OwnedWriter가 담당한다.
- block 모드: 그렇지 않으면 XOUT.md 본문을 마커로 감싼 소유 블록으로
  붙인다. 마커 사이만 xout 소유이고, 되돌림은 정확히 그 블록만 지운다.

두 모드 모두 허가 레코드(subject = 대상 파일 경로)가 있어야 쓰고, 쓰기 전에
세이브포인트를 남기며, 영수증(prefix 해시)으로 소유권 드리프트를 잡는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from xout.conflict import ConsentKind, ConsentRecord
from xout.savepoint import atomic_write_bytes, create as create_savepoint

BLOCK_BEGIN_PREFIX = "<!-- xout:begin"
BLOCK_BEGIN = BLOCK_BEGIN_PREFIX + " sha256={digest} -->"
BLOCK_END = "<!-- xout:end -->"
BLOCK_NOTE = "<!-- managed by xout - edit XOUT.md, not this block; remove with: xout undo -->"
_BLOCK_RE = re.compile(
    r"(?:\r?\n)?" + re.escape(BLOCK_BEGIN_PREFIX) + r"[^\n]*\n.*?" + re.escape(BLOCK_END) + r"(?:\r?\n)?",
    re.S,
)

MODE_IMPORT = "import"
MODE_BLOCK = "block"
SCOPE_USER = "user"
SCOPE_PROJECT = "project"

BLOCK_ADDED = "added"
BLOCK_UPDATED = "updated"
BLOCK_ALREADY_PRESENT = "already_present"
BLOCK_REMOVED = "removed"
BLOCK_NOT_PRESENT = "not_present"
BLOCK_NO_PERMISSION = "permission_missing"
BLOCK_SUBJECT_MISMATCH = "permission_subject_mismatch"
BLOCK_OWNERSHIP_DRIFT = "ownership_drift"


@dataclass(frozen=True, slots=True)
class Target:
    """한 에이전트의 지시 파일 규약 - 공식 문서에서 확인된 것만 verified=True."""

    target_id: str
    name: str
    scope: str
    relative_path: str  # user 스코프면 홈 기준, project 스코프면 프로젝트 루트 기준
    mode: str
    doc_url: str
    verified: bool = True
    note: str = ""
    preamble: str = ""  # xout이 파일을 새로 만들 때만 앞에 붙는 내용 (예: steering frontmatter)

    def resolve(self, home: Path, project: Path) -> Path:
        root = home if self.scope == SCOPE_USER else project
        return (root / self.relative_path).expanduser().resolve()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "name": self.name,
            "scope": self.scope,
            "path": self.relative_path,
            "mode": self.mode,
            "doc_url": self.doc_url,
            "verified": self.verified,
            "note": self.note,
            "preamble": self.preamble,
        }


@dataclass(frozen=True, slots=True)
class BlockOutcome:
    path: Path
    changed: bool
    reason: str
    savepoint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "changed": self.changed,
            "reason": self.reason,
            "savepoint_id": self.savepoint_id,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_block(xout_md: str) -> str:
    body = xout_md.rstrip("\n")
    return "\n".join([BLOCK_BEGIN.format(digest=_digest(body)), BLOCK_NOTE, body, BLOCK_END])


def find_block(text: str) -> re.Match[str] | None:
    return _BLOCK_RE.search(text)


def receipt_path(base_dir: Path, target_id: str) -> Path:
    return base_dir / f"activation.{target_id}.json"


def _permission_error(permission: ConsentRecord | None, target: Path) -> str | None:
    if permission is None:
        return BLOCK_NO_PERMISSION
    if permission.kind is not ConsentKind.IMPORT_PERMISSION_GRANTED:
        return BLOCK_NO_PERMISSION
    if Path(permission.subject).expanduser().resolve() != target:
        return BLOCK_SUBJECT_MISMATCH
    return None


def _load_receipt(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def ensure_block(
    base_dir: Path,
    target_id: str,
    target: Path,
    xout_md: str,
    permission: ConsentRecord | None,
    preamble: str = "",
) -> BlockOutcome:
    """소유 블록을 파일 끝에 붙이거나(없으면) 갱신한다(해시가 다르면). 멱등."""
    block = render_block(xout_md)
    existed = target.exists()
    text = target.read_text(encoding="utf-8") if existed else ""
    match = find_block(text)
    if match and match.group(0).strip() == block:
        return BlockOutcome(target, False, BLOCK_ALREADY_PRESENT)
    error = _permission_error(permission, target)
    if error is not None:
        return BlockOutcome(target, False, error)
    savepoint = create_savepoint(base_dir, [target], f"enable {target_id}")
    if match:
        new_text = text[: match.start()] + ("\n" if match.start() and not text[: match.start()].endswith("\n") else "") + block + "\n" + text[match.end():]
        reason = BLOCK_UPDATED
    else:
        if not existed and preamble:
            text = preamble.rstrip("\n") + "\n"
        separator = "" if (not text or text.endswith("\n")) else "\n"
        new_text = text + separator + ("\n" if text else "") + block + "\n"
        reason = BLOCK_ADDED
    receipt = {
        "artifact": "xout_activation_receipt",
        "target_id": target_id,
        "mode": MODE_BLOCK,
        "target": str(target),
        "created_file": not existed,
        "preamble": preamble if not existed else "",
        "block_sha256": _digest(xout_md.rstrip("\n")),
        "savepoint_id": savepoint.savepoint_id,
    }
    atomic_write_bytes(target, new_text.encode("utf-8"))
    atomic_write_bytes(
        receipt_path(base_dir, target_id),
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return BlockOutcome(target, True, reason, savepoint.savepoint_id)


def remove_block(base_dir: Path, target_id: str, target: Path) -> BlockOutcome:
    """마커로 감싼 소유 블록만 지운다. xout이 만든 파일이 비게 되면 파일도 지운다."""
    if not target.exists():
        _clear_receipt(base_dir, target_id)
        return BlockOutcome(target, False, BLOCK_NOT_PRESENT)
    text = target.read_text(encoding="utf-8")
    match = find_block(text)
    if match is None:
        _clear_receipt(base_dir, target_id)
        return BlockOutcome(target, False, BLOCK_NOT_PRESENT)
    receipt = _load_receipt(receipt_path(base_dir, target_id)) or {}
    savepoint = create_savepoint(base_dir, [target], f"undo {target_id}")
    new_text = text[: match.start()] + text[match.end():]
    if match.start() and not new_text.endswith("\n") and new_text.strip():
        new_text += "\n"
    leftover = new_text.strip()
    created_preamble = str(receipt.get("preamble") or "").strip()
    if receipt.get("created_file") and leftover in ("", created_preamble):
        target.unlink()
    else:
        atomic_write_bytes(target, new_text.encode("utf-8"))
    _clear_receipt(base_dir, target_id)
    return BlockOutcome(target, True, BLOCK_REMOVED, savepoint.savepoint_id)


def _clear_receipt(base_dir: Path, target_id: str) -> None:
    path = receipt_path(base_dir, target_id)
    if path.exists():
        path.unlink()


def block_state(base_dir: Path, target_id: str, target: Path) -> dict[str, Any]:
    """status용 - 파일에 블록이 있는가, 영수증과 맞는가."""
    receipt = _load_receipt(receipt_path(base_dir, target_id))
    present = False
    if target.exists():
        present = find_block(target.read_text(encoding="utf-8")) is not None
    return {
        "target_id": target_id,
        "path": str(target),
        "active": present,
        "receipt": receipt is not None,
    }


def targets_by_id(registry: Mapping[str, Target], ids: list[str] | None) -> list[Target]:
    if not ids or ids == ["all"]:
        return list(registry.values())
    missing = [t for t in ids if t not in registry]
    if missing:
        raise KeyError(", ".join(missing))
    return [registry[t] for t in ids]


#: 활성화 타깃 레지스트리 - 사용자 레벨 지시 파일. 경로는 공식 문서에서 확인된 것만 싣는다.
#: 문서에서 규약을 확인하지 못한 도구(gajae-code)는 등록하지 않는다 - 프로젝트 AGENTS.md가 폴백.
REGISTRY: dict[str, Target] = {
    "claude": Target(
        target_id="claude",
        name="Claude Code",
        scope=SCOPE_USER,
        relative_path=".claude/CLAUDE.md",
        mode=MODE_IMPORT,
        doc_url="https://code.claude.com/docs/en/memory",
        note="one owned @import line; the rules stay in XOUT.md",
    ),
    "codex": Target(
        target_id="codex",
        name="OpenAI Codex CLI",
        scope=SCOPE_USER,
        relative_path=".codex/AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://learn.chatgpt.com/docs/agent-configuration/agents-md",
        note="global AGENTS.md is read first; project AGENTS.md files come later and override",
    ),
    "opencode": Target(
        target_id="opencode",
        name="OpenCode",
        scope=SCOPE_USER,
        relative_path=".config/opencode/AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://opencode.ai/docs/rules/",
        note="when this file exists OpenCode stops falling back to ~/.claude/CLAUDE.md",
    ),
    "gemini": Target(
        target_id="gemini",
        name="Gemini CLI",
        scope=SCOPE_USER,
        relative_path=".gemini/GEMINI.md",
        mode=MODE_BLOCK,
        doc_url="https://geminicli.com/docs/cli/gemini-md/",
    ),
    "copilot": Target(
        target_id="copilot",
        name="GitHub Copilot CLI",
        scope=SCOPE_USER,
        relative_path=".copilot/copilot-instructions.md",
        mode=MODE_BLOCK,
        doc_url="https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions",
        note="Copilot CLI user instructions; the coding agent and VS Code read repository files instead (see the agents target)",
    ),
    "pi": Target(
        target_id="pi",
        name="pi coding agent",
        scope=SCOPE_USER,
        relative_path=".pi/agent/AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md",
    ),
    "omp": Target(
        target_id="omp",
        name="oh-my-pi",
        scope=SCOPE_USER,
        relative_path=".omp/agent/AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://github.com/can1357/oh-my-pi/blob/main/docs/context-files.md",
        note="oh-my-pi also reads ~/.claude/CLAUDE.md, so the claude target already reaches it",
    ),
    "gjc": Target(
        target_id="gjc",
        name="gajae-code",
        scope=SCOPE_USER,
        relative_path=".gjc/agent/AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://github.com/Yeachan-Heo/gajae-code/blob/main/docs/customization.md",
        note="path confirmed from the installed package source (@gajae-code/coding-agent 0.15.6, system-prompt.d.ts), not from its public docs",
    ),
    "kiro": Target(
        target_id="kiro",
        name="Kiro",
        scope=SCOPE_USER,
        relative_path=".kiro/steering/xout.md",
        mode=MODE_BLOCK,
        doc_url="https://kiro.dev/docs/steering/",
        note="a steering file owned entirely by xout; workspace steering wins on conflict",
        preamble="---\ninclusion: always\n---\n",
    ),
    "agents": Target(
        target_id="agents",
        name="AGENTS.md in this project",
        scope=SCOPE_PROJECT,
        relative_path="AGENTS.md",
        mode=MODE_BLOCK,
        doc_url="https://agents.md",
        note="read by Codex, OpenCode, pi, oh-my-pi, Copilot, Cursor and Kiro at project level",
    ),
}
