"""xout CLI - /xout 스킬과 터미널이 쓰는 단일 진입점.

세션 런타임(xout.state)은 시각을 조회하지 않는다. 벽시계 읽기(재심 배너 판정),
터미널 입출력, 동의 원장 적재 같은 바깥세상 접점은 전부 이 경계에서 끝낸다.

명령:
  open         일반(product) 세션 - 터미널에서 15긋기 완주 시 착지
  validate     검증(validation) 세션 - 판별 13 + 미러 프로브 2, 착지 없음
  recheck      4막 경량 재심 - manifest 재심 큐 선두를 5-7긋기로 재시험
  status       manifest/재심 배너/자기반증 판정 fold 요약
  land         저장된 이벤트 스트림에서 산출물 재착지 (수기 편집 감지 시 차단)
  enable       CLAUDE.md @import 한 줄 추가 - --grant 허가 레코드 필수
  rollback     @import 한 줄 제거 - 전체 롤백 지점
  optin        수기 룰 하나를 반증 대상으로 opt-in (default-in 금지)
  acknowledge  refutation_condition_met에 대한 인간 확정 이벤트 기록
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from xout.backup import create_backup, inspect_backup
from xout.compiler import (
    GRADE_LABELS_BY_LANG,
    MANIFEST_JSON,
    XOUT_MD,
    CompiledRule as CompilerRule,
    HashMismatch,
    compile_rules,
    default_base_dir,
    write_outputs,
)
from xout.conflict import (
    CompiledRule as ConflictRule,
    ConsentKind,
    ConsentLedger,
    ConsentRecord,
    ConsentViolation,
    ManualRule,
    detect_conflicts,
)
from xout.events import Event, EventType, SchemaViolation, StrikeEvent
from xout.fixtures import (
    CONTEXT_IRREVERSIBLE,
    DEFAULT_LANG,
    SCENE_CONTEXTS,
    SUPPORTED_LANGS,
    load_pack,
    localize_skin,
    scan_repo_skin,
)
from xout.migrate import migrate_legacy_home
from xout.mine import Conflict, find_conflicts, mine, summarize
from xout.probe import (
    DEFAULT_RUNNER,
    DEFAULT_TIMEOUT,
    ProbeError,
    ProbeOutcome,
    RuleSpec,
    build_cases,
    build_prompt,
    probe,
    subprocess_runner,
    write_receipt,
)
from xout.doctor import app_version, run_doctor
from xout.exporter import EXPORT_FORMATS, render_export, write_export
from xout.judgment import acknowledge, emit_condition_met, fold_judgment
from xout.locking import LockTimeout, base_lock, base_runtime_lock
from xout.recheck import (
    DEFAULT_BUDGET,
    MANUAL_COMMAND,
    RecheckViolation,
    check_due,
)
from xout.session import (
    DEFAULT_PREREG_PATH,
    PROFILE_PRODUCT,
    PROFILE_RECHECK,
    PROFILE_VALIDATION,
)
from xout.sessions import latest_resumable, summarize_sessions
from xout.store import EventStore, StoreViolation
from xout.state import (
    AXIS_LABELS,
    ColdOpenSession,
    axis_label,
    RecoveryUnavailable,
    SessionComplete,
    StalePresentation,
)
from xout.writer import OwnedWriter

logger = logging.getLogger("xout")

CONSENT_FILE = "consent.jsonl"
MANUAL_RULES_FILE = "manual_rules.json"
JUDGMENT_SESSION_ID = "judgment-ledger"


# ------------------------------------------------------------------ 파일 경계


def _load_manifest(base_dir: Path) -> dict[str, Any] | None:
    with base_lock(base_dir):
        path = base_dir / MANIFEST_JSON
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("manifest를 읽지 못했다: %s", path)
            return None
    return document if isinstance(document, dict) else None


def _banner_text(manifest: Mapping[str, Any] | None) -> str | None:
    """재심 배너 - 벽시계는 여기(CLI 경계)에서만 읽는다."""
    if manifest is None:
        return None
    banner = check_due(manifest, datetime.now(timezone.utc))
    if banner.text is None:
        return None
    return f"{banner.text} - {MANUAL_COMMAND}로 재심에 들어갈 수 있다"


def _activation_state(base_dir: Path) -> dict[str, str | None]:
    writer = OwnedWriter(base_dir=base_dir)
    expected = writer.import_line()
    target = writer.claude_md_path
    output_exists = (base_dir / "XOUT.md").is_file()
    if not target.is_file():
        return {
            "status": "inactive",
            "path": str(target),
            "expected_import": expected,
            "remediation": (
                "xout enable --grant를 실행해라"
                if output_exists
                else "xout open으로 세션을 먼저 완주해라"
            ),
        }
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return {
            "status": "import-drift",
            "path": str(target),
            "expected_import": expected,
            "remediation": f"CLAUDE.md를 읽지 못했다: {exc}",
        }
    if expected in lines and output_exists:
        return {
            "status": "active",
            "path": str(target),
            "expected_import": expected,
            "remediation": None,
        }
    stale = [
        line
        for line in lines
        if line.startswith("@") and ("XOUT.md" in line or "POPPER.md" in line)
    ]
    if expected in lines:
        stale.append(expected)
    return {
        "status": "import-drift" if stale else "inactive",
        "path": str(target),
        "expected_import": expected,
        "remediation": (
            (
                "xout rollback 후 xout enable --grant를 실행해라"
                if output_exists
                else "xout rollback 후 xout open을 실행해라"
            )
            if stale
            else (
                "xout enable --grant를 실행해라"
                if output_exists
                else "xout open으로 세션을 먼저 완주해라"
            )
        ),
    }


def _load_consent(base_dir: Path) -> ConsentLedger:
    with base_lock(base_dir):
        path = base_dir / CONSENT_FILE
        ledger = ConsentLedger()
        if not path.exists():
            return ledger
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            logger.exception("동의 원장을 읽지 못했다: %s", path)
            return ledger
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            fields: dict[str, Any] = {
                "kind": ConsentKind(str(record.get("kind"))),
                "subject": str(record.get("subject", "")),
            }
            if record.get("record_id"):
                fields["record_id"] = str(record["record_id"])
            if record.get("at"):
                fields["at"] = str(record["at"])
            ledger.append(ConsentRecord(**fields))
        except (json.JSONDecodeError, ValueError, ConsentViolation, TypeError) as exc:
            logger.warning(
                "동의 원장 손상 - %s:%d (%s), 해당 줄을 무시한다", path, number, exc
            )
    return ledger


def _persist_consent(base_dir: Path, record: ConsentRecord) -> None:
    with base_lock(base_dir):
        path = base_dir / CONSENT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            )
            f.flush()
            os.fsync(f.fileno())


def _load_manual_rules(base_dir: Path) -> tuple[ManualRule, ...]:
    with base_lock(base_dir):
        path = base_dir / MANUAL_RULES_FILE
        if not path.exists():
            return ()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("수기 룰 파일을 읽지 못했다: %s", path)
            return ()
    rules: list[ManualRule] = []
    for entry in document.get("rules", ()):
        if not isinstance(entry, Mapping):
            continue
        rules.append(
            ManualRule(
                rule_id=str(entry.get("rule_id", "")),
                axis=str(entry.get("axis", "")),
                value=str(entry.get("value", "")),
                text=str(entry.get("text", "")),
                source_path=entry.get("source_path"),
            )
        )
    return tuple(rules)


def _conflict_rule(rule: CompilerRule) -> ConflictRule:
    return ConflictRule(
        rule_id=rule.rule_id,
        axis=rule.axis,
        value=rule.value,
        text=rule.text,
        corroboration_grade=rule.corroboration_grade,
        value_source=rule.value_source,
        strike_provenance=tuple(rule.provenance),
    )


def _conflicts_for(
    base_dir: Path,
) -> Callable[[tuple[CompilerRule, ...]], Sequence[Mapping[str, Any]]]:
    """착지 시점 충돌 탐지 - opt-in 수기 룰만 반증 대상으로 본다(AC6)."""

    def compute(rules: tuple[CompilerRule, ...]) -> Sequence[Mapping[str, Any]]:
        manual = _load_manual_rules(base_dir)
        if not manual:
            return ()
        consent = _load_consent(base_dir)
        report = detect_conflicts(
            manual,
            tuple(_conflict_rule(rule) for rule in rules),
            catalog_version="v1",
            consent=consent,
        )
        return report.report_rows()

    return compute


def _seal_payload() -> dict[str, Any] | None:
    """봉인 문서에서 판정 기준 payload를 파생한다 - 수치의 소유자는 그 문서다.

    금지 어휘(code_scan_guard)가 런타임에 존재하면 안 되므로 동결 항목은
    키 이름이 아니라 unit으로 식별한다: 검증 세션 수는 unit=="sessions",
    누적 판별 인스턴스는 unit이 "instances"로 시작하는 유일 항목이다.
    """
    try:
        document = json.loads(DEFAULT_PREREG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("봉인 문서를 읽지 못했다: %s", DEFAULT_PREREG_PATH)
        return None
    seal = document.get("seal", {})
    body = document.get("document", {})
    frozen = body.get("frozen_parameters", {})

    def by_unit(matcher: Callable[[str], bool]) -> int | None:
        values = [
            entry.get("value")
            for entry in frozen.values()
            if isinstance(entry, Mapping) and matcher(str(entry.get("unit", "")))
        ]
        if len(values) == 1 and isinstance(values[0], int):
            return values[0]
        return None

    sessions_needed = by_unit(lambda unit: unit == "sessions")
    instances_needed = by_unit(lambda unit: unit.startswith("instances"))
    if sessions_needed is None or instances_needed is None:
        logger.error("봉인 문서에서 판정 기준을 식별하지 못했다")
        return None
    return {
        "catalog_version": str(body.get("catalog_version", "v1")),
        "digest": str(seal.get("digest", "")),
        "required_valid_sessions": sessions_needed,
        "required_discriminative_instances": instances_needed,
    }


def _ensure_seal_event(store: EventStore) -> None:
    """검증 세션 이전에 prereg_sealed 이벤트가 스트림에 정확히 하나 있게 한다."""
    with store.lock:
        for event in store.load_all():
            if getattr(event, "type", None) is EventType.PREREG_SEALED:
                return
        payload = _seal_payload()
        if payload is None:
            return
        store.append(
            Event(type=EventType.PREREG_SEALED, session_id="prereg", payload=payload)
        )
    logger.info("봉인 기준 적재: prereg_sealed (digest=%s...)", payload["digest"][:12])


def _validation_gap_hours() -> float | None:
    """봉인 문서가 소유하는 검증 세션 간 최소 간격을 읽는다."""
    try:
        document = json.loads(DEFAULT_PREREG_PATH.read_text(encoding="utf-8"))
        threats = document["document"]["threats_to_validity"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        logger.exception("봉인 문서에서 검증 세션 간격을 읽지 못했다")
        return None
    entries = threats.values() if isinstance(threats, Mapping) else threats
    values = [
        entry.get("inter_session_gap_hours_min")
        for entry in entries
        if isinstance(entry, Mapping)
        and isinstance(entry.get("inter_session_gap_hours_min"), (int, float))
    ]
    if len(values) != 1 or values[0] <= 0:
        logger.error("봉인 문서의 검증 세션 간격 기준이 유일하지 않다")
        return None
    return float(values[0])


def _latest_validation_end(
    events: Sequence[Event | Any],
) -> datetime | None:
    validation_sessions = {
        event.session_id
        for event in events
        if isinstance(event, Event)
        and event.type is EventType.SESSION_START
        and event.payload.get("profile") == PROFILE_VALIDATION
    }
    ended: list[datetime] = []
    for event in events:
        if not isinstance(event, Event) or event.type not in (
            EventType.SESSION_VALIDATED,
            EventType.SESSION_VOIDED,
        ):
            continue
        if (
            event.payload.get("profile") != PROFILE_VALIDATION
            and event.session_id not in validation_sessions
        ):
            continue
        try:
            ended.append(datetime.fromisoformat(event.at))
        except ValueError:
            logger.warning("검증 종료 시각을 해석하지 못했다: %s", event.event_id)
    return max(ended) if ended else None


# ---------------------------------------------------------------- 서브 명령


def _runtime_exclusive(
    command: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    @wraps(command)
    def wrapped(args: argparse.Namespace) -> int:
        try:
            with base_runtime_lock(args.base_dir):
                return command(args)
        except LockTimeout:
            logger.error(
                "다른 xout 세션이 이 소유 디렉토리에서 실행 중이다: %s",
                Path(args.base_dir).expanduser().resolve(),
            )
            return 1

    return wrapped


def _args_lang(args: argparse.Namespace) -> str:
    return getattr(args, "lang", DEFAULT_LANG)


def _resumed_session(
    base: Path,
    store: EventStore,
    session_id: str,
    args: argparse.Namespace,
) -> ColdOpenSession:
    events = store.load_session(session_id)
    summary = latest_resumable(events, session_id)
    if summary is None:
        raise ValueError(f"재개 가능한 세션이 아니다: {session_id}")
    return ColdOpenSession(
        repo_root=args.repo,
        session_id=session_id,
        profile=summary.profile,
        store=store,
        land_dir=base,
        history=store.load_completed(),
        resume_events=events,
        banner=_banner_text(_load_manifest(base)),
        conflicts_for=_conflicts_for(base),
        lang=_args_lang(args),
    )


@_runtime_exclusive
def cmd_open(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    store = EventStore(base)
    if not args.new:
        candidates = [
            summary
            for summary in summarize_sessions(store.load_all())
            if summary.resumable and summary.profile == PROFILE_PRODUCT
        ]
        if len(candidates) > 1:
            logger.error("미완료 일반 세션이 %d건 있다", len(candidates))
            for candidate in candidates:
                logger.error(
                    "  %s  %d/%d  %s",
                    candidate.session_id,
                    candidate.slots_used,
                    candidate.slots_total,
                    candidate.updated_at,
                )
            logger.error("xout resume <session-id> 또는 xout open --new를 사용해라")
            return 1
        if candidates:
            candidate = candidates[0]
            logger.info(
                "미완료 세션 계속: %s (%d/%d)",
                candidate.session_id,
                candidate.slots_used,
                candidate.slots_total,
            )
            return _launch(
                _resumed_session(base, store, candidate.session_id, args),
                args,
            )
    session = ColdOpenSession(
        repo_root=args.repo,
        profile=PROFILE_PRODUCT,
        store=store,
        land_dir=base,
        history=store.load_completed(),
        banner=_banner_text(manifest),
        conflicts_for=_conflicts_for(base),
        lang=_args_lang(args),
    )
    return _launch(session, args)


def _launch(session: ColdOpenSession, args: argparse.Namespace) -> int:
    return _run_tui(session, Path(args.base_dir), lang=_args_lang(args))


def _grant_and_enable(base: Path) -> int:
    """import 허가 레코드를 남기고 소유 @import 한 줄을 추가한다."""
    writer = OwnedWriter(base_dir=base)
    record = ConsentRecord(
        kind=ConsentKind.IMPORT_PERMISSION_GRANTED,
        subject=str(writer.claude_md_path),
    )
    _persist_consent(base, record)
    outcome = writer.ensure_import(record)
    logger.info("결과: %s (%s)", outcome.reason, outcome.path)
    return 0 if outcome.reason in ("added", "already_present") else 1


_TUI_TARGETS = {"1": "left", "2": "right", "b": "both", "p": "pair"}

#: 세션 플로우 화면 문자열 - 언어는 렌더 계층에만 존재한다.
_TUI_MSG: dict[str, dict[str, str]] = {
    "ko": {
        "intro": "xout - 아닌 쪽에 X를 치세요.",
        "keys": "입력: 1/2=한쪽에 X, b=둘 다 X, p=이 페어로는 판별 불가, u=되돌리기, q=중단",
        "aborted": "중단 - 진행 상황은 저장됐다. 다시 실행하면 이어진다.",
        "allowed": "허용 입력: 1, 2, b, p, u, q",
        "rejected": "반영 거부: %s",
        "voided": "세션 무효: %s",
        "complete": "세션 완료 - 컴파일된 규칙:",
        "landed": "착지 완료: %s",
        "apply": "지금 CLAUDE.md에 적용할까요? [y/N] ",
        "later": "나중에 적용하려면: xout enable --grant / 취소는 xout undo",
    },
    "en": {
        "intro": "xout - cross out the one you never want.",
        "keys": "keys: 1/2=strike one side, b=strike both, p=pair can't discriminate, u=undo, q=quit",
        "aborted": "Stopped - progress is saved. Run again to continue.",
        "allowed": "allowed inputs: 1, 2, b, p, u, q",
        "rejected": "rejected: %s",
        "voided": "session voided: %s",
        "complete": "Session complete - compiled rules:",
        "landed": "landed: %s",
        "apply": "Apply to CLAUDE.md now? [y/N] ",
        "later": "Apply later with: xout enable --grant / undo with xout undo",
    },
    "ja": {
        "intro": "xout - 二度と見たくない方に X を。",
        "keys": "入力: 1/2=片方に X, b=両方に X, p=このペアでは判別不能, u=取り消し, q=中断",
        "aborted": "中断 - 進捗は保存済み。再実行すれば続きから。",
        "allowed": "使える入力: 1, 2, b, p, u, q",
        "rejected": "反映拒否: %s",
        "voided": "セッション無効: %s",
        "complete": "セッション完了 - コンパイルされたルール:",
        "landed": "着地完了: %s",
        "apply": "今すぐ CLAUDE.md に適用しますか？ [y/N] ",
        "later": "後で適用: xout enable --grant / 取り消し: xout undo",
    },
    "zh": {
        "intro": "xout - 给你再也不想看到的那个打 X。",
        "keys": "输入: 1/2=给一侧打 X, b=两侧都打 X, p=这一对无法区分, u=撤销, q=退出",
        "aborted": "已停止 - 进度已保存。再次运行即可继续。",
        "allowed": "可用输入: 1, 2, b, p, u, q",
        "rejected": "已拒绝: %s",
        "voided": "会话作废: %s",
        "complete": "会话完成 - 编译出的规则:",
        "landed": "已落地: %s",
        "apply": "现在应用到 CLAUDE.md 吗？ [y/N] ",
        "later": "稍后应用: xout enable --grant / 撤销: xout undo",
    },
}


def _run_tui(session: ColdOpenSession, base: Path, lang: str = DEFAULT_LANG) -> int:
    """터미널 세션 루프 - 웹과 같은 이벤트 원장 위에서 긋는다."""
    msg = _TUI_MSG.get(lang, _TUI_MSG["ko"])
    print(msg["intro"])
    print(msg["keys"])
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        pair = snap.pair
        print()
        print(f"[{snap.slots_used + 1}/{snap.slots_total}] {pair.axis_label}")
        print("  (1) " + pair.left_text.replace("\n", "\n      "))
        print("  (2) " + pair.right_text.replace("\n", "\n      "))
        try:
            choice = input("X> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            logger.info(msg["aborted"])
            return 0
        try:
            if choice in _TUI_TARGETS:
                session.strike(_TUI_TARGETS[choice], expected_pair_id=pair.pair_id)
            elif choice == "u":
                session.undo()
            elif choice == "q":
                logger.info(msg["aborted"])
                return 0
            else:
                print(msg["allowed"])
        except SessionComplete:
            break
        except (StalePresentation, RecoveryUnavailable, SchemaViolation) as exc:
            logger.error(msg["rejected"], exc)
    snap = session.snapshot()
    if snap.voided_reason:
        logger.error(msg["voided"], snap.voided_reason)
        return 1
    print()
    print(msg["complete"])
    for rule in snap.rules:
        print(f"  - {rule.text}")
    conflicts = find_conflicts(mine([Path.cwd()]), _rules_by_axis(snap.rules))
    if conflicts:
        print()
        _print_conflicts(conflicts, lang)
    if snap.landing is not None:
        logger.info(msg["landed"], base)
    try:
        answer = input(msg["apply"]).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "y":
        return _grant_and_enable(base)
    logger.info(msg["later"])
    return 0


def _headless_session(args: argparse.Namespace) -> ColdOpenSession:
    """서버 없이 쓰는 일반 세션 - 미완료 1건이면 재개, 없으면 새로 연다."""
    base = Path(args.base_dir)
    store = EventStore(base)
    candidates = [
        summary
        for summary in summarize_sessions(store.load_all())
        if summary.resumable and summary.profile == PROFILE_PRODUCT
    ]
    if len(candidates) > 1:
        raise ValueError(
            f"미완료 일반 세션이 {len(candidates)}건 있다 - xout resume <session-id>로 정리해라"
        )
    if candidates:
        return _resumed_session(base, store, candidates[0].session_id, args)
    return ColdOpenSession(
        repo_root=args.repo,
        profile=PROFILE_PRODUCT,
        store=store,
        land_dir=base,
        history=store.load_completed(),
        banner=_banner_text(_load_manifest(base)),
        conflicts_for=_conflicts_for(base),
        lang=_args_lang(args),
    )


CONTEXT_LABELS = {
    "routine": "일상 작업",
    CONTEXT_IRREVERSIBLE: "되돌리기 어려운 작업",
}

CONTEXT_LABELS_EN = {
    "routine": "routine-work",
    CONTEXT_IRREVERSIBLE: "hard-to-reverse-work",
}

CONTEXT_LABELS_BY_LANG = {
    "ko": CONTEXT_LABELS,
    "en": CONTEXT_LABELS_EN,
    "ja": {"routine": "日常作業", CONTEXT_IRREVERSIBLE: "取り消しにくい作業"},
    "zh": {"routine": "日常工作", CONTEXT_IRREVERSIBLE: "难以撤销的工作"},
}

#: xout why 출력 문자열 - 규칙 본문은 manifest에 착지된 언어 그대로 나온다.
_WHY_MSG: dict[str, dict[str, str]] = {
    "ko": {
        "rule": "규칙: {text}",
        "state": "상태: {grade} / 출처: {origin}",
        "origin_elicited": "당신의 X",
        "origin_prior": "추정 기본값 (아직 안 물어봄)",
        "no_evidence": "근거: 이 축을 겨눈 X가 아직 없다.",
        "evidence": "근거:",
        "line": "  - {context} 장면({scene})에서 {values}에 X (세션 {session})",
    },
    "en": {
        "rule": "rule: {text}",
        "state": "state: {grade} / source: {origin}",
        "origin_elicited": "your X",
        "origin_prior": "assumed default (never asked yet)",
        "no_evidence": "evidence: no X has targeted this axis yet.",
        "evidence": "evidence:",
        "line": "  - X'd {values} in the {context} scene ({scene}) (session {session})",
    },
    "ja": {
        "rule": "ルール: {text}",
        "state": "状態: {grade} / 出所: {origin}",
        "origin_elicited": "あなたの X",
        "origin_prior": "推定デフォルト（まだ尋ねていない）",
        "no_evidence": "根拠: この軸を狙った X はまだない。",
        "evidence": "根拠:",
        "line": "  - {context}の場面({scene})で {values} に X (セッション {session})",
    },
    "zh": {
        "rule": "规则: {text}",
        "state": "状态: {grade} / 来源: {origin}",
        "origin_elicited": "你的 X",
        "origin_prior": "推定默认值（尚未询问）",
        "no_evidence": "依据: 还没有 X 指向这个轴。",
        "evidence": "依据:",
        "line": "  - 在{context}场景({scene})给 {values} 打了 X (会话 {session})",
    },
}


_CONFLICT_MSG = {
    "ko": {
        "header": "프로젝트 규칙과 갈리는 줄 {count}건 (정면 충돌이면 프로젝트가 이긴다):",
        "none": "프로젝트 규칙 파일 중 당신의 규칙과 갈리는 줄이 없다.",
        "no_rules": "컴파일된 규칙이 없다 - 먼저 xout을 돌려라.",
        "line": "  - [{axis}] 프로젝트는 {observed}, 당신의 규칙은 {rule}  {path}:{line_no}  \"{text}\"",
    },
    "en": {
        "header": "{count} project rule line(s) disagree with your rules (the project wins on a direct conflict):",
        "none": "No project rule file disagrees with your rules.",
        "no_rules": "No compiled rules yet - run xout first.",
        "line": "  - [{axis}] project says {observed}, your rule says {rule}  {path}:{line_no}  \"{text}\"",
    },
    "ja": {
        "header": "プロジェクトの規則と食い違う行 {count}件 (正面衝突ならプロジェクトが優先):",
        "none": "プロジェクトの規則ファイルにあなたの規則と食い違う行はない。",
        "no_rules": "コンパイルされた規則がない - 先に xout を実行する。",
        "line": "  - [{axis}] プロジェクトは {observed}、あなたの規則は {rule}  {path}:{line_no}  \"{text}\"",
    },
    "zh": {
        "header": "与项目规则相左的行 {count} 条 (正面冲突时以项目为准):",
        "none": "项目规则文件中没有与你的规则相左的行。",
        "no_rules": "还没有编译好的规则 - 先运行 xout。",
        "line": "  - [{axis}] 项目要求 {observed}，你的规则是 {rule}  {path}:{line_no}  \"{text}\"",
    },
}


def _rules_by_axis(rules: Sequence[CompilerRule]) -> dict[str, tuple[str, str | None]]:
    return {rule.axis: (rule.value, rule.irreversible_value) for rule in rules}


def _manifest_rules_by_axis(
    manifest: dict[str, Any],
) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for entry in manifest.get("rules", []):
        axis, value = entry.get("axis"), entry.get("value")
        if isinstance(axis, str) and isinstance(value, str):
            out[axis] = (value, entry.get("irreversible_value"))
    return out


def _print_conflicts(conflicts: list[Conflict], lang: str, limit: int = 8) -> None:
    msg = _CONFLICT_MSG.get(lang, _CONFLICT_MSG["ko"])
    if not conflicts:
        print(msg["none"])
        return
    print(msg["header"].format(count=len(conflicts)))
    for conflict in conflicts[:limit]:
        print(
            msg["line"].format(
                axis=axis_label(conflict.axis, lang),
                observed=conflict.observed_value,
                rule=conflict.rule_value,
                path=conflict.path,
                line_no=conflict.line_no,
                text=conflict.line,
            )
        )
    if len(conflicts) > limit:
        print(f"  ... +{len(conflicts) - limit}")


def cmd_conflicts(args: argparse.Namespace) -> int:
    """컴파일된 규칙과 로컬 프로젝트 규칙 파일이 갈리는 줄을 보고한다 - 읽기전용."""
    lang = _args_lang(args)
    manifest = _load_manifest(Path(args.base_dir))
    if manifest is None:
        if args.json_output:
            print(json.dumps({"error": "no_rules"}))
        else:
            print(_CONFLICT_MSG.get(lang, _CONFLICT_MSG["ko"])["no_rules"])
        return 1
    roots = [Path(root) for root in (args.roots or ["."])]
    conflicts = find_conflicts(mine(roots), _manifest_rules_by_axis(manifest))
    if args.json_output:
        print(
            json.dumps(
                {"conflicts": [conflict.to_dict() for conflict in conflicts]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    _print_conflicts(conflicts, lang)
    return 0


_PROBE_MSG = {
    "ko": {
        "no_rules": "착지된 규칙이 없다 - 먼저 xout을 돌려라.",
        "runner_missing": "러너를 시작할 수 없다: {error}",
        "start": "탐침 {n}건 x 2회 (규칙 없이 / XOUT.md 앞세워) - 러너: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (규칙: {survivor})  {verdict}",
        "held": "유지",
        "moved": "이동",
        "missed": "불일치",
        "unparsed": "판독 불가",
        "summary": "규칙 유지 {held}/{cases} · 규칙이 선택을 움직임 {moved} · 규칙 없이도 일치 {bare} · 판독 불가 {unparsed}",
        "receipt": "영수증: {path}",
        "dry": "탐침 {n}건 준비됨 (러너 호출 없음)",
    },
    "en": {
        "no_rules": "No landed rules yet - run xout first.",
        "runner_missing": "Cannot start the runner: {error}",
        "start": "Probing {n} cases x 2 (bare / with XOUT.md) - runner: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (rule: {survivor})  {verdict}",
        "held": "held",
        "moved": "moved",
        "missed": "missed",
        "unparsed": "unparsed",
        "summary": "rule held {held}/{cases} · rule moved the choice {moved} · matched without rules {bare} · unparsed {unparsed}",
        "receipt": "receipt: {path}",
        "dry": "{n} cases prepared (runner not called)",
    },
    "ja": {
        "no_rules": "着地した規則がない - 先に xout を実行する。",
        "runner_missing": "ランナーを起動できない: {error}",
        "start": "探針 {n}件 x 2回 (規則なし / XOUT.md 付き) - ランナー: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (規則: {survivor})  {verdict}",
        "held": "維持",
        "moved": "移動",
        "missed": "不一致",
        "unparsed": "判読不能",
        "summary": "規則維持 {held}/{cases} · 規則が選択を動かした {moved} · 規則なしでも一致 {bare} · 判読不能 {unparsed}",
        "receipt": "レシート: {path}",
        "dry": "探針 {n}件を準備 (ランナー未呼び出し)",
    },
    "zh": {
        "no_rules": "还没有落地的规则 - 先运行 xout。",
        "runner_missing": "无法启动运行器: {error}",
        "start": "探针 {n} 例 x 2 次 (无规则 / 带 XOUT.md) - 运行器: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (规则: {survivor})  {verdict}",
        "held": "保持",
        "moved": "移动",
        "missed": "不符",
        "unparsed": "无法判读",
        "summary": "规则保持 {held}/{cases} · 规则改变了选择 {moved} · 无规则也一致 {bare} · 无法判读 {unparsed}",
        "receipt": "回执: {path}",
        "dry": "已准备 {n} 例 (未调用运行器)",
    },
}


def _probe_verdict(outcome: ProbeOutcome, msg: Mapping[str, str]) -> str:
    if outcome.bare_value is None or outcome.ruled_value is None:
        return msg["unparsed"]
    if outcome.moved:
        return msg["moved"]
    return msg["held"] if outcome.held else msg["missed"]


def cmd_probe(args: argparse.Namespace) -> int:
    """착지된 XOUT.md가 실제 에이전트의 선택을 움직이는지 외부 러너로 잰다 - 옵트인."""
    lang = _args_lang(args)
    msg = _PROBE_MSG.get(lang, _PROBE_MSG["ko"])
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    xout_path = base / XOUT_MD
    if manifest is None or not xout_path.is_file():
        print(json.dumps({"error": "no_rules"}) if args.json_output else msg["no_rules"])
        return 1
    rules = {
        entry["axis"]: RuleSpec(
            value=entry["value"],
            irreversible_value=entry.get("irreversible_value"),
            eliminated=tuple(entry.get("eliminated_values", [])),
        )
        for entry in manifest.get("rules", [])
        if isinstance(entry.get("axis"), str) and isinstance(entry.get("value"), str)
    }
    pack = load_pack(lang=lang)
    skin = localize_skin(scan_repo_skin(Path.cwd()), lang)
    cases = build_cases(pack, rules, skin, axes=args.axes or None)
    if args.quick:
        seen: set[str] = set()
        cases = tuple(c for c in cases if not (c.axis in seen or seen.add(c.axis)))
    if args.dry_run:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "cases": [
                            {"scene_id": c.scene_id, "axis": c.axis, "survivor": c.survivor,
                             "alternative": c.alternative, "shown_as_a": c.first}
                            for c in cases
                        ],
                        "prompt_sample": build_prompt(cases[0], lang, None) if cases else "",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(msg["dry"].format(n=len(cases)))
        return 0
    # Windows 경로의 역슬래시를 이스케이프로 먹지 않도록 non-POSIX 분할.
    command = shlex.split(args.runner, posix=os.name != "nt")
    try:
        runner = subprocess_runner(command, timeout=args.timeout)
    except ProbeError as exc:
        print(json.dumps({"error": str(exc)}) if args.json_output else msg["runner_missing"].format(error=exc))
        return 2
    if not args.json_output:
        print(msg["start"].format(n=len(cases), runner=args.runner))

    def on_outcome(outcome: ProbeOutcome) -> None:
        if args.json_output:
            return
        print(
            msg["line"].format(
                axis=axis_label(outcome.case.axis, lang),
                scene=outcome.case.scene_id,
                bare=outcome.bare_value or "?",
                ruled=outcome.ruled_value or "?",
                survivor=outcome.case.survivor,
                verdict=_probe_verdict(outcome, msg),
            )
        )

    try:
        report = probe(
            cases, xout_path.read_text(encoding="utf-8"), runner, lang, command, on_outcome
        )
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}) if args.json_output else msg["runner_missing"].format(error=exc))
        return 2
    receipt = write_receipt(base, report)
    if args.json_output:
        payload = report.to_dict()
        payload["receipt_path"] = str(receipt)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print()
    print(msg["summary"].format(bare=report.summary["bare_matched"], **report.summary))
    print(msg["receipt"].format(path=receipt))
    return 0


_MINE_MSG = {
    "ko": {
        "none": "관측 없음 - 스캔한 규칙 파일에서 이 축을 겨눈 줄을 찾지 못했다.",
        "header": "로컬 채굴 보고 (읽기전용, 휴리스틱) - 관측 {count}건",
        "hint": "세션에서 X를 칠 때 이 관측과 교차 확인해라: xout open",
        "no_files": "규칙 파일을 찾지 못했다 (CLAUDE.md / AGENTS.md / .cursorrules 류)",
    },
    "en": {
        "none": "no observation - no scanned line targets this axis.",
        "header": "local mining report (read-only, heuristic) - {count} observations",
        "hint": "cross-check these against your strikes: xout open",
        "no_files": "no rule files found (CLAUDE.md / AGENTS.md / .cursorrules etc.)",
    },
    "ja": {
        "none": "観測なし - スキャンしたルールファイルにこの軸を狙う行はなかった。",
        "header": "ローカル採掘レポート（読み取り専用・ヒューリスティック） - 観測 {count} 件",
        "hint": "セッションで X を付けるとき、この観測と突き合わせる: xout open",
        "no_files": "ルールファイルが見つからない (CLAUDE.md / AGENTS.md / .cursorrules など)",
    },
    "zh": {
        "none": "无观测 - 扫描到的规则文件里没有针对这个轴的行。",
        "header": "本地挖掘报告（只读、启发式） - 观测 {count} 条",
        "hint": "在会话里打 X 时与这些观测交叉核对: xout open",
        "no_files": "没有找到规则文件 (CLAUDE.md / AGENTS.md / .cursorrules 等)",
    },
}


def cmd_mine(args: argparse.Namespace) -> int:
    """로컬 규칙 파일에서 축 관측을 채굴한다 - 아무것도 쓰지 않는다."""
    lang = _args_lang(args)
    roots = [Path(root) for root in (args.roots or ["."])]
    observations = mine(roots)
    if args.json_output:
        print(
            json.dumps(
                {
                    "observations": [obs.to_dict() for obs in observations],
                    "summary": summarize(observations),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    msg = _MINE_MSG.get(lang, _MINE_MSG["ko"])
    if not observations:
        print(msg["no_files"] if not any(
            Path(root).exists() for root in (args.roots or ["."])
        ) else msg["header"].format(count=0))
    else:
        print(msg["header"].format(count=len(observations)))
    counts = summarize(observations)
    by_axis: dict[str, list] = {}
    for obs in observations:
        by_axis.setdefault(obs.axis, []).append(obs)
    for axis in sorted(counts):
        print()
        print(f"[{axis_label(axis, lang)}]")
        found = by_axis.get(axis, [])
        if not found:
            print(f"  {msg['none']}")
            continue
        for obs in found[:6]:
            print(f"  - {obs.value}  {obs.path}:{obs.line_no}  \"{obs.line}\"")
        if len(found) > 6:
            print(f"  ... +{len(found) - 6}")
    print()
    print(msg["hint"])
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    """규칙 -> 그 규칙을 만든 X의 증거 사슬을 소급한다."""
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    if manifest is None:
        logger.error("착지된 manifest가 없다 - xout 세션을 먼저 완주해라")
        return 1
    rules = {
        str(entry.get("axis")): entry
        for entry in manifest.get("rules", ())
        if isinstance(entry, Mapping)
    }
    axes = [args.axis] if args.axis else sorted(rules)
    events_by_id: dict[str, StrikeEvent] = {}
    for event in EventStore(base).load_all():
        if isinstance(event, StrikeEvent):
            events_by_id[str(event.event_id)] = event
    for axis in axes:
        entry = rules.get(axis)
        if entry is None:
            logger.error("manifest에 없는 축: %s", axis)
            return 1
        lang = _args_lang(args)
        why = _WHY_MSG.get(lang, _WHY_MSG["ko"])
        contexts = CONTEXT_LABELS_BY_LANG.get(lang, CONTEXT_LABELS)
        print(f"[{axis_label(axis, lang)}]")
        print(why["rule"].format(text=entry.get("rule")))
        grade = str(entry.get("corroboration_grade", ""))
        source = str(entry.get("value_source", ""))
        origin = (
            why["origin_elicited"] if source == "elicited" else why["origin_prior"]
        )
        grade_label = GRADE_LABELS_BY_LANG.get(lang, {}).get(grade, grade)
        print(why["state"].format(grade=grade_label, origin=origin))
        provenance = [
            str(eid)
            for eid in dict.fromkeys(entry.get("refutation_provenance", ()))
        ]
        struck = [events_by_id[eid] for eid in provenance if eid in events_by_id]
        if not struck:
            print(why["no_evidence"])
            print()
            continue
        print(why["evidence"])
        for event in struck:
            context = SCENE_CONTEXTS.get(event.scene_id, "routine")
            values = [
                refutation.value
                for refutation in event.refutations
                if refutation.axis == axis
            ]
            print(
                why["line"].format(
                    context=contexts.get(context, context),
                    scene=event.scene_id,
                    values=", ".join(values),
                    session=event.session_id[:8],
                )
            )
        print()
    return 0


@_runtime_exclusive
def cmd_pair(args: argparse.Namespace) -> int:
    try:
        session = _headless_session(args)
    except (ValueError, SchemaViolation) as exc:
        logger.error("%s", exc)
        return 1
    print(json.dumps(session.snapshot().to_dict(), ensure_ascii=False, indent=2))
    return 0


@_runtime_exclusive
def cmd_strike(args: argparse.Namespace) -> int:
    try:
        session = _headless_session(args)
    except (ValueError, SchemaViolation) as exc:
        logger.error("%s", exc)
        return 1
    try:
        snapshot = session.strike(
            args.target,
            expected_pair_id=args.pair_id,
            expected_slot=args.slot,
        )
    except (SessionComplete, StalePresentation, SchemaViolation) as exc:
        logger.error("긋기 거부: %s", exc)
        return 1
    print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    return 0


@_runtime_exclusive
def cmd_resume(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    store = EventStore(base)
    all_events = store.load_all()
    resumable = [
        summary
        for summary in summarize_sessions(all_events)
        if summary.resumable
        and (args.session_id is None or summary.session_id == args.session_id)
    ]
    if args.session_id is None and len(resumable) > 1:
        logger.error(
            "재개 가능한 세션이 %d건 있다 - session-id를 지정해라", len(resumable)
        )
        for summary in resumable:
            logger.error(
                "  %s  %s  %d/%d",
                summary.session_id,
                summary.profile,
                summary.slots_used,
                summary.slots_total,
            )
        return 1
    candidate = resumable[0] if resumable else None
    if candidate is None:
        target = f" {args.session_id}" if args.session_id else ""
        logger.error("재개 가능한 세션이 없다%s", target)
        return 1
    try:
        session = _resumed_session(base, store, candidate.session_id, args)
    except (ValueError, SchemaViolation, SessionComplete) as exc:
        logger.error("세션 재개 실패 - %s", exc)
        return 1
    logger.info(
        "세션 재개: %s (%s %d/%d)",
        candidate.session_id,
        candidate.profile,
        candidate.slots_used,
        candidate.slots_total,
    )
    return _launch(session, args)


@_runtime_exclusive
def cmd_validate(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    store = EventStore(base)
    _ensure_seal_event(store)
    all_events = store.load_all()
    gap_hours = _validation_gap_hours()
    if gap_hours is None:
        return 1
    previous = _latest_validation_end(all_events)
    now = datetime.now(timezone.utc)
    if previous is not None and (now - previous).total_seconds() < gap_hours * 3600:
        logger.error(
            "검증 세션 간 봉인 간격이 지나지 않았다 - 마지막 종료 %s",
            previous.isoformat(),
        )
        return 1
    session = ColdOpenSession(
        repo_root=args.repo,
        profile=PROFILE_VALIDATION,
        store=store,
        land_dir=base,
        history=store.load_completed(),
        lang=_args_lang(args),
    )
    return _launch(session, args)


@_runtime_exclusive
def cmd_recheck(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    if manifest is None:
        logger.error("착지된 manifest가 없다 - 일반 세션(xout open)을 먼저 완주해라")
        return 1
    queue = manifest.get("recheck_queue")
    if not isinstance(queue, Sequence) or isinstance(queue, (str, bytes)) or not queue:
        logger.info("재심 대기 0건 - 열 재심 세션이 없다")
        return 0
    store = EventStore(base)
    try:
        session = ColdOpenSession(
            repo_root=args.repo,
            profile=PROFILE_RECHECK,
            store=store,
            land_dir=base,
            history=store.load_completed(),
            recheck_manifest=manifest,
            recheck_budget=args.budget,
            conflicts_for=_conflicts_for(base),
            lang=_args_lang(args),
        )
    except RecheckViolation as exc:
        logger.error("%s", exc)
        return 1
    return _launch(session, args)


def cmd_status(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    activation = _activation_state(base)
    banner = _banner_text(manifest)
    store = EventStore(base)
    events = store.load_all()
    summaries = summarize_sessions(events)
    state = fold_judgment(events)
    if args.json_output:
        print(
            json.dumps(
                {
                    "artifact": "popper_status",
                    "version": app_version(),
                    "base_dir": str(base.expanduser().resolve()),
                    "manifest": manifest,
                    "banner": banner,
                    "activation": activation,
                    "sessions": [summary.to_dict() for summary in summaries],
                    "judgment": {
                        "valid_sessions": state.valid_sessions,
                        "discriminative_instances": state.discriminative_instances,
                        "correct_restorations": state.correct_restorations,
                        "mis_restorations": state.mis_restorations,
                        "condition_met": state.condition_met,
                        "core_refutation_confirmed": state.core_refutation_confirmed,
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if manifest is None:
        logger.info("착지된 산출물이 없다 - xout open으로 첫 세션을 완주해라")
    else:
        logger.info("착지 디렉토리: %s", base)
        logger.info("마지막 착지: %s", manifest.get("generated_at"))
        logger.info("마지막 재심: %s", manifest.get("last_review"))
        logger.info("남은 가설 조합: %s", manifest.get("remaining_combinations"))
        queue = manifest.get("recheck_queue") or ()
        logger.info("재심 대기: %d건", len(queue))
        if banner:
            logger.info("배너: %s", banner)
    logger.info("활성화: %s", activation["status"])
    if activation["remediation"]:
        logger.info("활성화 다음 행동: %s", activation["remediation"])
    logger.info("저장된 세션: %d개, 이벤트 %d건", len(store.session_ids()), len(events))
    in_progress = [summary for summary in summaries if summary.resumable]
    if in_progress:
        logger.info("재개 가능: %d건 (xout resume)", len(in_progress))
    logger.info(
        "자기반증 판정: 유효 검증 세션 %d, 판별 인스턴스 %d, 정복원 %d, 오복원 %d",
        state.valid_sessions,
        state.discriminative_instances,
        state.correct_restorations,
        state.mis_restorations,
    )
    if state.core_refutation_confirmed:
        logger.info("핵심 반증 확정 - 긋기-only 접근이 반증됐다 (직접편집 전환 피벗)")
    elif state.condition_met:
        logger.info(
            "refutation_condition_met 성립 - 확정은 xout acknowledge --actor <이름>"
        )
    return 0


def cmd_land(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    store = EventStore(base)
    with store.lock:
        events = store.load_completed()
        if not events:
            logger.error("저장된 이벤트가 없다 - 착지할 스트림이 없다")
            return 1
        try:
            manifest = _load_manifest(base) or {}
            result = write_outputs(
                events,
                base_dir=base,
                session_id=manifest.get("session_id"),
                acknowledge_mismatch=args.acknowledge_mismatch,
                conflicts=_conflicts_for(base)(compile_rules(events)),
            )
        except HashMismatch as e:
            logger.error("착지 차단 - content hash 불일치 (silent overwrite 금지)")
            for record in e.records:
                logger.error("  %s (%s)", record.get("path"), record.get("reason"))
            logger.error("의도한 재착지라면 --acknowledge-mismatch를 붙여라")
            return 1
    logger.info("착지 완료: %s", result.base_dir)
    for path in result.written:
        logger.info("  %s", path)
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    writer = OwnedWriter(base_dir=base)
    if not args.grant:
        logger.info("추가될 한 줄: %s", writer.import_line())
        logger.info(
            "사용자 파일은 허가 없이는 건드리지 않는다 - --grant로 허가를 명시해라"
        )
        return 1
    return _grant_and_enable(base)


def cmd_rollback(args: argparse.Namespace) -> int:
    writer = OwnedWriter(base_dir=Path(args.base_dir))
    outcome = writer.remove_import()
    logger.info("결과: %s (%s)", outcome.reason, outcome.path)
    return 0 if outcome.reason in ("removed", "not_present", "not_owned") else 1


def cmd_optin(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    record = ConsentRecord(kind=ConsentKind.MANUAL_RULE_OPTED_IN, subject=args.rule_id)
    _persist_consent(base, record)
    logger.info("수기 룰 opt-in 기록: %s", args.rule_id)
    return 0


def cmd_acknowledge(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    store = EventStore(base)
    with store.lock:
        state = fold_judgment(store.load_all())
        if not state.condition_met:
            logger.error(
                "refutation_condition_met 미성립 - 인간 확정은 조건 성립 후에만 가능하다"
            )
            return 1
        condition = emit_condition_met(state, JUDGMENT_SESSION_ID)
        if condition is not None and not state.supported_condition_events:
            store.append(condition)
            logger.info("기계 방출 기록: refutation_condition_met")
        store.append(acknowledge(JUDGMENT_SESSION_ID, args.actor))
    logger.info("인간 확정 기록: refutation_acknowledged (actor=%s)", args.actor)
    logger.info("핵심 반증 확정 - 긋기-only 접근을 직접편집 전환으로 피벗한다")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    store = EventStore(base)
    if args.session_id:
        events = store.load_session(args.session_id)
        summaries = summarize_sessions(events)
        if not summaries:
            logger.error("세션을 찾지 못했다: %s", args.session_id)
            return 1
        payload = {
            "summary": summaries[0].to_dict(),
            "events": [
                event.to_dict()
                for event in (events[-args.events :] if args.events else ())
            ],
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            summary = summaries[0]
            logger.info(
                "%s  %s  %s  %d/%d  last=%s",
                summary.session_id,
                summary.profile,
                summary.status,
                summary.slots_used,
                summary.slots_total,
                summary.updated_at,
            )
            for event in events[-args.events :] if args.events else ():
                logger.info(
                    "  %s  %s  %s",
                    event.at,
                    event.type.value,
                    event.event_id,
                )
        return 0

    summaries = summarize_sessions(store.load_all(), limit=args.limit)
    if args.json_output:
        print(
            json.dumps(
                {
                    "artifact": "popper_sessions",
                    "sessions": [summary.to_dict() for summary in summaries],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not summaries:
        logger.info("저장된 사용자 세션이 없다")
        return 0
    for summary in summaries:
        marker = "*" if summary.resumable else " "
        logger.info(
            "%s %s  %-10s %-11s %2d/%-2d  %s",
            marker,
            summary.session_id,
            summary.profile,
            summary.status,
            summary.slots_used,
            summary.slots_total,
            summary.updated_at,
        )
    if any(summary.resumable for summary in summaries):
        logger.info("* xout resume [session-id]로 계속할 수 있다")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(args.base_dir)
    if args.json_output:
        print(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        logger.info("xout %s doctor - %s", report.version, report.base_dir)
        for check in report.checks:
            marker = "OK" if check.healthy else "ERROR"
            logger.info("[%s] %s - %s", marker, check.name, check.evidence)
            if check.remediation:
                logger.info("       복구: %s", check.remediation)
    return 0 if report.healthy else 1


def cmd_export(args: argparse.Namespace) -> int:
    store = EventStore(Path(args.base_dir))
    events = store.load_completed()
    if not events:
        logger.error("내보낼 완료 세션이 없다")
        return 1
    body = render_export(events, args.format)
    if args.output is None:
        print(body, end="")
    else:
        target = write_export(args.output, body)
        logger.info("%s 형식 내보내기: %s", args.format, target)
    return 0


def cmd_data_backup(args: argparse.Namespace) -> int:
    try:
        result = create_backup(args.base_dir, args.output)
    except (OSError, ValueError) as exc:
        logger.error("백업 생성 실패 - %s", exc)
        return 1
    logger.info(
        "백업 완료: %s (파일 %d, 세션 %d)",
        result.path,
        result.file_count,
        result.session_count,
    )
    logger.info("체크섬: %s", result.checksum_path)
    return 0


def cmd_data_inspect(args: argparse.Namespace) -> int:
    report = inspect_backup(args.path)
    if args.json_output:
        print(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        logger.info("백업: %s", report.path)
        logger.info(
            "상태=%s schema=%d files=%d sessions=%d latest=%s",
            "healthy" if report.healthy else "corrupt",
            report.schema_version,
            report.file_count,
            report.session_count,
            report.latest_session_at,
        )
        for error in report.errors:
            logger.error("  %s", error)
    return 0 if report.healthy else 1


def cmd_version(args: argparse.Namespace) -> int:
    pack = load_pack()
    print(f"xout {app_version()} (catalog {pack.catalog_version}, backup schema 1)")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    logger.info("xout은 자동 네트워크 확인이나 자동 업그레이드를 하지 않는다.")
    logger.info("uv:   uv tool upgrade xout")
    logger.info("pipx: pipx upgrade xout")
    logger.info("wheel/plugin: 새 공식 릴리스를 설치한 뒤 xout doctor를 실행해라")
    return 0


# ------------------------------------------------------------------- 파서


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-dir",
        default=str(default_base_dir()),
        help="xout 소유 디렉토리 (기본 ~/.claude/xout)",
    )
    parser.add_argument(
        "--lang",
        choices=SUPPORTED_LANGS,
        default=DEFAULT_LANG,
        help="세션 언어 - 페어/규칙/화면 텍스트 (기본 ko)",
    )


def _add_serve_common(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="슬롯 치환에 쓸 대상 레포 경로. 생략하면 일반 skin을 쓴다",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xout",
        description=(
            "아닌 행동에 X를 쳐서 Claude Code 규칙을 만드는 도구.\n"
            "  xout         세션 시작 (미완료 세션이 있으면 이어서)\n"
            "  xout undo    적용 취소 - 소유한 @import 한 줄만 제거\n"
            "  xout status  규칙 8줄과 활성화 상태 확인"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p_open = sub.add_parser("open", help="일반 세션을 연다 (15긋기, 완주 시 착지)")
    _add_serve_common(p_open)
    p_open.add_argument(
        "--new",
        action="store_true",
        help="미완료 일반 세션이 있어도 새 세션을 연다",
    )
    p_open.set_defaults(func=cmd_open)

    p_mine = sub.add_parser(
        "mine",
        help="로컬 규칙 파일(CLAUDE.md/AGENTS.md/.cursorrules)에서 축 관측을 채굴 (읽기전용)",
    )
    _add_common(p_mine)
    p_mine.add_argument("roots", nargs="*", help="스캔할 루트 (기본: 현재 디렉토리)")
    p_mine.add_argument("--json", dest="json_output", action="store_true")
    p_mine.set_defaults(func=cmd_mine)

    p_conflicts = sub.add_parser(
        "conflicts",
        help="컴파일된 규칙과 프로젝트 규칙 파일이 갈리는 줄을 보고 (읽기전용)",
    )
    p_conflicts.add_argument("roots", nargs="*", help="스캔할 루트 (기본: 현재 디렉토리)")
    p_conflicts.add_argument("--json", dest="json_output", action="store_true")
    _add_common(p_conflicts)
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_probe = sub.add_parser(
        "probe",
        help="착지된 XOUT.md가 에이전트의 선택을 움직이는지 외부 러너로 잰다 (옵트인, 세션 밖)",
    )
    p_probe.add_argument(
        "--runner",
        default=" ".join(DEFAULT_RUNNER),
        help="프롬프트를 마지막 인자로 받아 stdout에 답하는 명령 (기본: claude -p --output-format text)",
    )
    p_probe.add_argument("--axes", nargs="*", help="이 축만 탐침")
    p_probe.add_argument("--quick", action="store_true", help="축당 한 장면만")
    p_probe.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p_probe.add_argument("--dry-run", dest="dry_run", action="store_true", help="러너 호출 없이 준비된 탐침만 보고")
    p_probe.add_argument("--json", dest="json_output", action="store_true")
    _add_common(p_probe)
    p_probe.set_defaults(func=cmd_probe)

    p_why = sub.add_parser(
        "why", help="규칙이 어떤 X에서 나왔는지 증거를 소급해 보여준다"
    )
    _add_common(p_why)
    p_why.add_argument("axis", nargs="?", default=None, help="축 이름 (생략 시 전부)")
    p_why.set_defaults(func=cmd_why)

    p_pair = sub.add_parser(
        "pair", help="현재 세션의 다음 페어를 JSON으로 출력 (에이전트/스크립트용)"
    )
    _add_common(p_pair)
    p_pair.add_argument("--repo", type=Path, default=None, help="슬롯 치환용 레포 경로")
    p_pair.set_defaults(func=cmd_pair)

    p_strike = sub.add_parser(
        "strike", help="페어 하나에 긋기를 기록 (에이전트/스크립트용)"
    )
    _add_common(p_strike)
    p_strike.add_argument("target", choices=["left", "right", "both", "pair"])
    p_strike.add_argument("--pair-id", required=True, help="pair 명령이 보여준 pair_id")
    p_strike.add_argument("--slot", type=int, default=None, help="기대 슬롯 번호(선택)")
    p_strike.add_argument("--repo", type=Path, default=None, help="슬롯 치환용 레포 경로")
    p_strike.set_defaults(func=cmd_strike)

    p_resume = sub.add_parser("resume", help="마지막 또는 지정한 미완료 세션을 재개")
    _add_serve_common(p_resume)
    p_resume.add_argument("session_id", nargs="?", help="재개할 session_id")
    p_resume.set_defaults(func=cmd_resume)

    p_validate = sub.add_parser(
        "validate", help="검증 세션을 연다 (판별 13 + 미러 프로브 2)"
    )
    _add_serve_common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_recheck = sub.add_parser("recheck", help="4막 경량 재심 세션을 연다 (5-7긋기)")
    _add_serve_common(p_recheck)
    p_recheck.add_argument(
        "--budget", type=int, default=DEFAULT_BUDGET, help="재심 긋기 예산 (5-7)"
    )
    p_recheck.set_defaults(func=cmd_recheck)

    p_status = sub.add_parser("status", help="착지/재심/자기반증 판정 요약")
    _add_common(p_status)
    p_status.add_argument("--json", dest="json_output", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_sessions = sub.add_parser("sessions", help="최근 세션 목록 또는 상세 이벤트")
    _add_common(p_sessions)
    p_sessions.add_argument("session_id", nargs="?", help="상세 조회할 session_id")
    p_sessions.add_argument("--limit", type=int, default=10, help="목록 최대 건수")
    p_sessions.add_argument(
        "--events", type=int, default=10, help="상세 최근 이벤트 건수"
    )
    p_sessions.add_argument("--json", dest="json_output", action="store_true")
    p_sessions.set_defaults(func=cmd_sessions)

    p_doctor = sub.add_parser("doctor", help="설치와 로컬 데이터 무결성 진단")
    _add_common(p_doctor)
    p_doctor.add_argument("--json", dest="json_output", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_export = sub.add_parser(
        "export", help="수렴 규칙을 에이전트 중립 형식으로 내보냄"
    )
    _add_common(p_export)
    p_export.add_argument("--format", choices=EXPORT_FORMATS, default="markdown")
    p_export.add_argument("--output", type=Path, help="생략하면 stdout")
    p_export.set_defaults(func=cmd_export)

    p_data = sub.add_parser("data", help="소유 데이터 백업과 읽기 전용 검사")
    data_sub = p_data.add_subparsers(dest="data_command", required=True)
    p_backup = data_sub.add_parser("backup", help="원자 ZIP snapshot 생성")
    _add_common(p_backup)
    p_backup.add_argument("output", type=Path, help="소유 디렉토리 밖 .zip 경로")
    p_backup.set_defaults(func=cmd_data_backup)
    p_inspect = data_sub.add_parser("inspect", help="백업 무결성을 추출 없이 검사")
    p_inspect.add_argument("path", type=Path)
    p_inspect.add_argument("--json", dest="json_output", action="store_true")
    p_inspect.set_defaults(func=cmd_data_inspect)

    p_version = sub.add_parser("version", help="앱/카탈로그/백업 schema 버전")
    p_version.set_defaults(func=cmd_version)

    p_update = sub.add_parser("update", help="설치 방식별 명시적 업그레이드 명령 안내")
    p_update.set_defaults(func=cmd_update)

    p_land = sub.add_parser("land", help="저장된 스트림에서 산출물 재착지")
    _add_common(p_land)
    p_land.add_argument(
        "--acknowledge-mismatch",
        action="store_true",
        help="수기 편집 감지를 manifest에 기록하고 착지를 강행한다",
    )
    p_land.set_defaults(func=cmd_land)

    p_enable = sub.add_parser("enable", help="CLAUDE.md @import 한 줄 추가 (허가 필수)")
    _add_common(p_enable)
    p_enable.add_argument(
        "--grant", action="store_true", help="import_permission_granted 허가를 기록한다"
    )
    p_enable.set_defaults(func=cmd_enable)

    p_undo = sub.add_parser("undo", help="@import 한 줄 제거 (전체 롤백 지점)")
    _add_common(p_undo)
    p_undo.set_defaults(func=cmd_rollback)

    p_rollback = sub.add_parser("rollback", help="undo의 이전 이름 (동일 동작)")
    _add_common(p_rollback)
    p_rollback.set_defaults(func=cmd_rollback)

    p_optin = sub.add_parser("optin", help="수기 룰 반증 대상 opt-in")
    _add_common(p_optin)
    p_optin.add_argument("rule_id", help="manual_rules.json의 rule_id")
    p_optin.set_defaults(func=cmd_optin)

    p_ack = sub.add_parser(
        "acknowledge", help="핵심 반증 인간 확정 (refutation_acknowledged)"
    )
    _add_common(p_ack)
    p_ack.add_argument("--actor", required=True, help="확정 주체 이름")
    p_ack.set_defaults(func=cmd_acknowledge)

    return parser


def normalize_argv(argv: Sequence[str] | None) -> list[str]:
    """인자 없는 `xout`은 `xout open`과 같다 - 시작이 곧 기본 동작."""
    resolved = list(sys.argv[1:] if argv is None else argv)
    return resolved if resolved else ["open"]


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(normalize_argv(argv))
    if getattr(args, "base_dir", None) is None:
        try:
            migrate_legacy_home()
        except OSError as exc:
            logger.warning("레거시 데이터 이관 실패 - 기존 경로로 계속한다: %s", exc)
    try:
        return args.func(args)
    except StoreViolation as exc:
        logger.error("이벤트 스토어 손상 - %s", exc)
        logger.error(
            "append-only 스트림은 자동 복구하지 않는다 - sessions/ 안 해당 파일의 손상 줄을 직접 확인해라"
        )
        return 1
