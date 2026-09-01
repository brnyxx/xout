"""세션 런타임 - 콜드 오픈부터 착지까지, 전부 이벤트 스트림에서 파생한다.

질문/설정/온보딩 화면 없이 열려서 첫 페어를 즉시 띄운다. 첫 페어의 대비 축은
항상 자율성(COLD_OPEN_AXIS)이며, 픽스처 팩에서 렌더된 페어 목록을 결정론적으로
재배열해 얻는다.

사용자의 유일한 동사는 긋기다. 긋기가 들어오면 append-only 이벤트 로그에
StrikeEvent 하나가 쌓이고, 가설 카운터와 컴파일된 실행 룰은 그 스트림을 다시
접어서(fold) 파생한다. 카운터도 룰도 어디에도 저장되지 않는다 - 같은 스트림을
replay하면 항상 같은 값이 나온다.

프로덕션 런타임이 콜드 오픈 위에 얹는 것:

- 프로파일 강제: product(판별 15/프로브 0), validation(판별 13 + 슬롯 9/13
  미러 프로브 2), recheck(재심 큐 선두 5-7긋기). 수치의 소유자는 봉인
  사전등록 문서이며 session.load_session_specs()로 읽는다.
- 슬롯 캡: 슬롯을 다 쓰면 세션이 닫히고 이후 긋기는 SessionComplete로
  기각된다. 자동 연장은 없다.
- 페어 스케줄러: 이벤트 스트림의 순수 함수. 이미 그은 페어와 판별력을 잃은
  페어(대비 값이 소거된 페어)를 결정론적으로 건너뛴다.
- 오긋기 복구: undo_tombstone 명시 이벤트 채널(AC3). 확률 모델 없음.
  무른 페어는 스케줄러가 그대로 다시 세운다. 이미 소비된 슬롯은 돌려주지
  않는다 - 캡은 봉인 수치다.
- 착지: 마지막 슬롯이 닫히면 세션 판정(fold_session)을 거쳐
  session_validated/voided를 방출하고, product/recheck 세션은
  ~/.claude/popper/에 XOUT.md + manifest.json + settings.xout.json을
  착지시킨다. content hash 불일치(수기 편집)는 silent overwrite 대신
  착지 차단으로 표면화된다.

런타임 LLM/네트워크 호출은 0회다. 픽스처 파일 읽기와 순수 함수 fold만 쓴다.
"""

from __future__ import annotations

import logging
import hashlib
import json
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from xout.compiler import (
    CompiledRule,
    HashMismatch,
    _context_stream,
    compile_rules,
    write_outputs,
)
from xout.counter import fold
from xout.judgment import (
    CORRECT_RESTORATIONS_KEY,
    DISCRIMINATIVE_INSTANCES_KEY,
    MIS_RESTORATIONS_KEY,
)
from xout.locking import base_lock
from xout.events import (
    Event,
    EventLog,
    EventType,
    Refutation,
    SchemaViolation,
    StrikeEvent,
    StrikeTarget,
)
from xout.events import strike as make_strike
from xout.fixtures import (
    CONTEXT_ROUTINE,
    DEFAULT_LANG,
    GENERIC_SKIN,
    SCENE_CONTEXTS,
    RenderedPair,
    RenderedTranscript,
    RepoSkin,
    contrast_span,
    load_pack,
    localize_skin,
    refutation_for_fragment,
    render_all_pairs,
    scan_repo_skin,
)
from xout.recheck import DEFAULT_BUDGET, plan_recheck_session
from xout.session import (
    PROFILE_PRODUCT,
    PROFILE_RECHECK,
    PROFILE_VALIDATION,
    SessionSpec,
    fold_session,
    load_session_specs,
    probe_result,
    probe_shown,
    select_probe_pairs,
)
from xout.scoring import (
    CELL_MIS_RESTORED,
    CELL_RESTORED,
    DEFAULT_GROUND_TRUTH_HASH_PATH,
    DEFAULT_GROUND_TRUTH_PATH,
    load_ground_truth,
    score_restoration,
)
from xout.store import EventStore, event_sort_key
from xout.writer import OwnedWriter

logger = logging.getLogger(__name__)

#: 콜드 오픈이 반드시 첫 순서에 세우는 대비 축.
COLD_OPEN_AXIS = "autonomy"

#: 화면에 노출되는 축 이름 - 기본 언어는 한국어다.
AXIS_LABELS: dict[str, str] = {
    "autonomy": "자율성",
    "commit_style": "커밋 스타일",
    "test_discipline": "테스트 규율",
    "comment_doc": "주석과 문서화",
    "error_behavior": "에러가 났을 때의 행동",
    "scope_adherence": "범위 준수",
    "verification": "완료 전 검증",
    "dependency_policy": "의존성 정책",
}

AXIS_LABELS_EN: dict[str, str] = {
    "autonomy": "Autonomy",
    "commit_style": "Commit style",
    "test_discipline": "Test discipline",
    "comment_doc": "Comments and docs",
    "error_behavior": "Behavior on errors",
    "scope_adherence": "Scope adherence",
    "verification": "Verification before done",
    "dependency_policy": "Dependency policy",
}

AXIS_LABELS_JA: dict[str, str] = {
    "autonomy": "自律性",
    "commit_style": "コミット方針",
    "test_discipline": "テスト規律",
    "comment_doc": "コメントとドキュメント",
    "error_behavior": "エラー時の行動",
    "scope_adherence": "範囲の遵守",
    "verification": "完了前の検証",
    "dependency_policy": "依存関係の方針",
}

AXIS_LABELS_ZH: dict[str, str] = {
    "autonomy": "自主性",
    "commit_style": "提交方式",
    "test_discipline": "测试纪律",
    "comment_doc": "注释与文档",
    "error_behavior": "出错时的行为",
    "scope_adherence": "范围遵守",
    "verification": "完成前验证",
    "dependency_policy": "依赖策略",
}

AXIS_LABELS_BY_LANG: dict[str, dict[str, str]] = {
    "ko": AXIS_LABELS,
    "en": AXIS_LABELS_EN,
    "ja": AXIS_LABELS_JA,
    "zh": AXIS_LABELS_ZH,
}

#: 화면이 제공하는 유일한 입력 어포던스 - 긋기 대상 네 가지.
STRIKE_TARGETS: tuple[str, ...] = tuple(target.value for target in StrikeTarget)

#: 긋기 대상별 한국어 라벨.
STRIKE_LABELS: dict[str, str] = {
    StrikeTarget.LEFT.value: "왼쪽 긋기",
    StrikeTarget.RIGHT.value: "오른쪽 긋기",
    StrikeTarget.BOTH.value: "양쪽 모두 긋기",
    StrikeTarget.PAIR.value: "이 페어 통째로 긋기",
}

#: 미러 프로브의 좌우 뒤집기 - 원본 strike와 프로브 strike의 일관성 판정에 쓴다.
_MIRRORED_TARGET: dict[StrikeTarget, StrikeTarget] = {
    StrikeTarget.LEFT: StrikeTarget.RIGHT,
    StrikeTarget.RIGHT: StrikeTarget.LEFT,
    StrikeTarget.BOTH: StrikeTarget.BOTH,
}

PROBE_FLIP = "flip"
PROBE_CONSISTENT = "consistent"

LANDING_LANDED = "landed"
LANDING_BLOCKED = "blocked"
LANDING_SKIPPED = "skipped"
LANDING_VOIDED = "voided"
LANDING_FAILED = "failed"


class SessionComplete(RuntimeError):
    """슬롯 캡이 닫힌 세션에 대한 추가 긋기 - 자동 연장은 없다."""


class RecoveryUnavailable(RuntimeError):
    """무를 긋기가 없는 상태의 undo 요청."""


class StalePresentation(RuntimeError):
    """클라이언트가 보지 못한 페어/슬롯에 대한 긋기."""


def axis_label(axis: str, lang: str = "ko") -> str:
    """축의 화면 라벨 - 미등록 축/언어는 원문 그대로 돌려준다."""
    return AXIS_LABELS_BY_LANG.get(lang, AXIS_LABELS).get(axis, axis)


@dataclass(frozen=True, slots=True)
class PairView:
    """화면에 걸린 대비 페어 한 쌍 - 렌더된 좌우 본문과 귀속 정보."""

    pair_id: str
    scene_id: str
    axis: str
    axis_label: str
    left_value: str
    right_value: str
    left_text: str
    right_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "scene_id": self.scene_id,
            "axis": self.axis,
            "axis_label": self.axis_label,
            "left_value": self.left_value,
            "right_value": self.right_value,
            "left_text": self.left_text,
            "right_text": self.right_text,
        }


@dataclass(frozen=True, slots=True)
class RuleView:
    """컴파일 페인에 뿌려지는 실행 룰 한 줄."""

    axis: str
    axis_label: str
    value: str
    text: str
    corroboration_grade: str = ""
    value_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "axis_label": self.axis_label,
            "value": self.value,
            "text": self.text,
            "corroboration_grade": self.corroboration_grade,
            "value_source": self.value_source,
        }


@dataclass(frozen=True, slots=True)
class LandingView:
    """세션 종료 착지 결과 - 착지 경로 또는 차단 사유."""

    status: str
    base_dir: str | None = None
    written: tuple[str, ...] = ()
    detections: tuple[dict[str, Any], ...] = ()
    import_line: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "base_dir": self.base_dir,
            "written": list(self.written),
            "detections": [dict(d) for d in self.detections],
            "import_line": self.import_line,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    """한 시점의 화면 상태 - 전부 이벤트 스트림에서 파생된 값이다."""

    session_id: str
    profile: str
    pair: PairView | None
    remaining_combinations: int
    eliminated_pairs: int
    strike_count: int
    slots_used: int
    slots_total: int
    rules: tuple[RuleView, ...]
    last_strike: str | None = None
    undoable: bool = False
    session_complete: bool = False
    voided_reason: str | None = None
    landing: LandingView | None = None
    banner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "pair": self.pair.to_dict() if self.pair is not None else None,
            "remaining_combinations": self.remaining_combinations,
            "eliminated_pairs": self.eliminated_pairs,
            "strike_count": self.strike_count,
            "slots_used": self.slots_used,
            "slots_total": self.slots_total,
            "rules": [rule.to_dict() for rule in self.rules],
            "last_strike": self.last_strike,
            "undoable": self.undoable,
            "session_complete": self.session_complete,
            "voided_reason": self.voided_reason,
            "landing": self.landing.to_dict() if self.landing is not None else None,
            "banner": self.banner,
            "strike_targets": list(STRIKE_TARGETS),
        }


def ordered_pairs(pairs: Sequence[RenderedPair]) -> tuple[RenderedPair, ...]:
    """콜드 오픈 축의 페어가 항상 앞에 오도록 결정론적으로 재배열한다.

    안정 정렬이라 축 내부 순서와 나머지 축들의 상대 순서는 그대로 보존된다.
    """
    return tuple(
        tuple(pairs)
    )


def _contrast_refutation(transcript: RenderedTranscript) -> Refutation:
    """대비 슬롯 span을 축으로 귀속시킨 반증 provenance 한 건."""
    return refutation_for_fragment(transcript, contrast_span(transcript).fragment_id)


def _mirrored_view(pair: RenderedPair) -> RenderedPair:
    """미러 프로브 제시용 좌우 반전 - 렌더 산출물은 그대로, 좌우만 바꾼다."""
    return RenderedPair(
        pair_id=pair.pair_id,
        scene_id=pair.scene_id,
        axis=pair.axis,
        left_value=pair.right_value,
        right_value=pair.left_value,
        left=replace(pair.right, side="left"),
        right=replace(pair.left, side="right"),
    )


def _repo_skin_payload(skin: RepoSkin) -> dict[str, Any]:
    return {
        "file": skin.file,
        "lang": skin.lang,
        "framework": skin.framework,
        "generic": skin.generic,
    }


def _rendered_pairs_digest(pairs: Sequence[RenderedPair]) -> str:
    payload = json.dumps(
        [asdict(pair) for pair in pairs],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sealed_repo_skin(payload: Mapping[str, Any]) -> RepoSkin:
    raw = payload.get("repo_skin")
    if not isinstance(raw, Mapping):
        raise SchemaViolation("재개 이벤트에 봉인된 repo_skin이 없다")
    file = raw.get("file")
    lang = raw.get("lang")
    framework = raw.get("framework")
    generic = raw.get("generic")
    if not all(isinstance(value, str) and value for value in (file, lang, framework)):
        raise SchemaViolation("봉인된 repo_skin 문자열이 유효하지 않다")
    if not isinstance(generic, bool):
        raise SchemaViolation("봉인된 repo_skin.generic이 bool이 아니다")
    file_path = Path(file)
    if file_path.is_absolute() or ".." in file_path.parts:
        raise SchemaViolation("봉인된 repo_skin.file은 안전한 상대 경로여야 한다")
    return RepoSkin(
        file=file,
        lang=lang,
        framework=framework,
        generic=generic,
    )


def undone_strike_ids(events: Sequence[StrikeEvent | Event]) -> frozenset[str]:
    """undo_tombstone이 무른 strike 이벤트 id 집합 - 스트림의 순수 함수."""
    undone: set[str] = set()
    for event in events:
        if isinstance(event, Event) and event.type is EventType.UNDO_TOMBSTONE:
            target = event.payload.get("strike_event_id") or event.payload.get(
                "target_event_id"
            )
            if target:
                undone.add(str(target))
    return frozenset(undone)


def consumed_pair_ids(events: Sequence[StrikeEvent | Event]) -> frozenset[str]:
    """이번 스트림에서 이미 소비된(그어지고 무르지 않은) 페어 id 집합."""
    undone = undone_strike_ids(events)
    return frozenset(
        event.pair_id
        for event in events
        if isinstance(event, StrikeEvent) and event.event_id not in undone
    )


def select_pair(
    all_events: Sequence[StrikeEvent | Event],
    session_events: Sequence[StrikeEvent | Event],
    pairs: Sequence[RenderedPair],
    allowed_axes: Sequence[str] | None = None,
) -> RenderedPair | None:
    """다음에 세울 페어 - 이벤트 스트림의 순수 함수라 replay가 같은 답을 낸다.

    1순위: 아직 안 그었고 대비 두 값이 모두 생존한(판별력 있는) 페어.
    2순위: 아직 안 그은 페어 - 남은 판별력이 없어도 슬롯 캡은 채워야 한다.
    3순위: 이미 그은 페어 재등판 - 같은 조합 재긋기는 복구 채널의 일부다.
    allowed_axes가 주어지면(재심) 각 순위를 그 축들에서 먼저 찾는다.
    """
    counters = {
        context: fold(_context_stream(tuple(all_events), context))
        for context in dict.fromkeys(SCENE_CONTEXTS.values())
    }
    consumed = consumed_pair_ids(session_events)

    def surviving(pair: RenderedPair) -> set[str]:
        """판별력은 그 페어가 속한 맥락의 생존값 기준이다 - 맥락 간 오염 금지."""
        context = SCENE_CONTEXTS.get(pair.scene_id, CONTEXT_ROUTINE)
        return set(counters[context].axis(pair.axis).surviving)

    def rank(candidates: Sequence[RenderedPair]) -> RenderedPair | None:
        for pair in candidates:
            alive = surviving(pair)
            if (
                pair.pair_id not in consumed
                and len(alive) > 1
                and pair.left_value in alive
                and pair.right_value in alive
            ):
                return pair
        for pair in candidates:
            if pair.pair_id not in consumed:
                return pair
        return candidates[0] if candidates else None

    if allowed_axes is not None:
        allowed = [p for p in pairs if p.axis in set(allowed_axes)]
        found = rank(allowed)
        if found is not None:
            return found
    return rank(tuple(pairs))


class ColdOpenSession:
    """질문 0개로 열리는 세션 - 긋기만 받고 나머지는 전부 fold로 파생한다."""

    __slots__ = (
        "_pairs",
        "_by_id",
        "_log",
        "_lock",
        "_session_id",
        "_last",
        "_profile",
        "_spec",
        "_slots_total",
        "_store",
        "_land_dir",
        "_history",
        "_recheck_axes",
        "_landing",
        "_banner",
        "_conflicts_for",
        "_lang",
    )

    def __init__(
        self,
        repo_root: Path | str | None = None,
        fixtures_dir: Path | str | None = None,
        session_id: str | None = None,
        profile: str = PROFILE_PRODUCT,
        store: EventStore | None = None,
        land_dir: Path | str | None = None,
        history: Sequence[StrikeEvent | Event] = (),
        resume_events: Sequence[StrikeEvent | Event] = (),
        recheck_manifest: Mapping[str, Any] | None = None,
        recheck_budget: int = DEFAULT_BUDGET,
        banner: str | None = None,
        conflicts_for: Callable[
            [tuple[CompiledRule, ...]], Sequence[Mapping[str, Any]]
        ]
        | None = None,
        lang: str = DEFAULT_LANG,
    ) -> None:
        self._lang = lang
        pack = load_pack(fixtures_dir, lang=lang)
        skin: RepoSkin = GENERIC_SKIN if repo_root is None else scan_repo_skin(repo_root)
        skin = localize_skin(skin, lang)
        pairs = ordered_pairs(render_all_pairs(pack, skin))
        if not pairs:
            raise SchemaViolation("픽스처 팩에서 페어를 하나도 렌더하지 못했다")
        if pairs[0].axis != COLD_OPEN_AXIS:
            raise SchemaViolation(f"콜드 오픈 첫 페어의 축이 {COLD_OPEN_AXIS}가 아니다")

        resumed = tuple(resume_events)
        if resumed:
            resumed_ids = {event.session_id for event in resumed}
            if len(resumed_ids) != 1:
                raise SchemaViolation("재개 이벤트는 한 세션에만 속해야 한다")
            event_ids: set[str] = set()
            for expected_seq, event in enumerate(resumed):
                if event.seq != expected_seq:
                    raise SchemaViolation(
                        f"재개 이벤트 seq 불연속: {event.seq!r} != {expected_seq}"
                    )
                if event.event_id in event_ids:
                    raise SchemaViolation(
                        f"재개 이벤트 ID 중복: {event.event_id}"
                    )
                event_ids.add(event.event_id)
            resumed_id = next(iter(resumed_ids))
            if session_id is not None and session_id != resumed_id:
                raise SchemaViolation("재개 session_id와 이벤트 session_id가 다르다")
            session_id = resumed_id
            opening_event = next(
                (
                    event
                    for event in resumed
                    if isinstance(event, Event)
                    and event.type is EventType.SESSION_START
                ),
                None,
            )
            if opening_event is None:
                raise SchemaViolation("재개 이벤트에 session_start가 없다")
            resumed_profile = str(
                opening_event.payload.get("profile")
                or opening_event.payload.get("session_kind")
                or ""
            )
            if resumed_profile != profile:
                raise SchemaViolation(
                    f"재개 프로파일 불일치: {resumed_profile!r} != {profile!r}"
                )
            if opening_event.payload.get("fixture_catalog_version") != (
                pack.catalog_version
            ):
                raise SchemaViolation("재개 fixture catalog 버전이 현재 배포물과 다르다")
            skin = _sealed_repo_skin(opening_event.payload)
            pairs = ordered_pairs(render_all_pairs(pack, skin))
            if not pairs or pairs[0].axis != COLD_OPEN_AXIS:
                raise SchemaViolation(
                    "봉인된 repo_skin으로 콜드 오픈 페어를 복원하지 못했다"
                )
            if opening_event.payload.get(
                "rendered_pairs_sha256"
            ) != _rendered_pairs_digest(pairs):
                raise SchemaViolation("재개 rendered pair digest가 현재 fixture와 다르다")
            if any(
                isinstance(event, Event)
                and event.type
                in (EventType.SESSION_VALIDATED, EventType.SESSION_VOIDED)
                for event in resumed
            ):
                raise SessionComplete("이미 종료된 세션은 재개할 수 없다")

        self._log = EventLog(resumed)
        self._lock = RLock()
        self._session_id = session_id or uuid.uuid4().hex
        if profile == PROFILE_VALIDATION:
            # 검증 세션은 기억 효과를 피하되 같은 session_id replay는 결정적이어야 한다.
            # 축 블록 순서는 봉인된 판별 커버리지를 보존하고, 블록 내부 순서와
            # 각 페어의 좌우만 세션별로 바꿔 동일 경로 기억 효과를 줄인다.
            block_map: dict[tuple[str, str], list[RenderedPair]] = {}
            block_order: list[tuple[str, str]] = []
            for pair in pairs:
                key = (pair.scene_id, pair.axis)
                if key not in block_map:
                    block_map[key] = []
                    block_order.append(key)
                block_map[key].append(pair)
            blocks: list[list[RenderedPair]] = [block_map[key] for key in block_order]
            for block in blocks:
                block.sort(
                    key=lambda pair: hashlib.sha256(
                        f"{self._session_id}:order:{pair.pair_id}".encode()
                    ).digest()
                )
            ordered = tuple(pair for block in blocks for pair in block)
            self._pairs = tuple(
                _mirrored_view(pair)
                if hashlib.sha256(
                    f"{self._session_id}:side:{pair.pair_id}".encode()
                ).digest()[0]
                & 1
                else pair
                for pair in ordered
            )
        else:
            self._pairs = pairs
        self._by_id = {pair.pair_id: pair for pair in self._pairs}
        self._last = next(
            (
                event.strike_target.value
                for event in reversed(self._log.events)
                if isinstance(event, StrikeEvent)
            ),
            None,
        )
        self._store = store
        self._land_dir = Path(land_dir) if land_dir is not None else None
        self._history = tuple(history)
        self._landing: LandingView | None = None
        self._banner = banner
        self._conflicts_for = conflicts_for
        self._profile = profile

        self._spec: SessionSpec | None
        self._recheck_axes: tuple[str, ...] | None
        if profile == PROFILE_RECHECK:
            self._spec = None
            if resumed:
                opening = next(
                    event
                    for event in resumed
                    if isinstance(event, Event)
                    and event.type is EventType.SESSION_START
                )
                self._slots_total = int(
                    opening.payload.get("recheck_budget", recheck_budget)
                )
                raw_axes = opening.payload.get("recheck_axes", ())
                self._recheck_axes = tuple(
                    str(axis) for axis in raw_axes if isinstance(axis, str)
                )
                if not self._recheck_axes:
                    raise SchemaViolation("재개할 재심 축이 없다")
            else:
                manifest = recheck_manifest if recheck_manifest is not None else {}
                plan = plan_recheck_session(
                    manifest, self._session_id, budget=recheck_budget
                )
                self._slots_total = plan.budget
                self._recheck_axes = tuple(
                    dict.fromkeys(target.axis for target in plan.targets)
                )
                if not plan.targets:
                    raise SchemaViolation("재심 대기 대상이 없다")
                opening = replace(
                    plan.opening,
                    payload={
                        **plan.opening.payload,
                        "fixture_catalog_version": pack.catalog_version,
                        "rendered_pairs_sha256": _rendered_pairs_digest(pairs),
                        "repo_skin": _repo_skin_payload(skin),
                    },
                )
        else:
            specs = load_session_specs()
            spec = specs.get(profile)
            if spec is None:
                raise SchemaViolation(f"봉인 문서에 없는 세션 프로파일: {profile!r}")
            self._spec = spec
            sealed_spec = {
                "discriminative_slots": spec.discriminative_slots,
                "probe_slots": list(spec.probe_slots),
                "required_full_axes": spec.required_full_axes,
            }
            if resumed and opening_event.payload.get("session_spec") != sealed_spec:
                raise SchemaViolation("재개 session_spec이 현재 봉인 규격과 다르다")
            self._slots_total = spec.total_slots
            self._recheck_axes = None
            opening = Event(
                type=EventType.SESSION_START,
                session_id=self._session_id,
                payload={
                    "profile": profile,
                    "fixture_catalog_version": pack.catalog_version,
                    "rendered_pairs_sha256": _rendered_pairs_digest(pairs),
                    "repo_skin": _repo_skin_payload(skin),
                    "session_spec": sealed_spec,
                },
            )
        if not resumed:
            self._append(opening)
        elif self._slots_used() > self._slots_total:
            raise SchemaViolation("재개 이벤트가 봉인된 슬롯 cap을 초과했다")
        elif self._slots_used() == self._slots_total:
            self._finalize()
        logger.debug(
            "세션 준비 완료: session=%s profile=%s 슬롯 %d개 페어 %d개",
            self._session_id,
            profile,
            self._slots_total,
            len(self._pairs),
        )

    # ------------------------------------------------------------------ 속성

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def log(self) -> EventLog:
        """append-only 이벤트 로그 - 세션의 단일 진실원."""
        return self._log

    @property
    def landing(self) -> LandingView | None:
        return self._landing

    # ------------------------------------------------------------- 내부 파생

    def _append(self, event: StrikeEvent | Event) -> StrikeEvent | Event:
        stamped = self._log.append(event)
        if self._store is not None:
            self._store.append(stamped)
        return stamped

    def _all_events(self) -> tuple[StrikeEvent | Event, ...]:
        return self._history + self._log.events

    def _slots_used(self) -> int:
        used = 0
        for event in self._log.events:
            if isinstance(event, StrikeEvent):
                used += 1
            elif event.type is EventType.PROBE_SHOWN:
                used += 1
        return used

    def _complete(self) -> bool:
        return self._slots_used() >= self._slots_total

    def _pending_probe(self) -> Event | None:
        """제시됐지만 아직 결과가 없는 미러 프로브 - 있으면 그 프로브가 화면이다."""
        pending: Event | None = None
        for event in self._log.events:
            if isinstance(event, StrikeEvent):
                continue
            if event.type is EventType.PROBE_SHOWN:
                pending = event
            elif event.type is EventType.PROBE_RESULT:
                pending = None
        return pending

    def _last_undoable_strike(self) -> StrikeEvent | None:
        undone = undone_strike_ids(self._log.events)
        for event in reversed(self._log.events):
            if isinstance(event, Event) and event.type is EventType.PROBE_RESULT:
                return None
            if isinstance(event, StrikeEvent) and event.event_id not in undone:
                return event
        return None

    def _ensure_presentation(self) -> RenderedPair | None:
        """지금 화면에 세울 페어 - 필요하면 프로브 제시 이벤트를 여기서 적재한다."""
        if self._complete():
            return None
        pending = self._pending_probe()
        if pending is not None:
            origin = self._by_id.get(str(pending.payload.get("pair_id")))
            return _mirrored_view(origin) if origin is not None else None

        position = self._slots_used() + 1
        if self._spec is not None and position in self._spec.probe_slots:
            expected = select_probe_pairs(self._log.events, self._spec)
            pair_id = expected.get(position)
            origin = self._by_id.get(pair_id) if pair_id is not None else None
            if origin is not None:
                self._append(
                    probe_shown(
                        self._session_id,
                        position,
                        origin.pair_id,
                        axis=origin.axis,
                        mirrored=True,
                    )
                )
                return _mirrored_view(origin)
            logger.warning(
                "프로브 위치 %d에 미러할 판별쌍이 없다 - 판별 페어로 대체한다", position
            )
        scheduling_events = (
            self._log.events
            if self._profile == PROFILE_VALIDATION
            else self._all_events()
        )
        return select_pair(
            scheduling_events, self._log.events, self._pairs, self._recheck_axes
        )

    # ---------------------------------------------------------------- 스냅샷

    def snapshot(self) -> Snapshot:
        """지금 화면에 걸릴 상태를 이벤트 스트림에서 다시 파생한다."""
        with self._lock:
            return self._derive()

    def strike(
        self,
        target: str,
        *,
        expected_pair_id: str | None = None,
        expected_slot: int | None = None,
    ) -> Snapshot:
        """긋기 한 건을 스트림에 쌓고 갱신된 상태를 돌려준다."""
        resolved = self._resolve(target)
        with self._lock:
            if self._complete():
                raise SessionComplete(
                    f"세션 슬롯 {self._slots_total}개가 이미 닫혔다 - 자동 연장은 없다"
                )
            pending = self._pending_probe()
            if pending is not None:
                self._check_presentation(
                    str(pending.payload.get("pair_id")),
                    pending.payload.get("slot"),
                    expected_pair_id,
                    expected_slot,
                )
                self._record_probe_result(pending, resolved)
            else:
                pair = self._ensure_presentation()
                if pair is None:
                    raise SessionComplete("세울 페어가 없다 - 세션이 이미 닫혔다")
                pending_after = self._pending_probe()
                if pending_after is not None:
                    # 방금 제시가 프로브였다 - 이 긋기는 프로브 결과다.
                    self._check_presentation(
                        str(pending_after.payload.get("pair_id")),
                        pending_after.payload.get("slot"),
                        expected_pair_id,
                        expected_slot,
                    )
                    self._record_probe_result(pending_after, resolved)
                else:
                    self._check_presentation(
                        pair.pair_id,
                        self._slots_used() + 1,
                        expected_pair_id,
                        expected_slot,
                    )
                    event = make_strike(
                        session_id=self._session_id,
                        pair_id=pair.pair_id,
                        axis=pair.axis,
                        scene_id=pair.scene_id,
                        target=resolved,
                        refutations=self._refutations(pair, resolved),
                    )
                    self._append(event)
            self._last = resolved.value
            if self._complete():
                self._finalize()
            return self._derive()

    def undo(self) -> Snapshot:
        """마지막 긋기를 undo_tombstone 명시 채널로 무른다 - 슬롯은 돌려주지 않는다."""
        with self._lock:
            origin = self._last_undoable_strike()
            if origin is None:
                raise RecoveryUnavailable("무를 긋기가 없다")
            if self._complete():
                raise SessionComplete("닫힌 세션의 긋기는 무를 수 없다")
            self._append(
                Event(
                    type=EventType.UNDO_TOMBSTONE,
                    session_id=self._session_id,
                    payload={
                        "strike_event_id": origin.event_id,
                        "pair_id": origin.pair_id,
                        "axis": origin.axis,
                    },
                )
            )
            logger.debug("긋기 무름: strike=%s pair=%s", origin.event_id, origin.pair_id)
            return self._derive()

    # ------------------------------------------------------------- 내부 동작

    @staticmethod
    def _resolve(target: str) -> StrikeTarget:
        try:
            return StrikeTarget(target)
        except ValueError as e:
            raise SchemaViolation(f"허용되지 않은 긋기 대상: {target!r}") from e

    @staticmethod
    def _refutations(pair: RenderedPair, target: StrikeTarget) -> tuple[Refutation, ...]:
        """긋기 대상에 맞는 반증 provenance를 대비 슬롯에서 뽑는다."""
        if target is StrikeTarget.PAIR:
            # 페어 긋기는 축x장면 판별력-없음 이벤트라 반증을 남기지 않는다.
            return ()
        sides: list[RenderedTranscript] = []
        if target in (StrikeTarget.LEFT, StrikeTarget.BOTH):
            sides.append(pair.left)
        if target in (StrikeTarget.RIGHT, StrikeTarget.BOTH):
            sides.append(pair.right)
        return tuple(_contrast_refutation(side) for side in sides)

    def _record_probe_result(self, shown: Event, target: StrikeTarget) -> None:
        """프로브 위 긋기를 flip/consistent 결과 이벤트로만 기록한다(컴파일 불활성)."""
        pair_id = str(shown.payload.get("pair_id"))
        position = shown.payload.get("slot")
        original = self._original_strike_target(pair_id)
        mirrored_expectation = (
            _MIRRORED_TARGET.get(original) if original is not None else None
        )
        result = (
            PROBE_CONSISTENT if target is mirrored_expectation else PROBE_FLIP
        )
        raw_axis = shown.payload.get("axis")
        self._append(
            probe_result(
                self._session_id,
                int(position) if isinstance(position, int) else self._slots_used(),
                pair_id,
                result,
                axis=str(raw_axis) if raw_axis is not None else None,
            )
        )
        logger.debug("프로브 결과: pair=%s result=%s", pair_id, result)

    def _original_strike_target(self, pair_id: str) -> StrikeTarget | None:
        undone = undone_strike_ids(self._log.events)
        for event in reversed(self._log.events):
            if (
                isinstance(event, StrikeEvent)
                and event.pair_id == pair_id
                and event.event_id not in undone
            ):
                return event.strike_target
        return None

    def _finalize(self) -> None:
        """마지막 슬롯이 닫힌 직후 한 번 - 판정 방출과 착지."""
        lock = (
            self._store.lock
            if self._store is not None
            else (
                base_lock(self._land_dir)
                if self._land_dir is not None
                else nullcontext()
            )
        )
        with lock:
            self._finalize_locked()

    def _finalize_locked(self) -> None:
        """base 단위로 직렬화된 terminal+landing 전이를 수행한다."""
        voided_reason: str | None = None
        terminal: Event
        if self._spec is not None:
            judgment = fold_session(self._log.events, {self._profile: self._spec})
            if judgment.voided is not None:
                voided = judgment.voided.to_event()
                terminal = Event(
                    type=voided.type,
                    session_id=voided.session_id,
                    event_id=voided.event_id,
                    at=voided.at,
                    payload={**voided.payload, "profile": self._profile},
                )
                voided_reason = judgment.voided.reason
            elif judgment.complete and judgment.stream_valid:
                evidence: dict[str, Any] = {
                    "profile": self._profile,
                    DISCRIMINATIVE_INSTANCES_KEY: len(
                        judgment.fully_discriminated_axes
                    ),
                }
                if self._profile == PROFILE_VALIDATION:
                    ground_truth = load_ground_truth(
                        DEFAULT_GROUND_TRUTH_PATH,
                        expected_file_hash=DEFAULT_GROUND_TRUTH_HASH_PATH.read_text(
                            encoding="utf-8"
                        ).strip(),
                    )
                    report = score_restoration(
                        ground_truth,
                        compile_rules(self._log.events),
                    )
                    counts = report.cell_counts()
                    evidence[CORRECT_RESTORATIONS_KEY] = counts[CELL_RESTORED]
                    evidence[MIS_RESTORATIONS_KEY] = counts[CELL_MIS_RESTORED]
                terminal = Event(
                    type=EventType.SESSION_VALIDATED,
                    session_id=self._session_id,
                    payload=evidence,
                )
            elif judgment.complete:
                reason = judgment.reasons[0] if judgment.reasons else "stream_invalid"
                terminal = Event(
                    type=EventType.SESSION_VOIDED,
                    session_id=self._session_id,
                    payload={
                        "reason": reason,
                        "reasons": list(judgment.reasons),
                        "profile": self._profile,
                    },
                )
                voided_reason = reason
            else:
                raise SchemaViolation("슬롯 cap 이전에는 세션을 finalize할 수 없다")
        else:
            terminal = Event(
                type=EventType.SESSION_VALIDATED,
                session_id=self._session_id,
                payload={
                    "profile": PROFILE_RECHECK,
                    "session_kind": PROFILE_RECHECK,
                },
            )
        if (
            voided_reason is None
            and self._profile in (PROFILE_PRODUCT, PROFILE_RECHECK)
        ):
            landing = self._land(None)
            self._append(terminal)
            self._landing = landing
        else:
            self._append(terminal)
            self._landing = self._land(voided_reason)

    @staticmethod
    def _check_presentation(
        pair_id: str,
        slot: Any,
        expected_pair_id: str | None,
        expected_slot: int | None,
    ) -> None:
        if expected_pair_id is not None and expected_pair_id != pair_id:
            raise StalePresentation("제시된 페어가 현재 페어와 다르다")
        if expected_slot is not None:
            try:
                actual_slot = int(slot)
            except (TypeError, ValueError) as exc:
                raise StalePresentation("제시 슬롯이 유효하지 않다") from exc
            if expected_slot != actual_slot:
                raise StalePresentation("제시된 슬롯이 현재 슬롯과 다르다")

    def _land(self, voided_reason: str | None) -> LandingView:
        """product/recheck 세션의 산출물 착지 - 검증 세션과 무효 세션은 착지하지 않는다."""
        if voided_reason is not None:
            return LandingView(status=LANDING_VOIDED, detail=voided_reason)
        if self._profile == PROFILE_VALIDATION:
            return LandingView(
                status=LANDING_SKIPPED, detail="검증 세션은 산출물을 착지시키지 않는다"
            )
        if self._land_dir is None:
            return LandingView(status=LANDING_SKIPPED, detail="착지 디렉토리 미지정")

        try:
            lock = self._store.lock if self._store is not None else base_lock(self._land_dir)
            with lock:
                if self._store is not None:
                    completed = self._store.load_completed()
                    current_is_completed = any(
                        event.session_id == self._session_id
                        for event in completed
                    )
                    events = (
                        completed
                        if current_is_completed
                        else tuple(
                            sorted(
                                completed + self._log.events,
                                key=event_sort_key,
                            )
                        )
                    )
                else:
                    events = self._all_events()
                conflicts: Sequence[Mapping[str, Any]] = ()
                if self._conflicts_for is not None:
                    conflicts = self._conflicts_for(compile_rules(events, lang=self._lang))
                result = write_outputs(
                    events,
                    base_dir=self._land_dir,
                    session_id=self._session_id,
                    conflicts=conflicts,
                    lang=self._lang,
                )
        except HashMismatch as e:
            records = tuple(dict(r) for r in e.records)
            logger.warning("착지 차단 - 수기 편집 감지: %s", records)
            return LandingView(
                status=LANDING_BLOCKED,
                base_dir=str(self._land_dir),
                detections=records,
                detail="content hash 불일치 - silent overwrite 금지",
            )
        except OSError as e:
            logger.exception("착지 실패: %s", self._land_dir)
            return LandingView(
                status=LANDING_FAILED, base_dir=str(self._land_dir), detail=str(e)
            )
        writer = OwnedWriter(base_dir=self._land_dir)
        return LandingView(
            status=LANDING_LANDED,
            base_dir=str(result.base_dir),
            written=tuple(str(path) for path in result.written),
            import_line=writer.import_line(),
        )

    def _derive(self) -> Snapshot:
        events = self._all_events()
        counter = fold(events)
        rules = compile_rules(events, lang=self._lang)
        presented = self._ensure_presentation()
        return Snapshot(
            session_id=self._session_id,
            profile=self._profile,
            pair=self._pair_view(presented) if presented is not None else None,
            remaining_combinations=counter.remaining_combinations,
            eliminated_pairs=counter.eliminated_pairs,
            strike_count=len(self._log.strikes()),
            slots_used=self._slots_used(),
            slots_total=self._slots_total,
            rules=tuple(_rule_view(rule) for rule in rules),
            last_strike=self._last,
            undoable=self._last_undoable_strike() is not None and not self._complete(),
            session_complete=self._complete(),
            voided_reason=self._voided_reason(),
            landing=self._landing,
            banner=self._banner,
        )

    def _voided_reason(self) -> str | None:
        for event in reversed(self._log.events):
            if isinstance(event, Event) and event.type is EventType.SESSION_VOIDED:
                reason = event.payload.get("reason")
                return str(reason) if reason is not None else None
        return None

    def _pair_view(self, pair: RenderedPair) -> PairView:
        return PairView(
            pair_id=pair.pair_id,
            scene_id=pair.scene_id,
            axis=pair.axis,
            axis_label=axis_label(pair.axis, self._lang),
            left_value=pair.left_value,
            right_value=pair.right_value,
            left_text=pair.left.text,
            right_text=pair.right.text,
        )


def _rule_view(rule: CompiledRule, lang: str = DEFAULT_LANG) -> RuleView:
    return RuleView(
        axis=rule.axis,
        axis_label=axis_label(rule.axis, lang),
        value=rule.value,
        text=rule.text,
        corroboration_grade=rule.corroboration_grade,
        value_source=rule.value_source,
    )
