"""리컨사일 - 기존 규칙 파일과 착지된 XOUT.md의 중복·모순을 정리한다.

보고는 항상 읽기전용이다. 패치는 소유 디렉토리 안에 파일로만 제안한다.
실제 적용(`--apply --grant`)은 중복 줄 제거뿐이며, 그 전에 반드시
세이브포인트를 만든다 - 모순 줄은 어느 쪽이 맞는지 사용자만 알기 때문에
목록으로 보여 주기만 하고 절대 손대지 않는다.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from xout.mine import Conflict, Duplicate, Observation, find_conflicts, find_duplicates
from xout.savepoint import Savepoint, atomic_write_bytes, create

RECONCILE_DIR = "reconcile"


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    duplicates: tuple[Duplicate, ...]
    conflicts: tuple[Conflict, ...]

    @property
    def files_to_edit(self) -> tuple[str, ...]:
        return tuple(sorted({d.abs_path for d in self.duplicates if d.abs_path}))

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicates": [d.to_dict() for d in self.duplicates],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "files_to_edit": list(self.files_to_edit),
        }


def plan(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
) -> ReconcilePlan:
    return ReconcilePlan(
        duplicates=tuple(find_duplicates(observations, rules)),
        conflicts=tuple(find_conflicts(observations, rules)),
    )


def _without_lines(text: str, line_numbers: set[int]) -> str:
    keep_newline = text.endswith("\n")
    lines = text.splitlines()
    kept = [line for index, line in enumerate(lines, start=1) if index not in line_numbers]
    out = "\n".join(kept)
    return out + ("\n" if keep_newline and kept else "")


def _edits(duplicates: Sequence[Duplicate]) -> dict[str, set[int]]:
    edits: dict[str, set[int]] = {}
    for duplicate in duplicates:
        if duplicate.abs_path:
            edits.setdefault(duplicate.abs_path, set()).add(duplicate.line_no)
    return edits


def render_patch(duplicates: Sequence[Duplicate]) -> str:
    """중복 줄 제거를 unified diff로 제안한다 - 파일은 읽기만 한다."""
    chunks: list[str] = []
    for abs_path, line_numbers in sorted(_edits(duplicates).items()):
        path = Path(abs_path)
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            continue
        modified = _without_lines(original, line_numbers)
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
        chunks.append(f"# {abs_path}\n" + "".join(diff))
    return "\n".join(chunks)


def write_patch(base_dir: Path, patch_text: str, stamp: str) -> Path:
    directory = base_dir / RECONCILE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"reconcile-{stamp}.patch"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"reconcile-{stamp}-{counter}.patch"
    atomic_write_bytes(path, patch_text.encode("utf-8"))
    return path


def apply_removals(
    base_dir: Path,
    duplicates: Sequence[Duplicate],
    reason: str = "reconcile --apply",
    now: str | None = None,
) -> tuple[Savepoint, list[str]]:
    """세이브포인트를 먼저 만들고 중복 줄을 지운다. 되돌림: savepoint restore <id>."""
    edits = _edits(duplicates)
    savepoint = create(base_dir, [Path(p) for p in sorted(edits)], reason, now=now)
    changed: list[str] = []
    for abs_path, line_numbers in sorted(edits.items()):
        path = Path(abs_path)
        original = path.read_text(encoding="utf-8")
        modified = _without_lines(original, line_numbers)
        if modified != original:
            atomic_write_bytes(path, modified.encode("utf-8"))
            changed.append(abs_path)
    return savepoint, changed
