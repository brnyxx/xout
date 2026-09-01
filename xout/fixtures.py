"""AC7 - 고정 픽스처 렌더러: 오프라인 슬롯 치환과 slot span 맵.

세션 전체는 fixtures/ 에 동결된 정적 픽스처 팩(공통 skeleton + 축별 3변형
대비 슬롯, 전역 스타일 축은 통짜 3본)과 대상 레포의 읽기전용 스캔으로 얻은
{file}/{lang}/{framework} 슬롯 치환만으로 렌더된다. 런타임 LLM/네트워크
호출은 0회이며 표준 라이브러리만 사용한다.

축의 국소/전역 판정은 docs/axis_locality_table.md 판정표를 따른다:
- 전역(통짜) 2축: response_language, verbosity -> fixtures/global_wholes.json
- 국소(슬롯) 6축 -> fixtures/scene_skeleton.json + fixtures/axis_slots.json

렌더된 트랜스크립트의 모든 조각은 slot span 맵(SlotSpan)에 문자 범위로
기록되며, strike의 fragment_id 또는 임의 span은 이 맵을 통해 축으로
귀속된다(refutation_for_span / refutation_for_fragment).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator, Mapping

from xout.counter import DEFAULT_CATALOG
from xout.events import Refutation

logger = logging.getLogger(__name__)

CATALOG_VERSION = "v1"

_SOURCE_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_PACKAGED_FIXTURES_DIR = Path(__file__).resolve().parent / "_data" / "fixtures"
FIXTURES_DIR = (
    _SOURCE_FIXTURES_DIR
    if _SOURCE_FIXTURES_DIR.is_dir()
    else _PACKAGED_FIXTURES_DIR
)

MANIFEST_FILE = "pack_manifest.json"
SKELETON_FILE = "scene_skeleton.json"
AXIS_SLOTS_FILE = "axis_slots.json"
GLOBAL_WHOLES_FILE = "global_wholes.json"

# docs/axis_locality_table.md 의 전역(통짜) 판정 축 - 표와 어긋나면 안 된다.
GLOBAL_AXES: tuple[str, ...] = ("response_language", "verbosity")
LOCAL_AXES: tuple[str, ...] = tuple(
    axis for axis in DEFAULT_CATALOG if axis not in GLOBAL_AXES
)

SKIN_PLACEHOLDERS: tuple[str, ...] = ("{file}", "{lang}", "{framework}")

KIND_STATIC = "static"
KIND_SLOT = "slot"

ROLE_STATIC = "static"
ROLE_BACKGROUND = "background"
ROLE_CONTRAST = "contrast"

SIDE_LEFT = "left"
SIDE_RIGHT = "right"

SEGMENT_SEPARATOR = "\n\n"

_MAX_SCAN_FILES = 20000

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", "__pycache__", "venv", "dist", "build", "target"}
)

_LANG_BY_EXT: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cs": "C#",
    ".sh": "Shell",
}

#: 동률일 때의 언어 우선순위 - 결정성을 위해 순서를 고정한다.
_EXT_PRIORITY: tuple[str, ...] = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".sh",
)

#: 프레임워크 마커 파일명 - 우선순위 순. 파일명/확장자 기반 판정만 쓴다.
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (
    ("manage.py", "Django"),
    ("next.config.js", "Next.js"),
    ("next.config.mjs", "Next.js"),
    ("next.config.ts", "Next.js"),
    ("nuxt.config.ts", "Nuxt"),
    ("vite.config.ts", "Vite"),
    ("vite.config.js", "Vite"),
    ("angular.json", "Angular"),
    ("Cargo.toml", "Cargo"),
    ("go.mod", "Go 모듈"),
    ("pom.xml", "Maven"),
    ("build.gradle", "Gradle"),
    ("build.gradle.kts", "Gradle"),
    ("pyproject.toml", "Python 프로젝트"),
    ("requirements.txt", "Python 프로젝트"),
    ("package.json", "Node.js"),
)

_FALLBACK_FRAMEWORK = "일반 프로젝트"


class FixtureViolation(ValueError):
    """픽스처 팩/렌더 계약 위반."""


@dataclass(frozen=True, slots=True)
class RepoSkin:
    """레포 읽기전용 스캔 결과 - {file}/{lang}/{framework} 치환값."""

    file: str
    lang: str
    framework: str
    generic: bool = False

    def substitutions(self) -> dict[str, str]:
        return {
            "{file}": self.file,
            "{lang}": self.lang,
            "{framework}": self.framework,
        }


#: 빈 레포/비코드 레포 폴백 - 제네릭 스킨 기본값.
GENERIC_SKIN = RepoSkin(
    file="README.md", lang="텍스트", framework=_FALLBACK_FRAMEWORK, generic=True
)


@dataclass(frozen=True, slots=True)
class Segment:
    """skeleton의 한 조각 - 정적 배경 서술이거나 축 슬롯이다."""

    kind: str
    segment_id: str
    text: str = ""
    axis: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (KIND_STATIC, KIND_SLOT):
            raise FixtureViolation(f"허용되지 않은 segment kind: {self.kind!r}")
        if not self.segment_id:
            raise FixtureViolation("segment_id는 비울 수 없다")
        if self.kind == KIND_STATIC and not self.text:
            raise FixtureViolation(f"static 조각의 text가 비어 있다: {self.segment_id}")
        if self.kind == KIND_SLOT and not self.axis:
            raise FixtureViolation(f"slot 조각의 axis가 비어 있다: {self.segment_id}")


@dataclass(frozen=True, slots=True)
class FixturePack:
    """fixtures/ 에 동결된 정적 픽스처 팩 - 시나리오 수는 사전등록 봉인 문서가 정한다."""

    catalog_version: str
    scene_id: str
    skeleton: tuple[Segment, ...]
    axis_slots: Mapping[str, Mapping[str, str]]
    global_user_fragments: Mapping[str, tuple[str, str]]
    global_wholes: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class SlotSpan:
    """렌더된 트랜스크립트 안에서 한 조각이 차지하는 문자 범위 [start, end)."""

    fragment_id: str
    role: str
    start: int
    end: int
    axis: str | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        if not (0 <= self.start < self.end):
            raise FixtureViolation(
                f"span 범위 위반: [{self.start}, {self.end}) ({self.fragment_id})"
            )

    def covers(self, start: int, end: int) -> bool:
        """부분 span [start, end)가 이 조각 안에 온전히 들어 있는가."""
        return self.start <= start and end <= self.end


@dataclass(frozen=True, slots=True)
class RenderedTranscript:
    """렌더된 한쪽 트랜스크립트 - 본문 텍스트와 slot span 맵."""

    scene_id: str
    side: str
    text: str
    spans: tuple[SlotSpan, ...]


@dataclass(frozen=True, slots=True)
class RenderedPair:
    """좌/우 대비 페어 - 같은 skeleton에 대비 축만 다른 값으로 렌더된다."""

    pair_id: str
    scene_id: str
    axis: str
    left_value: str
    right_value: str
    left: RenderedTranscript
    right: RenderedTranscript


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
            document = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise FixtureViolation(f"픽스처 파일을 읽지 못했다: {path}") from e
    if not isinstance(document, dict):
        raise FixtureViolation(f"픽스처 파일 최상위는 객체여야 한다: {path}")
    return document


def _validate_variants(axis: str, variants: Mapping[str, str], origin: str) -> None:
    """축의 3변형이 카탈로그 값과 일치하고 실제로 서로 대비되는지 검사한다."""
    expected = DEFAULT_CATALOG[axis]
    if set(variants) != set(expected):
        raise FixtureViolation(
            f"{origin}의 {axis} 변형 키가 카탈로그와 다르다: {sorted(variants)}"
        )
    texts = [variants[value] for value in expected]
    if any(not text for text in texts):
        raise FixtureViolation(f"{origin}의 {axis} 변형에 빈 텍스트가 있다")
    if len(set(texts)) != len(texts):
        raise FixtureViolation(f"{origin}의 {axis} 변형이 서로 대비되지 않는다")


def load_pack(fixtures_dir: Path | str | None = None) -> FixturePack:
    """fixtures/ 의 정적 데이터 파일만으로 픽스처 팩을 적재한다."""
    base = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES_DIR

    manifest = _load_json(base / MANIFEST_FILE)
    if manifest.get("catalog_version") != CATALOG_VERSION:
        raise FixtureViolation(
            f"manifest catalog_version 불일치: {manifest.get('catalog_version')!r}"
        )

    skeleton_doc = _load_json(base / SKELETON_FILE)
    scene_id = skeleton_doc.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise FixtureViolation("skeleton의 scene_id가 비어 있다")

    segments: list[Segment] = []
    for raw in skeleton_doc.get("segments", ()):
        if not isinstance(raw, Mapping):
            raise FixtureViolation(f"segment 형식 위반: {raw!r}")
        segments.append(
            Segment(
                kind=str(raw.get("kind", "")),
                segment_id=str(raw.get("segment_id", "")),
                text=str(raw.get("text", "")),
                axis=str(raw.get("axis", "")),
            )
        )
    if not segments:
        raise FixtureViolation("skeleton에 segment가 하나도 없다")

    segment_ids = [seg.segment_id for seg in segments]
    if len(set(segment_ids)) != len(segment_ids):
        raise FixtureViolation("segment_id가 중복됐다")

    slot_axes = [seg.axis for seg in segments if seg.kind == KIND_SLOT]
    if sorted(slot_axes) != sorted(LOCAL_AXES):
        raise FixtureViolation(
            f"skeleton 슬롯 축이 국소 6축과 다르다: {sorted(slot_axes)}"
        )

    slots_doc = _load_json(base / AXIS_SLOTS_FILE)
    raw_slots = slots_doc.get("slots")
    if not isinstance(raw_slots, Mapping) or set(raw_slots) != set(LOCAL_AXES):
        raise FixtureViolation("axis_slots의 축 키가 국소 6축과 다르다")
    axis_slots: dict[str, dict[str, str]] = {}
    for axis in LOCAL_AXES:
        variants = {str(k): str(v) for k, v in dict(raw_slots[axis]).items()}
        _validate_variants(axis, variants, AXIS_SLOTS_FILE)
        axis_slots[axis] = variants

    wholes_doc = _load_json(base / GLOBAL_WHOLES_FILE)
    raw_wholes = wholes_doc.get("wholes")
    if not isinstance(raw_wholes, Mapping) or set(raw_wholes) != set(GLOBAL_AXES):
        raise FixtureViolation("global_wholes의 축 키가 전역 2축과 다르다")
    global_wholes: dict[str, dict[str, str]] = {}
    global_user_fragments: dict[str, tuple[str, str]] = {}
    for axis in GLOBAL_AXES:
        entry = raw_wholes[axis]
        if not isinstance(entry, Mapping):
            raise FixtureViolation(f"global_wholes[{axis}] 형식 위반")
        fragment = entry.get("user_fragment")
        if not isinstance(fragment, Mapping):
            raise FixtureViolation(f"global_wholes[{axis}].user_fragment 형식 위반")
        fragment_seg_id = str(fragment.get("segment_id", ""))
        fragment_text = str(fragment.get("text", ""))
        if not fragment_seg_id or not fragment_text:
            raise FixtureViolation(f"global_wholes[{axis}].user_fragment가 비어 있다")
        bodies = {str(k): str(v) for k, v in dict(entry.get("bodies", {})).items()}
        _validate_variants(axis, bodies, GLOBAL_WHOLES_FILE)
        global_user_fragments[axis] = (fragment_seg_id, fragment_text)
        global_wholes[axis] = bodies

    logger.debug(
        "픽스처 팩 적재 완료: scene=%s, 국소 %d축, 전역 %d축",
        scene_id,
        len(axis_slots),
        len(global_wholes),
    )
    return FixturePack(
        catalog_version=CATALOG_VERSION,
        scene_id=scene_id,
        skeleton=tuple(segments),
        axis_slots=axis_slots,
        global_user_fragments=global_user_fragments,
        global_wholes=global_wholes,
    )


def _iter_repo_files(root: Path) -> Iterator[Path]:
    """루트 아래 파일을 결정적 순서로 낸다 - 숨김/벤더 디렉토리는 건너뛴다."""
    count = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(
            part.startswith(".") or part in _SKIP_DIR_NAMES for part in rel.parts
        ):
            continue
        if not path.is_file():
            continue
        yield rel
        count += 1
        if count >= _MAX_SCAN_FILES:
            logger.debug("레포 스캔 상한 %d개 도달 - 조기 종료", _MAX_SCAN_FILES)
            return


def _dominant_extension(code_files: list[Path]) -> str:
    counts = Counter(path.suffix for path in code_files)
    return max(
        counts,
        key=lambda ext: (
            counts[ext],
            -_EXT_PRIORITY.index(ext) if ext in _EXT_PRIORITY else -len(_EXT_PRIORITY),
        ),
    )


def scan_repo_skin(repo_root: Path | str) -> RepoSkin:
    """대상 레포를 읽기전용으로 스캔해 스킨을 만든다 - 파일명/확장자 기반.

    파일 내용은 읽지 않으며 네트워크/서브프로세스도 쓰지 않는다.
    빈 레포/비코드 레포는 제네릭 스킨으로 폴백한다.
    """
    root = Path(repo_root)
    if not root.is_dir():
        logger.debug("레포 루트가 없어 제네릭 스킨 폴백: %s", root)
        return GENERIC_SKIN

    files = list(_iter_repo_files(root))
    code_files = [path for path in files if path.suffix in _LANG_BY_EXT]
    if not code_files:
        logger.debug("코드 파일이 없어 제네릭 스킨 폴백: %s", root)
        return GENERIC_SKIN

    dominant = _dominant_extension(code_files)
    candidates = [path for path in code_files if path.suffix == dominant]
    representative = min(candidates, key=lambda p: (len(p.parts), p.as_posix()))

    names = {path.name for path in files}
    framework = _FALLBACK_FRAMEWORK
    for marker, detected in _FRAMEWORK_MARKERS:
        if marker in names:
            framework = detected
            break

    return RepoSkin(
        file=representative.as_posix(),
        lang=_LANG_BY_EXT[dominant],
        framework=framework,
        generic=False,
    )


def _substitute(text: str, skin: RepoSkin) -> str:
    out = text
    for placeholder, value in skin.substitutions().items():
        out = out.replace(placeholder, value)
    return out


def _assemble(
    scene_id: str,
    side: str,
    resolved: list[tuple[str, str, str | None, str | None, str]],
    skin: RepoSkin,
) -> RenderedTranscript:
    """(fragment_id, role, axis, value, 원문) 목록을 본문 + span 맵으로 조립한다."""
    parts: list[str] = []
    spans: list[SlotSpan] = []
    cursor = 0
    for index, (fragment_id, role, axis, value, raw) in enumerate(resolved):
        if index:
            cursor += len(SEGMENT_SEPARATOR)
        text = _substitute(raw, skin)
        spans.append(
            SlotSpan(
                fragment_id=fragment_id,
                role=role,
                start=cursor,
                end=cursor + len(text),
                axis=axis,
                value=value,
            )
        )
        parts.append(text)
        cursor += len(text)
    return RenderedTranscript(
        scene_id=scene_id,
        side=side,
        text=SEGMENT_SEPARATOR.join(parts),
        spans=tuple(spans),
    )


def _render_local(
    pack: FixturePack, contrast_axis: str, value: str, side: str, skin: RepoSkin
) -> RenderedTranscript:
    """국소 축 렌더 - 공통 skeleton에 배경 슬롯은 채굴 최빈값(index 0)으로 채운다."""
    resolved: list[tuple[str, str, str | None, str | None, str]] = []
    for seg in pack.skeleton:
        if seg.kind == KIND_STATIC:
            resolved.append((seg.segment_id, ROLE_STATIC, None, None, seg.text))
            continue
        if seg.axis == contrast_axis:
            chosen, role = value, ROLE_CONTRAST
        else:
            chosen, role = DEFAULT_CATALOG[seg.axis][0], ROLE_BACKGROUND
        resolved.append(
            (
                f"{seg.segment_id}:{chosen}",
                role,
                seg.axis,
                chosen,
                pack.axis_slots[seg.axis][chosen],
            )
        )
    return _assemble(pack.scene_id, side, resolved, skin)


def _render_global(
    pack: FixturePack, axis: str, value: str, side: str, skin: RepoSkin
) -> RenderedTranscript:
    """전역 축 렌더 - 공통 사용자 조각 + 통짜 본문 1본이 대비 슬롯이 된다."""
    fragment_seg_id, fragment_text = pack.global_user_fragments[axis]
    resolved: list[tuple[str, str, str | None, str | None, str]] = [
        (fragment_seg_id, ROLE_STATIC, None, None, fragment_text),
        (
            f"whole-{axis}:{value}",
            ROLE_CONTRAST,
            axis,
            value,
            pack.global_wholes[axis][value],
        ),
    ]
    return _assemble(pack.scene_id, side, resolved, skin)


def render_pair(
    pack: FixturePack,
    axis: str,
    left_value: str,
    right_value: str,
    skin: RepoSkin,
    pair_id: str | None = None,
) -> RenderedPair:
    """대비 페어를 렌더한다 - 좌/우는 같은 skeleton에 대비 축만 다른 값이다."""
    values = DEFAULT_CATALOG.get(axis)
    if values is None:
        raise FixtureViolation(f"카탈로그에 없는 축: {axis!r}")
    for value in (left_value, right_value):
        if value not in values:
            raise FixtureViolation(f"{axis} 축에 없는 값: {value!r}")
    if left_value == right_value:
        raise FixtureViolation(f"대비 페어의 좌우 값이 같다: {left_value!r}")

    if pair_id is None:
        pair_id = f"{pack.scene_id}:{axis}:{left_value}|{right_value}"

    renderer = _render_global if axis in GLOBAL_AXES else _render_local
    return RenderedPair(
        pair_id=pair_id,
        scene_id=pack.scene_id,
        axis=axis,
        left_value=left_value,
        right_value=right_value,
        left=renderer(pack, axis, left_value, SIDE_LEFT, skin),
        right=renderer(pack, axis, right_value, SIDE_RIGHT, skin),
    )


def render_all_pairs(pack: FixturePack, skin: RepoSkin) -> tuple[RenderedPair, ...]:
    """8축 x 3값 조합(축당 3페어) 전체를 렌더한다 - 총 24페어."""
    pairs: list[RenderedPair] = []
    for axis, values in DEFAULT_CATALOG.items():
        for left_value, right_value in combinations(values, 2):
            pairs.append(render_pair(pack, axis, left_value, right_value, skin))
    return tuple(pairs)


def span_at(transcript: RenderedTranscript, offset: int) -> SlotSpan:
    """오프셋을 포함하는 조각을 찾는다 - 조각 사이 구분자 위면 계약 위반."""
    for span in transcript.spans:
        if span.start <= offset < span.end:
            return span
    raise FixtureViolation(f"어떤 조각에도 속하지 않는 오프셋: {offset}")


def attribute_span(
    transcript: RenderedTranscript, start: int, end: int
) -> SlotSpan:
    """임의 span [start, end)를 단일 조각으로 귀속시킨다."""
    if not (0 <= start < end <= len(transcript.text)):
        raise FixtureViolation(f"본문 범위를 벗어난 span: [{start}, {end})")
    for span in transcript.spans:
        if span.covers(start, end):
            return span
    raise FixtureViolation(f"단일 조각 경계에 들어가지 않는 span: [{start}, {end})")


def span_for_fragment(
    transcript: RenderedTranscript, fragment_id: str
) -> SlotSpan:
    """fragment_id로 span 맵을 조회한다 - strike 귀속의 기본 경로."""
    for span in transcript.spans:
        if span.fragment_id == fragment_id:
            return span
    raise FixtureViolation(f"span 맵에 없는 fragment_id: {fragment_id!r}")


def _refutation_from(span: SlotSpan, side: str) -> Refutation:
    if span.axis is None or span.value is None:
        raise FixtureViolation(
            f"정적 skeleton 조각은 축으로 귀속되지 않는다: {span.fragment_id}"
        )
    return Refutation(
        axis=span.axis, value=span.value, fragment_id=span.fragment_id, side=side
    )


def refutation_for_span(
    transcript: RenderedTranscript, start: int, end: int
) -> Refutation:
    """임의 span을 슬롯 -> 축으로 귀속시킨 반증 provenance를 만든다."""
    return _refutation_from(attribute_span(transcript, start, end), transcript.side)


def refutation_for_fragment(
    transcript: RenderedTranscript, fragment_id: str
) -> Refutation:
    """strike의 fragment_id를 슬롯 -> 축으로 귀속시킨 반증 provenance를 만든다."""
    return _refutation_from(
        span_for_fragment(transcript, fragment_id), transcript.side
    )


def contrast_span(transcript: RenderedTranscript) -> SlotSpan:
    """대비 축 슬롯 span - 렌더된 트랜스크립트마다 정확히 하나 존재한다."""
    found = [span for span in transcript.spans if span.role == ROLE_CONTRAST]
    if len(found) != 1:
        raise FixtureViolation(f"대비 슬롯이 정확히 1개가 아니다: {len(found)}개")
    return found[0]
