"""도구 자기반증 판정 - append-only 이벤트 스트림에 대한 순수 fold.

이 모듈은 도구 자체가 반증되었는지(핵심 추측 반증)를 판정한다. 판정 상태는
어디에도 저장되지 않는다 - 같은 이벤트 리스트를 다시 fold하면 항상 같은
판정 상태가 나온다. 계약의 원본은 봉인 문서(docs/prereg/prereg_sealed.json)다.

1. prereg_sealed
   카탈로그 동결과 판정 기준의 유일한 통로다. 판정에 영향을 주는 수치는
   런타임 코드에 존재하지 않으며, 봉인 문서에서 파생된 이 이벤트의 payload
   (required_valid_sessions / required_discriminative_instances)로만 흘러든다.
   봉인 이전에 관측된 검증 세션 증거는 어느 쪽으로도 산입하지 않는다.

2. session_validated / session_voided
   유효 세션만 증거를 낸다. payload의 discriminative_instances(판별시험-통과
   인스턴스, 전체 합산 단위) / correct_restorations(정복원) /
   mis_restorations(오복원)를 누적한다. VOID 세션의 인스턴스는 payload에
   무엇이 실려 있든 어느 쪽 증거로도 불산입한다.

3. catalog_revision_consumed
   crosswalk 해시 동봉 시에만, 단 한 번만 소진된다. 두 번째 개정과 crosswalk
   없는 개정은 fold가 거부한다(무시 + 기록). 개정은 누적 카운터를 리셋하지
   않는다 - 개정 전 세션/인스턴스 집계를 그대로 승계하며, 개정 후 동일
   기준이 재충족되면 추가 개정 없이 조건이 성립한다.

4. refutation_condition_met (기계 방출)
   조건 성립은 이벤트가 아니라 fold가 증거에서 도출한다. 스트림에 실린 이
   이벤트는 fold 파생 조건과 대조해 지지/불지지로 기록될 뿐, 그 자체로는
   어떤 판정 상태도 만들지 않는다.

5. refutation_acknowledged (인간 확정)
   핵심반증 확정은 fold 파생 상태 'core_refuted'가 아니라 '조건 성립 AND
   인간 refutation_acknowledged 존재'일 때만이다. 조건이 성립하지 않은
   시점의 acknowledged는 효력 없이 기록만 남는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from xout.events import Event, EventType, StrikeEvent

logger = logging.getLogger(__name__)

CATALOG_SEALED_VERSION = "v1"
CATALOG_REVISED_VERSION = "v2"

REQUIRED_VALID_SESSIONS_KEY = "required_valid_sessions"
REQUIRED_INSTANCES_KEY = "required_discriminative_instances"
DISCRIMINATIVE_INSTANCES_KEY = "discriminative_instances"
CORRECT_RESTORATIONS_KEY = "correct_restorations"
MIS_RESTORATIONS_KEY = "mis_restorations"
CROSSWALK_KEY = "crosswalk"


class JudgmentViolation(RuntimeError):
    """판정 계약 위반."""


@dataclass(frozen=True, slots=True)
class SealedBasis:
    """prereg_sealed payload에서 파생된 판정 기준 - 수치의 유일한 런타임 통로."""

    event_id: str
    catalog_version: str
    digest: str
    required_valid_sessions: int
    required_discriminative_instances: int


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """소진된 카탈로그 개정 - crosswalk 해시가 동봉된 단 한 건."""

    event_id: str
    crosswalk: str
    from_version: str
    to_version: str


@dataclass(frozen=True, slots=True)
class RejectedJudgmentEvent:
    """fold가 거부한 판정 이벤트 - 상태를 바꾸지 않고 기록만 남긴다."""

    event_id: str
    event_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class JudgmentState:
    """자기반증 판정 fold의 전체 파생 상태 - 저장되지 않는다."""

    basis: SealedBasis | None
    catalog_version: str | None
    valid_sessions: int
    voided_sessions: int
    discriminative_instances: int
    correct_restorations: int
    mis_restorations: int
    revision: RevisionRecord | None
    rejected: tuple[RejectedJudgmentEvent, ...]
    condition_met: bool
    supported_condition_events: tuple[str, ...]
    acknowledged_event_ids: tuple[str, ...]

    @property
    def catalog_frozen(self) -> bool:
        return self.basis is not None

    @property
    def revision_consumed(self) -> bool:
        return self.revision is not None

    @property
    def acknowledged(self) -> bool:
        return bool(self.acknowledged_event_ids)

    @property
    def core_refutation_confirmed(self) -> bool:
        """확정 = 조건 성립 AND 인간 acknowledged - 별도 플래그는 존재하지 않는다."""
        return self.condition_met and self.acknowledged


def _positive_int(value: Any) -> int | None:
    """양의 정수만 판정 기준으로 인정한다 - bool은 정수가 아니다."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _evidence_int(payload: Mapping[str, Any], key: str) -> int:
    """세션 증거 수치 - 정수가 아니거나 음수면 불산입(0)하고 경고를 남긴다."""
    value = payload.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning("정수가 아닌 판정 증거 - 불산입: %s=%r", key, value)
        return 0
    if value < 0:
        logger.warning("음수 판정 증거 - 불산입: %s=%r", key, value)
        return 0
    return value


def _parse_basis(event: Event) -> SealedBasis | None:
    """prereg_sealed payload를 판정 기준으로 해석한다 - 수치 누락 시 None."""
    payload = event.payload
    sessions_needed = _positive_int(payload.get(REQUIRED_VALID_SESSIONS_KEY))
    instances_needed = _positive_int(payload.get(REQUIRED_INSTANCES_KEY))
    if sessions_needed is None or instances_needed is None:
        return None
    return SealedBasis(
        event_id=event.event_id,
        catalog_version=str(payload.get("catalog_version", CATALOG_SEALED_VERSION)),
        digest=str(payload.get("digest", "")),
        required_valid_sessions=sessions_needed,
        required_discriminative_instances=instances_needed,
    )


def fold_judgment(events: Iterable[StrikeEvent | Event]) -> JudgmentState:
    """이벤트 prefix에 대한 순수 fold - 같은 입력은 항상 같은 판정 상태를 낳는다."""
    basis: SealedBasis | None = None
    catalog_version: str | None = None
    valid_sessions = 0
    voided_sessions = 0
    instances = 0
    correct = 0
    mis = 0
    revision: RevisionRecord | None = None
    rejected: list[RejectedJudgmentEvent] = []
    supported: list[str] = []
    acknowledged: list[str] = []

    def condition() -> bool:
        """트리거 - 동결 + 유효 세션 + 판별시험-통과 인스턴스 + 오복원 >= 정복원."""
        return (
            basis is not None
            and valid_sessions >= basis.required_valid_sessions
            and instances >= basis.required_discriminative_instances
            and correct + mis > 0
            and mis >= correct
        )

    def reject(event: Event, reason: str, *, warning: bool = True) -> None:
        log = logger.warning if warning else logger.debug
        log("판정 이벤트 거부 - %s: %s", reason, event.event_id)
        rejected.append(
            RejectedJudgmentEvent(
                event_id=event.event_id, event_type=event.type.value, reason=reason
            )
        )

    for event in events:
        if not isinstance(event, Event):
            continue  # 긋기 등 판정 외 이벤트는 판정 fold의 입력이 아니다.

        etype = event.type
        payload = event.payload

        if etype is EventType.PREREG_SEALED:
            if basis is not None:
                reject(event, "이미 봉인된 판정 기준 - 재봉인 거부")
                continue
            candidate = _parse_basis(event)
            if candidate is None:
                reject(event, "판정 기준 수치 누락/위반 - 봉인 불성립")
                continue
            basis = candidate
            catalog_version = candidate.catalog_version
            continue

        if etype is EventType.SESSION_VALIDATED:
            declared_profile = payload.get("profile")
            if (
                isinstance(declared_profile, str)
                and declared_profile != "validation"
            ):
                reject(
                    event,
                    "검증 프로파일 밖 세션 - 증거 불산입",
                    warning=False,
                )
                continue
            if basis is None:
                reject(event, "봉인 이전의 검증 세션 - 증거 불산입")
                continue
            valid_sessions += 1
            instances += _evidence_int(payload, DISCRIMINATIVE_INSTANCES_KEY)
            correct += _evidence_int(payload, CORRECT_RESTORATIONS_KEY)
            mis += _evidence_int(payload, MIS_RESTORATIONS_KEY)
            continue

        if etype is EventType.SESSION_VOIDED:
            # VOID 세션의 인스턴스는 payload와 무관하게 어느 쪽 증거로도 불산입한다.
            voided_sessions += 1
            continue

        if etype is EventType.CATALOG_REVISION_CONSUMED:
            crosswalk = str(payload.get(CROSSWALK_KEY, "") or "")
            if basis is None:
                reject(event, "동결된 카탈로그가 없는 개정 - 거부")
                continue
            if not crosswalk:
                reject(event, "crosswalk 해시 미동봉 개정 - 거부")
                continue
            if revision is not None:
                reject(event, "개정 소진 한도 초과 - 두 번째 개정 거부")
                continue
            revision = RevisionRecord(
                event_id=event.event_id,
                crosswalk=crosswalk,
                from_version=catalog_version or basis.catalog_version,
                to_version=str(payload.get("to_version", CATALOG_REVISED_VERSION)),
            )
            catalog_version = revision.to_version
            # 누적 카운터는 리셋하지 않는다 - 개정 전 세션/인스턴스 집계를 승계한다.
            continue

        if etype is EventType.REFUTATION_CONDITION_MET:
            if condition():
                supported.append(event.event_id)
            else:
                reject(event, "fold 파생 조건 불성립 - 지지되지 않는 기계 방출")
            continue

        if etype is EventType.REFUTATION_ACKNOWLEDGED:
            if condition():
                acknowledged.append(event.event_id)
            else:
                reject(event, "조건 성립 전의 acknowledged - 효력 없음")
            continue

        # 그 밖의 이벤트 타입(session_start, probe 등)은 판정에 영향을 주지 않는다.

    return JudgmentState(
        basis=basis,
        catalog_version=catalog_version,
        valid_sessions=valid_sessions,
        voided_sessions=voided_sessions,
        discriminative_instances=instances,
        correct_restorations=correct,
        mis_restorations=mis,
        revision=revision,
        rejected=tuple(rejected),
        condition_met=condition(),
        supported_condition_events=tuple(supported),
        acknowledged_event_ids=tuple(acknowledged),
    )


def emit_condition_met(state: JudgmentState, session_id: str) -> Event | None:
    """기계 방출 - fold 파생 조건이 성립할 때만 refutation_condition_met을 만든다."""
    if not state.condition_met:
        return None
    return Event(
        type=EventType.REFUTATION_CONDITION_MET,
        session_id=session_id,
        payload={
            "catalog_version": state.catalog_version,
            "valid_sessions": state.valid_sessions,
            DISCRIMINATIVE_INSTANCES_KEY: state.discriminative_instances,
            CORRECT_RESTORATIONS_KEY: state.correct_restorations,
            MIS_RESTORATIONS_KEY: state.mis_restorations,
        },
    )


def acknowledge(session_id: str, actor: str, **payload: Any) -> Event:
    """인간 확정 이벤트 생성자 - 확정 주체를 비울 수 없다."""
    if not actor:
        raise JudgmentViolation("refutation_acknowledged는 확정 주체를 명시해야 한다")
    return Event(
        type=EventType.REFUTATION_ACKNOWLEDGED,
        session_id=session_id,
        payload={"actor": actor, **payload},
    )
