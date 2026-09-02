"""리컨사일 - 기존 규칙 파일과 착지된 XOUT.md의 중복·모순을 정리한다.

보고는 항상 읽기전용이다. 패치는 소유 디렉토리 안에 파일로만 제안한다.
실제 적용(`--apply --grant`)은 중복 줄 제거뿐이며, 그 전에 반드시
세이브포인트를 만든다 - 모순 줄은 어느 쪽이 맞는지 사용자만 알기 때문에
목록으로 보여 주기만 하고 절대 손대지 않는다.

같은 값을 말하되 문장까지 XOUT.md와 거의 같은 줄은 따로 센다. 이런 줄은
사용자가 직접 다듬어 둔 문장이거나 조건 하나가 덧붙어 있을 수 있어, 유사도
점수를 붙여 보고만 하고 `--apply`도 건드리지 않는다.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from xout.mine import Conflict, Duplicate, Observation, find_conflicts, find_duplicates
from xout.savepoint import Savepoint, atomic_write_bytes, create

RECONCILE_DIR = "reconcile"

#: 문장까지 거의 같다고 볼 최소 유사도 - 이 위는 지우지 않고 점수만 붙여 보고한다.
NEAR_DUPLICATE_THRESHOLD = 0.6

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
#: 한중일 문자 - 띄어쓰기가 토큰 경계를 주지 않아 글자 bigram으로 자른다.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def normalize(text: str) -> str:
    """소문자로 낮추고 구두점을 지우고 공백을 하나로 모은다."""
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", text.lower())).strip()


def shingles(text: str) -> set[str]:
    """비교 단위 - 한중일은 글자 bigram, 그 밖은 공백 토큰."""
    normal = normalize(text)
    if not normal:
        return set()
    if _CJK_RE.search(normal):
        chars = normal.replace(" ", "")
        if len(chars) < 2:
            return {chars}
        return {chars[index:index + 2] for index in range(len(chars) - 1)}
    return set(normal.split())


def similarity(left: str, right: str) -> float:
    """두 문장의 겹침 비율 (0.0 ~ 1.0) - 공유 shingle의 Dice 계수."""
    first, second = shingles(left), shingles(right)
    if not first or not second:
        return 0.0
    return 2 * len(first & second) / (len(first) + len(second))


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    """규칙 파일 한 줄이 같은 (축, 값)의 XOUT.md 문장과 문장까지 거의 같은 지점."""

    axis: str
    value: str
    path: str
    line_no: int
    line: str
    score: float
    rule: str

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "value": self.value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
            "score": round(self.score, 3),
            "rule": self.rule,
        }


def find_near_duplicates(
    duplicates: Sequence[Duplicate],
    sentences: Mapping[tuple[str, str], str],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> list[NearDuplicate]:
    """같은 (축, 값)의 규칙 문장과 threshold 이상 겹치는 줄을 점수와 함께 낸다."""
    near: list[NearDuplicate] = []
    for duplicate in duplicates:
        sentence = sentences.get((duplicate.axis, duplicate.value))
        if not sentence:
            continue
        score = similarity(duplicate.line, sentence)
        if score < threshold:
            continue
        near.append(
            NearDuplicate(
                axis=duplicate.axis,
                value=duplicate.value,
                path=duplicate.path,
                line_no=duplicate.line_no,
                line=duplicate.line,
                score=score,
                rule=sentence,
            )
        )
    return near


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    duplicates: tuple[Duplicate, ...]
    conflicts: tuple[Conflict, ...]
    near_duplicates: tuple[NearDuplicate, ...] = ()

    @property
    def files_to_edit(self) -> tuple[str, ...]:
        return tuple(sorted({d.abs_path for d in self.duplicates if d.abs_path}))

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicates": [d.to_dict() for d in self.duplicates],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "near_duplicates": [n.to_dict() for n in self.near_duplicates],
            "files_to_edit": list(self.files_to_edit),
        }


def plan(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
    sentences: Mapping[tuple[str, str], str] | None = None,
) -> ReconcilePlan:
    """중복/모순/거의 같은 줄로 나눈다 - 거의 같은 줄은 제거 대상에서 뺀다."""
    duplicates = find_duplicates(observations, rules)
    near = find_near_duplicates(duplicates, sentences or {})
    reported = {(item.path, item.line_no) for item in near}
    return ReconcilePlan(
        duplicates=tuple(
            duplicate for duplicate in duplicates
            if (duplicate.path, duplicate.line_no) not in reported
        ),
        conflicts=tuple(find_conflicts(observations, rules)),
        near_duplicates=tuple(near),
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
