"""로컬 채굴 - 이미 갖고 있는 에이전트 규칙 파일에서 축 관측을 읽는다.

`xout mine`은 로컬 레포의 CLAUDE.md / AGENTS.md / .cursorrules 류 파일을
읽기전용으로 스캔해, 각 줄을 8축 카탈로그의 값으로 귀속시키고 file:line
영수증과 함께 보고한다. 아무것도 쓰지 않고 원장에도 기록하지 않는다 -
세션에서 X를 칠 때 자기 환경과 교차 확인하는 용도의 관측 보고서다.

귀속은 투명한 키워드 휴리스틱이다: 패턴 테이블이 이 파일에 그대로 있고,
모든 관측은 매칭된 원문 줄을 증거로 동반한다. 휴리스틱이 놓친 줄은
관측이 없는 것이지 선호가 없는 것이 아니다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from xout.counter import DEFAULT_CATALOG

logger = logging.getLogger(__name__)

#: 스캔 대상 규칙 파일 - 이름 그대로의 파일.
RULE_FILE_NAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
)

#: 스캔 대상 규칙 파일 - 루트 기준 상대 경로.
RULE_FILE_PATHS: tuple[str, ...] = (
    ".github/copilot-instructions.md",
)

#: 스캔 대상 규칙 디렉토리 - 안의 .md/.mdc 파일을 모두 읽는다.
RULE_DIR_PATHS: tuple[str, ...] = (
    ".cursor/rules",
    ".claude/rules",
)

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target"}
)

_MAX_DEPTH = 4
_MAX_FILES = 200
_MAX_LINE_CHARS = 240

#: (축, 값) -> 매칭 패턴. 정밀도 우선 - 애매한 줄은 귀속하지 않는 편을 택한다.
MINED_PATTERNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("autonomy", "ask_first"): (
        r"ask (for )?(approval|permission)",
        r"(confirm|ask) before",
        r"do not (start|proceed|act) without",
        r"승인을? 받",
        r"먼저 물어",
        r"확인(을)? 받(고|은|아)",
    ),
    ("autonomy", "propose_then_act"): (
        r"(plan|proposal) first",
        r"(propose|outline).{0,20}then",
        r"계획(을)? 먼저",
    ),
    ("autonomy", "act_then_report"): (
        r"act first",
        r"먼저 실행",
        r"즉시 (실행|진행)",
    ),
    ("commit_style", "conventional"): (
        r"conventional commit",
        r"\bfeat[:/]",
        r"commit (message|title).{0,30}prefix",
        r"커밋 (메시지|컨벤션)",
    ),
    ("commit_style", "narrative"): (
        r"narrative commit",
        r"서술형 (커밋|제목)",
    ),
    ("commit_style", "no_auto_commit"): (
        r"(do not|don't|never) commit",
        r"commit only (when|if) asked",
        r"커밋(을|은)? (만들지|하지) (않|말)",
        r"요청.{0,10}커밋",
    ),
    ("test_discipline", "test_first"): (
        r"test[- ]first",
        r"\bTDD\b",
        r"(failing|reproducing) test first",
        r"테스트를? 먼저",
        r"재현 테스트",
    ),
    ("test_discipline", "test_after"): (
        r"(add|write|include) tests",
        r"tests? (are )?required",
        r"with tests",
        r"테스트(를|는)? (추가|필수|동반)",
    ),
    ("test_discipline", "on_request"): (
        r"tests? only (when|if)",
        r"요청.{0,15}테스트",
    ),
    ("comment_doc", "minimal"): (
        r"(avoid|no|minimal|unnecessary) comments",
        r"self[- ]documenting",
        r"comments? only (for|when)",
        r"주석.{0,10}(금지|최소|지양)",
        r"불필요한 주석",
    ),
    ("comment_doc", "docstring_only"): (
        r"docstrings?",
        r"\bJSDoc\b",
    ),
    ("comment_doc", "thorough"): (
        r"(comment|document) thoroughly",
        r"상세한 주석",
    ),
    ("error_behavior", "stop_and_report"): (
        r"stop and (report|ask)",
        r"(do not|don't|never) swallow",
        r"raw (error|log|output)",
        r"원문 로그",
        r"즉시 멈추",
    ),
    ("error_behavior", "retry_then_report"): (
        r"retry (once|one time)",
        r"한 ?번만? 재시도",
    ),
    ("error_behavior", "self_heal"): (
        r"until (they|tests?|it) pass",
        r"keep (going|fixing) until",
        r"통과할 때까지",
    ),
    ("scope_adherence", "strict"): (
        r"only.{0,25}(requested|asked)",
        r"(do not|don't|never) (touch|modify|change).{0,25}unrelated",
        r"stay (in|within) scope",
        r"out[- ]of[- ]scope",
        r"범위 밖",
        r"관련 없는 파일",
    ),
    ("scope_adherence", "adjacent_fix_ok"): (
        r"(closely )?related fixes",
        r"인접한? (결함|수정)",
    ),
    ("scope_adherence", "proactive"): (
        r"refactor as you go",
        r"proactive(ly)? (fix|improve|clean)",
        r"선제적",
    ),
    ("verification", "always_run"): (
        r"(run|pass).{0,25}(tests?|build).{0,25}before",
        r"before (submitting|committing|declaring|pushing)",
        r"must pass",
        r"(pnpm|npm|yarn|cargo|make|pytest|go) test",
        r"검증.{0,10}(통과|후에)",
        r"커밋 전.{0,15}(테스트|빌드)",
    ),
    ("verification", "on_risky"): (
        r"(full|whole) (suite|verification) (only )?(for|when) risky",
        r"위험한? (변경|작업)일? ?때만",
    ),
    ("verification", "trust_static"): (
        r"type[- ]?check(ing)? is (enough|sufficient)",
        r"정적 확인만",
    ),
    ("dependency_policy", "prefer_existing"): (
        r"(prefer|use).{0,20}(existing|standard library)",
        r"(avoid|no) new dependenc",
        r"minimi[sz]e dependenc",
        r"기존 의존성",
        r"표준 라이브러리.{0,10}우선",
    ),
    ("dependency_policy", "ask_first"): (
        r"(ask|confirm|approval) before.{0,25}(dependenc|package|install)",
        r"(dependenc|package)[a-z]* without (asking|approval)",
        r"before (adding|installing).{0,20}(dependenc|package)",
        r"의존성.{0,15}(확인|허락|승인)",
        r"패키지 추가 전",
    ),
    ("dependency_policy", "free"): (
        r"install (whatever|any).{0,15}(needed|necessary)",
        r"필요한 의존성.{0,10}바로",
    ),
}

_COMPILED: dict[tuple[str, str], tuple[re.Pattern[str], ...]] = {
    key: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for key, patterns in MINED_PATTERNS.items()
}


@dataclass(frozen=True, slots=True)
class Observation:
    """규칙 파일 한 줄이 한 (축, 값)으로 귀속된 관측 - file:line 영수증 동반."""

    axis: str
    value: str
    path: str
    line_no: int
    line: str
    abs_path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "value": self.value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def _iter_rule_files(root: Path) -> Iterator[Path]:
    """루트 아래의 규칙 파일을 결정적 순서로 낸다 - 얕은 깊이만 걷는다."""
    seen: set[Path] = set()
    count = 0

    def _emit(path: Path) -> Iterator[Path]:
        nonlocal count
        if path in seen or not path.is_file() or count >= _MAX_FILES:
            return
        seen.add(path)
        count += 1
        yield path

    for rel in RULE_FILE_PATHS:
        yield from _emit(root / rel)
    for rel in RULE_DIR_PATHS:
        directory = root / rel
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if child.suffix in (".md", ".mdc"):
                    yield from _emit(child)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if len(relative.parts) > _MAX_DEPTH:
            continue
        if any(
            part in _SKIP_DIR_NAMES
            or (part.startswith(".") and part not in (".cursorrules", ".github"))
            for part in relative.parts[:-1]
        ):
            continue
        if path.name in RULE_FILE_NAMES:
            yield from _emit(path)


def _observe_file(path: Path, display: str) -> list[Observation]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("규칙 파일을 읽지 못했다: %s", path, exc_info=True)
        return []
    found: list[Observation] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or len(line) > _MAX_LINE_CHARS:
            continue
        for (axis, value), patterns in _COMPILED.items():
            if any(pattern.search(line) for pattern in patterns):
                found.append(
                    Observation(
                        axis=axis,
                        value=value,
                        path=display,
                        line_no=line_no,
                        line=line,
                        abs_path=str(path.resolve()),
                    )
                )
    return found


#: 사용자 레벨 규칙 - Claude Code가 모든 프로젝트에 읽히는 파일들.
USER_RULE_FILE = ".claude/CLAUDE.md"
USER_RULE_DIR = ".claude/rules"


def user_rule_files(home: Path | None = None) -> list[Path]:
    """`~/.claude/CLAUDE.md`와 `~/.claude/rules/*.md|.mdc` - 존재하는 것만, 결정적 순서."""
    home = (home or Path.home()).expanduser()
    files: list[Path] = []
    claude_md = home / USER_RULE_FILE
    if claude_md.is_file():
        files.append(claude_md)
    rules_dir = home / USER_RULE_DIR
    if rules_dir.is_dir():
        files.extend(
            child for child in sorted(rules_dir.iterdir())
            if child.is_file() and child.suffix in (".md", ".mdc")
        )
    return files


def mine(
    roots: list[Path],
    include_user: bool = False,
    home: Path | None = None,
) -> list[Observation]:
    """루트들을 읽기전용으로 스캔해 축 관측 목록을 낸다 (결정적 순서).

    include_user=True면 사용자 레벨 규칙(`~/.claude/CLAUDE.md`, `~/.claude/rules/`)을
    루트 뒤에 덧붙인다 - 같은 파일이 루트에서 이미 읽혔으면 중복하지 않는다.
    """
    observations: list[Observation] = []
    seen: set[str] = set()
    for root in roots:
        root = root.expanduser()
        if root.is_file():
            seen.add(str(root.resolve()))
            observations.extend(_observe_file(root, str(root)))
            continue
        if not root.is_dir():
            logger.warning("채굴 루트가 없다: %s", root)
            continue
        for path in _iter_rule_files(root):
            seen.add(str(path.resolve()))
            observations.extend(
                _observe_file(path, str(path.relative_to(root)))
            )
    if include_user:
        home_dir = (home or Path.home()).expanduser()
        for path in user_rule_files(home_dir):
            if str(path.resolve()) in seen:
                continue
            observations.extend(
                _observe_file(path, "~/" + path.relative_to(home_dir).as_posix())
            )
    return observations


def summarize(observations: list[Observation]) -> dict[str, dict[str, int]]:
    """축별 값 관측 수 - 카탈로그의 모든 축을 항상 포함한다."""
    counts: dict[str, dict[str, int]] = {
        axis: {value: 0 for value in values}
        for axis, values in DEFAULT_CATALOG.items()
    }
    for observation in observations:
        counts[observation.axis][observation.value] += 1
    return counts


@dataclass(frozen=True, slots=True)
class Conflict:
    """프로젝트 규칙 파일의 한 줄이 컴파일된 규칙과 다른 값을 요구하는 지점."""

    axis: str
    rule_value: str
    observed_value: str
    path: str
    line_no: int
    line: str

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "rule_value": self.rule_value,
            "observed_value": self.observed_value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def find_conflicts(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
) -> list[Conflict]:
    """관측값이 두 맥락(일상/되돌리기 어려운 작업) 어느 쪽 생존값도 아니면 충돌이다.

    rules: 축 -> (일상 생존값, 되돌리기-어려운-작업 생존값 또는 None).
    프로젝트 규칙이 이기는 것은 XOUT.md 프리앰블이 이미 말한다 - 여기서는
    어디가 갈리는지 file:line으로 보여 주기만 한다.
    """
    conflicts: list[Conflict] = []
    for obs in observations:
        kept = rules.get(obs.axis)
        if kept is None:
            continue
        if obs.value in {value for value in kept if value}:
            continue
        conflicts.append(
            Conflict(obs.axis, kept[0], obs.value, obs.path, obs.line_no, obs.line)
        )
    return conflicts


@dataclass(frozen=True, slots=True)
class Duplicate:
    """프로젝트/사용자 규칙 파일의 한 줄이 이미 컴파일된 규칙과 같은 값을 말하는 지점."""

    axis: str
    value: str
    path: str
    line_no: int
    line: str
    abs_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "value": self.value,
            "path": self.path,
            "line": self.line_no,
            "text": self.line,
        }


def find_duplicates(
    observations: list[Observation],
    rules: Mapping[str, tuple[str, str | None]],
) -> list[Duplicate]:
    """관측값이 두 맥락 중 한쪽 생존값과 같으면 XOUT.md가 이미 커버하는 줄이다."""
    duplicates: list[Duplicate] = []
    for obs in observations:
        kept = rules.get(obs.axis)
        if kept is None or obs.value not in {value for value in kept if value}:
            continue
        duplicates.append(
            Duplicate(obs.axis, obs.value, obs.path, obs.line_no, obs.line, obs.abs_path)
        )
    return duplicates
