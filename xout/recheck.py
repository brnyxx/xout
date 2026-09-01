"""4막 경량 재심 - 데몬/훅 없이 manifest와 순수 함수만으로 동작한다.

- check_due(manifest, now)는 last_review 경과 >= 7일 판정과 대기 건수 N 계산만
  수행해 배너 데이터를 돌려주는 순수 함수다. 부수효과가 없고 manifest를 바꾸지
  않는다. 수동 진입(/popper recheck)은 배너와 무관하게 항상 열려 있다 -
  plan_recheck_session은 due를 전혀 검사하지 않는다.
- 재심 큐 우선순위는 전순서다: 불안정(unstable) > untested-prior(미시험) >
  충돌(conflict). recovery.RECHECK_CLASS_PRIORITY를 그대로 재사용한다.
- 미니 재심 세션 예산은 5-7긋기(기본 5, 상한 7)다. 세션은 1막 페어 UI를
  재사용한다 - 신규 UI 기계장치 없이 기존 세션 이벤트 스키마
  (session_start + strike)로만 열리고, session_start.payload.session_kind="recheck"
  표시는 recovery.fold_recovery가 이미 해석하는 막 경계 표기를 그대로 쓴다.
- 재심에서 부활(revive)한 값의 미시험 강등은 recovery.fold_recovery의 기존
  의미론을 그대로 사용한다(재구현 없음). 이 모듈은 그 결과를 읽기만 한다.
- 재심 결과는 append-only 이벤트(긋기/revive/session_validated)로 기록되고,
  last_review 갱신은 새 manifest 착지(refresh_last_review)로 표현된다
  (불변 아티팩트 원칙 - 기존 manifest는 절대 제자리 수정되지 않는다).
- 시각(now)은 반드시 인자로 주입받는다 - 이 모듈은 datetime.now()를 호출하지
  않는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from xout.compiler import MANIFEST_JSON, manifest_self_hash
from xout.events import Event, EventType
from xout.recovery import (
    RECHECK_CLASS_PRIORITY,
    Demotion,
    RecoveryChannel,
    RecoveryState,
)
from xout.session import PROFILE_RECHECK

logger = logging.getLogger(__name__)

DUE_DAYS: int = 7
MIN_BUDGET: int = 5
MAX_BUDGET: int = 7
DEFAULT_BUDGET: int = 5
RECHECK_SESSION_KIND: str = "recheck"
MANUAL_COMMAND: str = "/popper recheck"
BANNER_TEMPLATE: str = "재심 대기 {count}건"


class RecheckViolation(ValueError):
    """4막 재심 계약 위반."""


def _as_utc(value: datetime | str, label: str) -> datetime:
    """주입된 시각을 UTC aware datetime으로 정규화한다 - 벽시계 조회 없음."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as e:
            raise RecheckViolation(f"{label}를 시각으로 해석할 수 없다: {value!r}") from e
    if not isinstance(value, datetime):
        raise RecheckViolation(
            f"{label}는 datetime 또는 ISO 문자열이어야 한다: {type(value).__name__}"
        )
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RecheckTarget:
    """정렬된 재심 큐의 한 항목 - manifest.recheck_queue 스키마를 정규화한 것."""

    klass: str
    axis: str
    rule_id: str | None = None
    conflict_id: str | None = None
    source_order: int = 0

    def __post_init__(self) -> None:
        if self.klass not in RECHECK_CLASS_PRIORITY:
            raise RecheckViolation(f"알 수 없는 재심 클래스: {self.klass!r}")
        if not self.axis:
            raise RecheckViolation("RecheckTarget.axis는 비울 수 없다")

    @property
    def priority(self) -> int:
        return RECHECK_CLASS_PRIORITY.index(self.klass)


def _target_from(entry: Mapping[str, Any], position: int) -> RecheckTarget:
    if not isinstance(entry, Mapping):
        raise RecheckViolation(f"재심 큐 항목은 매핑이어야 한다: {type(entry).__name__}")
    klass = str(entry.get("class", entry.get("klass", "")))
    rule_id = entry.get("rule_id")
    conflict_id = entry.get("conflict_id")
    raw_order = entry.get("order", position)
    return RecheckTarget(
        klass=klass,
        axis=str(entry.get("axis", "")),
        rule_id=str(rule_id) if rule_id is not None else None,
        conflict_id=str(conflict_id) if conflict_id is not None else None,
        source_order=int(raw_order) if isinstance(raw_order, int) else position,
    )


def order_queue(queue: Iterable[Mapping[str, Any]]) -> tuple[RecheckTarget, ...]:
    """재심 큐 전순서 정렬 - 클래스 우선순위(불안정 > untested-prior > 충돌).

    같은 클래스 안에서는 manifest가 매긴 order(없으면 입력 순서)를 유지한다.
    """
    targets = tuple(_target_from(entry, index) for index, entry in enumerate(queue))
    return tuple(sorted(targets, key=lambda t: (t.priority, t.source_order)))


@dataclass(frozen=True, slots=True)
class RecheckBanner:
    """check_due가 돌려주는 배너 데이터 - 표시는 호출자(/popper 실행부) 몫."""

    due: bool
    pending: int
    days_elapsed: float | None
    last_review: str | None

    @property
    def text(self) -> str | None:
        """배너 문구 - 경과 미달이거나 대기 0건이면 배너 자체가 없다."""
        if not self.due or self.pending == 0:
            return None
        return BANNER_TEMPLATE.format(count=self.pending)


def check_due(manifest: Mapping[str, Any], now: datetime | str) -> RecheckBanner:
    """last_review 경과 >= 7일 판정 + 대기 건수 N 계산 - 순수 함수, 부수효과 없음."""
    moment = _as_utc(now, "now")
    pending = len(order_queue(manifest.get("recheck_queue", ())))
    raw = manifest.get("last_review")
    if raw is None:
        logger.warning("manifest에 last_review가 없다 - 배너를 띄우지 않는다")
        return RecheckBanner(due=False, pending=pending, days_elapsed=None, last_review=None)
    reviewed = _as_utc(str(raw), "last_review")
    elapsed = (moment - reviewed) / timedelta(days=1)
    due = elapsed >= DUE_DAYS
    logger.debug("재심 판정: 경과 %.2f일 due=%s 대기 %d건", elapsed, due, pending)
    return RecheckBanner(due=due, pending=pending, days_elapsed=elapsed, last_review=str(raw))


@dataclass(frozen=True, slots=True)
class RecheckPlan:
    """미니 재심 세션 계획 - 1막 페어 UI를 그대로 재사용하는 세션의 청사진."""

    session_id: str
    budget: int
    targets: tuple[RecheckTarget, ...]
    pending_total: int
    opening: Event

    @property
    def session_kind(self) -> str:
        return RECHECK_SESSION_KIND


def plan_recheck_session(
    manifest: Mapping[str, Any],
    session_id: str,
    *,
    budget: int = DEFAULT_BUDGET,
) -> RecheckPlan:
    """미니 재심 세션 계획 - 수동(/popper recheck) 경로는 due와 무관하게 항상 열린다.

    예산은 5-7긋기(기본 5)로 강제되고, 대상은 정렬된 큐의 선두 budget건이다.
    반환되는 opening은 기존 session_start 이벤트 스키마 그대로이며 신규 이벤트
    타입을 만들지 않는다.
    """
    if not session_id:
        raise RecheckViolation("session_id는 비울 수 없다")
    if not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise RecheckViolation(f"재심 예산은 {MIN_BUDGET}-{MAX_BUDGET}긋기다: {budget}")
    ordered = order_queue(manifest.get("recheck_queue", ()))
    targets = ordered[:budget]
    opening = Event(
        type=EventType.SESSION_START,
        session_id=session_id,
        payload={
            "session_kind": RECHECK_SESSION_KIND,
            "profile": PROFILE_RECHECK,
            "recheck_budget": budget,
            "recheck_axes": list(dict.fromkeys(target.axis for target in targets)),
        },
    )
    logger.info(
        "재심 세션 계획: session=%s 대상 %d/%d건 예산 %d긋기",
        session_id,
        len(targets),
        len(ordered),
        budget,
    )
    return RecheckPlan(
        session_id=session_id,
        budget=budget,
        targets=targets,
        pending_total=len(ordered),
        opening=opening,
    )


def revived_demotions(state: RecoveryState) -> tuple[Demotion, ...]:
    """재심 revive로 부활한 값의 미시험 강등 기록 - recovery 의미론을 읽기만 한다."""
    return tuple(d for d in state.demotions if d.cause is RecoveryChannel.REVIVE)


def refresh_last_review(manifest: Mapping[str, Any], now: datetime | str) -> dict[str, Any]:
    """재심 완료를 새 manifest 착지로 표현한다 - 입력 manifest는 불변.

    last_review만 주입 시각으로 갱신하고 manifest 자기 해시를 다시 계산한
    새 dict를 돌려준다. 기존 manifest는 어떤 키도 바뀌지 않는다.
    """
    stamp = _as_utc(now, "now").isoformat()
    landed: dict[str, Any] = json.loads(json.dumps(manifest, ensure_ascii=False))
    landed["last_review"] = stamp
    outputs = landed.get("outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get(MANIFEST_JSON), dict):
        outputs[MANIFEST_JSON]["content_hash"] = manifest_self_hash(landed)
    logger.info("재심 착지: last_review=%s (새 manifest 방출)", stamp)
    return landed
