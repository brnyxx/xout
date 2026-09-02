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
from dataclasses import replace
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
from xout.judge import (
    candidates as judge_candidates,
    judge,
    merge as judge_merge,
    write_receipt as write_judge_receipt,
)
from xout.mine import Conflict, Observation, find_conflicts, mine, summarize, user_rule_files
from xout.reconcile import apply_removals, plan as reconcile_plan, render_patch, write_patch
from xout.targets import MODE_IMPORT, REGISTRY, block_state, ensure_block, remove_block, targets_by_id
from xout.savepoint import SavepointError, create as create_savepoint, list_savepoints, restore as restore_savepoint
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
            "remediation": "enable" if output_exists else "open",
        }
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return {
            "status": "import-drift",
            "path": str(target),
            "expected_import": expected,
            "remediation": f"unreadable: {exc}",
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
            ("undo_then_enable" if output_exists else "undo_then_open")
            if stale
            else ("enable" if output_exists else "open")
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


def _grant_and_enable(base: Path, lang: str = DEFAULT_LANG) -> int:
    """import 허가 레코드를 남기고 소유 @import 한 줄을 추가한다."""
    writer = OwnedWriter(base_dir=base)
    record = ConsentRecord(
        kind=ConsentKind.IMPORT_PERMISSION_GRANTED,
        subject=str(writer.claude_md_path),
    )
    _persist_consent(base, record)
    outcome = writer.ensure_import(record)
    msg = _ENABLE_MSG.get(lang, _ENABLE_MSG["ko"])
    print(msg["result"].format(id="claude", reason=outcome.reason, path=outcome.path))
    return 0 if outcome.reason in ("added", "already_present") else 1


_TUI_TARGETS = {"1": "left", "2": "right", "b": "both", "p": "pair"}

#: 세션 플로우 화면 문자열 - 언어는 렌더 계층에만 존재한다.
_TUI_MSG: dict[str, dict[str, str]] = {
    "ko": {
        "intro": "xout - 아닌 쪽에 X를 치세요.",
        "keys": "입력: 1/2=한쪽에 X, b=둘 다 X, p=이 페어로는 못 고르겠다, u=되돌리기, q=중단",
        "aborted": "중단 - 여기까지는 저장됐다. 다시 실행하면 이어진다.",
        "allowed": "허용 입력: 1, 2, b, p, u, q",
        "rejected": "기록 안 됨: %s",
        "voided": "세션 무효: %s",
        "complete": "세션 완료 - 만들어진 규칙:",
        "landed": "저장 완료: %s",
        "apply": "지금 CLAUDE.md에 적용할까요? [y/N] ",
        "mined": "      ↳ 이미 있는 규칙 {path}:{line_no} \"{text}\" → {value}",
        "later": "나중에 적용하려면: xout enable --grant / 취소는 xout undo",
        "targets": "다른 도구에도 꽂기: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|kiro|agents|all",
    },
    "en": {
        "intro": "xout - cross out the one you never want.",
        "keys": "keys: 1/2=X out that side, b=X out both, p=can't tell these apart, u=undo, q=quit",
        "aborted": "Stopped - progress is saved. Run again to continue.",
        "allowed": "allowed inputs: 1, 2, b, p, u, q",
        "rejected": "rejected: %s",
        "voided": "session voided: %s",
        "complete": "Session complete - compiled rules:",
        "landed": "landed: %s",
        "apply": "Apply to CLAUDE.md now? [y/N] ",
        "mined": "      ↳ already in your files {path}:{line_no} \"{text}\" → {value}",
        "later": "Apply later with xout enable --grant; undo with xout undo",
        "targets": "Other tools: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|kiro|agents|all",
    },
    "ja": {
        "intro": "xout - 二度と見たくない方に X を。",
        "keys": "操作: 1/2=どちらかに X, b=両方に X, p=このペアでは判別できない, u=取り消し, q=中断",
        "aborted": "中断した - 進捗は保存済み。次に実行すると続きから始まる。",
        "allowed": "使える入力: 1, 2, b, p, u, q",
        "rejected": "受け付けなかった: %s",
        "voided": "セッション無効: %s",
        "complete": "セッション完了 - コンパイルしたルール:",
        "landed": "着地完了: %s",
        "apply": "今すぐ CLAUDE.md に適用しますか？ [y/N] ",
        "mined": "      ↳ 手元のファイルにすでにある {path}:{line_no} \"{text}\" → {value}",
        "later": "あとで適用するには: xout enable --grant / 取り消すには: xout undo",
        "targets": "ほかのツールへ差し込むには: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|kiro|agents|all",
    },
    "zh": {
        "intro": "xout - 给你再也不想看到的那个打 X。",
        "keys": "按键: 1/2=划掉一侧, b=两侧都划掉, p=这一对分不出来, u=撤销, q=退出",
        "aborted": "已中断 - 进度已保存，再运行一次就能接着划。",
        "allowed": "可用输入: 1, 2, b, p, u, q",
        "rejected": "已拒绝: %s",
        "voided": "会话作废: %s",
        "complete": "会话结束 - 编译出来的规则:",
        "landed": "已落地: %s",
        "apply": "现在就写进 CLAUDE.md 吗？ [y/N] ",
        "mined": "      ↳ 已有规则 {path}:{line_no} \"{text}\" → {value}",
        "later": "稍后应用: xout enable --grant / 撤销: xout undo",
        "targets": "接入其他工具: xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|kiro|agents|all",
    },
}


def _mined_by_axis(roots: Sequence[Path] | None = None, include_user: bool = True) -> dict[str, list]:
    """현재 디렉토리 + 사용자 레벨 규칙 파일의 관측을 축별로 묶는다 - 페어 옆에 보여 줄 맥락."""
    by_axis: dict[str, list] = {}
    for obs in mine(list(roots or [Path.cwd()]), include_user=include_user):
        by_axis.setdefault(obs.axis, []).append(obs)
    return by_axis


def _run_tui(session: ColdOpenSession, base: Path, lang: str = DEFAULT_LANG) -> int:
    """터미널 세션 루프 - 웹과 같은 이벤트 원장 위에서 긋는다."""
    msg = _TUI_MSG.get(lang, _TUI_MSG["ko"])
    print(msg["intro"])
    print(msg["keys"])
    mined_by_axis = _mined_by_axis()
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        pair = snap.pair
        print()
        print(f"[{snap.slots_used + 1}/{snap.slots_total}] {pair.axis_label}")
        print("  (1) " + pair.left_text.replace("\n", "\n      "))
        print("  (2) " + pair.right_text.replace("\n", "\n      "))
        for obs in mined_by_axis.get(pair.axis, ())[:2]:
            print(msg["mined"].format(path=obs.path, line_no=obs.line_no, text=obs.line, value=obs.value))
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
    conflicts = find_conflicts(
        mine([Path.cwd()], include_user=True), _rules_by_axis(snap.rules)
    )
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
        code = _grant_and_enable(base, lang)
        print(msg["targets"])
        return code
    logger.info(msg["later"])
    print(msg["targets"])
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
        "origin_prior": "추정 기본값",
        "no_evidence": "근거: 이 축에 친 X가 아직 없다.",
        "evidence": "근거:",
        "line": "  - {context} 장면({scene})에서 {values}에 X (세션 {session})",
    },
    "en": {
        "rule": "rule: {text}",
        "state": "state: {grade} / source: {origin}",
        "origin_elicited": "your X",
        "origin_prior": "guessed default (you haven't been asked yet)",
        "no_evidence": "evidence: none of your X's has touched this axis yet.",
        "evidence": "evidence:",
        "line": "  - X'd {values} in the {context} scene ({scene}) (session {session})",
    },
    "ja": {
        "rule": "ルール: {text}",
        "state": "状態: {grade} / 出所: {origin}",
        "origin_elicited": "あなたの X",
        "origin_prior": "推定したデフォルト（まだ聞いていない）",
        "no_evidence": "根拠: この軸に向けた X はまだない。",
        "evidence": "根拠:",
        "line": "  - {context}の場面({scene})で {values} に X (セッション {session})",
    },
    "zh": {
        "rule": "规则: {text}",
        "state": "状态: {grade} / 来源: {origin}",
        "origin_elicited": "你的 X",
        "origin_prior": "推定的默认值（还没问过你）",
        "no_evidence": "依据: 这个轴上还没打过 X。",
        "evidence": "依据:",
        "line": "  - 在{context}场景({scene})给 {values} 打了 X (会话 {session})",
    },
}


_JUDGE_MSG = {
    "ko": {
        "judged": "에이전트 판정 ({runner}): 파일 {files}개 · 줄 {lines}개 → 두 계층 일치 {agreed} · 에이전트만 잡음 {added} · 정규식만 잡음 (탈락) {dropped} · 값이 다름 {disagreed}",
        "receipt": "판정 영수증: {path}",
        "runner_missing": "러너를 시작할 수 없다: {error}",
    },
    "en": {
        "judged": "agent verdicts ({runner}): {files} file(s) · {lines} line(s) → both layers agree {agreed} · agent only {added} · pattern only (dropped) {dropped} · different value {disagreed}",
        "receipt": "verdict receipt: {path}",
        "runner_missing": "Cannot start the runner: {error}",
    },
    "ja": {
        "judged": "エージェント判定 ({runner}): ファイル {files} 件 · 行 {lines} 件 → 両層一致 {agreed} · エージェントのみ {added} · パターンのみ (除外) {dropped} · 値が異なる {disagreed}",
        "receipt": "判定レシート: {path}",
        "runner_missing": "ランナーを起動できない: {error}",
    },
    "zh": {
        "judged": "智能体判定 ({runner}): 文件 {files} 个 · 行 {lines} 条 → 两层一致 {agreed} · 仅智能体 {added} · 仅模式 (剔除) {dropped} · 值不同 {disagreed}",
        "receipt": "判定回执: {path}",
        "runner_missing": "运行器启动不了: {error}",
    },
}


def _judged_observations(
    args: argparse.Namespace, lang: str, roots: list[Path], pattern: list[Observation]
) -> tuple[list[Observation], dict[str, Any]]:
    """옵트인 두 번째 계층: 사용자의 에이전트가 같은 줄들을 판정하고, 정규식과 대조한다."""
    command = shlex.split(args.runner, posix=os.name != "nt")
    runner = subprocess_runner(command, timeout=args.timeout)
    items = judge_candidates(roots, include_user=args.include_user)
    report = judge(items, runner, lang, command)
    merged, agreement, source = judge_merge(pattern, report.observations)
    report = replace(report, agreement=agreement)
    receipt = write_judge_receipt(Path(args.base_dir), report)
    meta = {
        "runner": args.runner,
        "files": len({item.abs_path for item in items}),
        "lines": len(items),
        "agreement": agreement,
        "source": source,
        "receipt_path": str(receipt),
    }
    return merged, meta


def _print_judged(meta: Mapping[str, Any], lang: str) -> None:
    msg = _JUDGE_MSG.get(lang, _JUDGE_MSG["ko"])
    print(msg["judged"].format(runner=meta["runner"], files=meta["files"], lines=meta["lines"], **meta["agreement"]))
    print(msg["receipt"].format(path=meta["receipt_path"]))


def _obs_dict(obs: Observation, source: Mapping[tuple[str, int, str], str] | None) -> dict[str, Any]:
    payload = obs.to_dict()
    if source is not None:
        payload["source"] = source.get((obs.abs_path or obs.path, obs.line_no, obs.axis), "pattern")
    return payload


_CONFLICT_MSG = {
    "ko": {
        "header": "프로젝트 규칙과 갈리는 줄 {count}건 (정면 충돌이면 프로젝트가 이긴다):",
        "none": "프로젝트 규칙 파일에 당신의 규칙과 갈리는 줄이 없다.",
        "no_rules": "아직 규칙이 없다 - 먼저 xout을 돌려라.",
        "line": "  - [{axis}] 프로젝트는 {observed}, 당신의 규칙은 {rule}  {path}:{line_no}  \"{text}\"",
    },
    "en": {
        "header": "{count} project rule line(s) disagree with your rules (the project wins on a direct conflict):",
        "none": "Nothing in your project rule files contradicts your rules.",
        "no_rules": "No compiled rules yet - run xout first.",
        "line": "  - [{axis}] project says {observed}, your rule says {rule}  {path}:{line_no}  \"{text}\"",
    },
    "ja": {
        "header": "プロジェクトの規則と食い違う行 {count}件 (正面衝突ならプロジェクトが優先):",
        "none": "プロジェクトの規則ファイルに、あなたの規則と食い違う行はない。",
        "no_rules": "コンパイル済みの規則がない - 先に xout を実行すること。",
        "line": "  - [{axis}] プロジェクトは {observed}、あなたの規則は {rule}  {path}:{line_no}  \"{text}\"",
    },
    "zh": {
        "header": "有 {count} 行项目规则和你的规则不一致 (正面冲突时以项目为准):",
        "none": "项目规则文件里没有和你的规则不一致的行。",
        "no_rules": "还没有编译出规则 - 先跑一次 xout。",
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
    observations = mine(roots, include_user=args.include_user)
    meta: dict[str, Any] | None = None
    if getattr(args, "runner", None):
        try:
            observations, meta = _judged_observations(args, lang, roots, observations)
        except (ProbeError, subprocess.TimeoutExpired) as exc:
            text = _JUDGE_MSG.get(lang, _JUDGE_MSG["ko"])["runner_missing"].format(error=exc)
            print(json.dumps({"error": str(exc)}) if args.json_output else text)
            return 2
    conflicts = find_conflicts(observations, _manifest_rules_by_axis(manifest))
    if args.json_output:
        payload: dict[str, Any] = {"conflicts": [conflict.to_dict() for conflict in conflicts]}
        if meta is not None:
            payload["agent"] = {k: v for k, v in meta.items() if k != "source"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if meta is not None:
        _print_judged(meta, lang)
    _print_conflicts(conflicts, lang)
    return 0


_PROBE_MSG = {
    "ko": {
        "no_rules": "아직 규칙이 없다 - 먼저 xout을 돌려라.",
        "runner_missing": "러너를 시작할 수 없다: {error}",
        "start": "탐침 {n}건 x 2회 (규칙 없이 / XOUT.md 붙여서) - 러너: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (규칙: {survivor})  {verdict}",
        "held": "지킴",
        "moved": "바뀜",
        "missed": "어긋남",
        "unparsed": "판독 불가",
        "summary": "규칙 지킴 {held}/{cases} · 규칙이 답을 바꿈 {moved} · 규칙 없이도 같은 답 {bare} · 판독 불가 {unparsed}",
        "receipt": "영수증: {path}",
        "dry": "탐침 {n}건 준비만 함 (러너 호출 없음)",
        "repeat": " x {repeat}회 반복",
        "via": "규칙 전달: {path} 의 xout 블록/한 줄로만 (프롬프트에는 넣지 않음) - 세이브포인트 {savepoint}",
        "context": "방해 문서: {path}",
        "not_active": "[{id}]가 활성 상태가 아니다 - 먼저: xout enable --grant --target {id}",
        "unknown_target": "모르는 타깃: {id} (xout targets 로 목록 확인)",
        "restore_failed": "주의: [{id}] 원상복구 실패 - xout enable --grant --target {id} 로 되돌려라: {error}",
        "trials": " · 시행 기준 {trials_held}/{trials} · 매 시행 지킴 {every}/{cases}",
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
        "dry": "{n} cases prepared; runner not called",
        "repeat": " x {repeat} trials",
        "via": "rules delivered only through {path} (not in the prompt) - savepoint {savepoint}",
        "context": "distractor document: {path}",
        "not_active": "[{id}] is not active - first: xout enable --grant --target {id}",
        "unknown_target": "unknown target: {id} (see xout targets)",
        "restore_failed": "warning: could not restore [{id}] - run xout enable --grant --target {id}: {error}",
        "trials": " · by trial {trials_held}/{trials} · held every trial {every}/{cases}",
    },
    "ja": {
        "no_rules": "着地した規則がない - 先に xout を実行すること。",
        "runner_missing": "ランナーを起動できない: {error}",
        "start": "プローブ {n}件 x 2回 (規則なし / XOUT.md あり) - ランナー: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (規則: {survivor})  {verdict}",
        "held": "維持",
        "moved": "変化",
        "missed": "不一致",
        "unparsed": "判読不能",
        "summary": "規則維持 {held}/{cases} · 規則が選択を動かした {moved} · 規則なしでも一致 {bare} · 判読不能 {unparsed}",
        "receipt": "レシート: {path}",
        "dry": "プローブ {n}件を準備した (ランナーは呼んでいない)",
        "repeat": " x {repeat} 回",
        "via": "規則の受け渡しは {path} の xout ブロック/1 行のみ (プロンプトには入れない) - セーブポイント {savepoint}",
        "context": "妨害文書: {path}",
        "not_active": "[{id}] は有効になっていない - 先に: xout enable --grant --target {id}",
        "unknown_target": "不明なターゲット: {id} (xout targets で一覧)",
        "restore_failed": "注意: [{id}] を元に戻せなかった - xout enable --grant --target {id} で戻すこと: {error}",
        "trials": " · 試行ベース {trials_held}/{trials} · 毎回維持 {every}/{cases}",
    },
    "zh": {
        "no_rules": "还没有落地的规则 - 先跑一次 xout。",
        "runner_missing": "运行器启动不了: {error}",
        "start": "探测 {n} 例 x 2 次 (不带规则 / 带 XOUT.md) - 运行器: {runner}",
        "line": "  [{axis}] {scene}: {bare} -> {ruled}  (规则: {survivor})  {verdict}",
        "held": "保持",
        "moved": "改变",
        "missed": "不符",
        "unparsed": "无法判读",
        "summary": "规则保持 {held}/{cases} · 规则改变了选择 {moved} · 不带规则也一致 {bare} · 无法判读 {unparsed}",
        "receipt": "回执: {path}",
        "dry": "已准备好 {n} 例 (没有调用运行器)",
        "repeat": " x {repeat} 次",
        "via": "规则只通过 {path} 里的 xout 区块/一行传递 (不放进提示) - 存档点 {savepoint}",
        "context": "干扰文档: {path}",
        "not_active": "[{id}] 没有启用 - 先执行: xout enable --grant --target {id}",
        "unknown_target": "未知目标: {id} (用 xout targets 查看)",
        "restore_failed": "注意: [{id}] 没能恢复原状 - 用 xout enable --grant --target {id} 恢复: {error}",
        "trials": " · 按次数 {trials_held}/{trials} · 每次都保持 {every}/{cases}",
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
    context_text: str | None = None
    if args.context_file:
        context_text = Path(args.context_file).read_text(encoding="utf-8")
    rules_text = xout_path.read_text(encoding="utf-8")
    deliver: Callable[[bool], None] | None = None
    delivery = "prompt"
    via_line = ""
    if args.via_target:
        target = REGISTRY.get(args.via_target)
        if target is None:
            text = msg["unknown_target"].format(id=args.via_target)
            print(json.dumps({"error": "unknown_target"}) if args.json_output else text)
            return 1
        path = target.resolve(Path.home(), Path.cwd())
        if target.mode == MODE_IMPORT:
            active = _activation_state(base)["status"] == "active"
            path = OwnedWriter(base_dir=base).claude_md_path
        else:
            active = block_state(base, target.target_id, path)["active"]
        if not active:
            text = msg["not_active"].format(id=target.target_id)
            print(json.dumps({"error": "target_not_active"}) if args.json_output else text)
            return 1
        record = ConsentRecord(kind=ConsentKind.IMPORT_PERMISSION_GRANTED, subject=str(path))
        _persist_consent(base, record)
        savepoint = create_savepoint(base, [path], f"probe via {target.target_id}")

        def deliver(on: bool) -> None:
            if target.mode == MODE_IMPORT:
                writer = OwnedWriter(base_dir=base)
                outcome = writer.ensure_import(record) if on else writer.remove_import()
                ok = outcome.reason in (("added", "already_present") if on else ("removed", "not_present"))
            else:
                outcome = (
                    ensure_block(base, target.target_id, path, rules_text, record, preamble=target.preamble)
                    if on
                    else remove_block(base, target.target_id, path)
                )
                ok = outcome.reason in (("added", "updated", "already_present") if on else ("removed", "not_present"))
            if not ok:
                raise ProbeError(f"[{target.target_id}] {'restore' if on else 'remove'} failed: {outcome.reason}")

        delivery = f"target:{target.target_id}"
        via_line = msg["via"].format(path=path, savepoint=savepoint.savepoint_id)
    if not args.json_output:
        print(
            msg["start"].format(n=len(cases), runner=args.runner)
            + (msg["repeat"].format(repeat=args.repeat) if args.repeat > 1 else "")
        )
        if via_line:
            print(via_line)
        if args.context_file:
            print(msg["context"].format(path=args.context_file))

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
            cases,
            rules_text,
            runner,
            lang,
            command,
            on_outcome,
            repeat=args.repeat,
            context_text=context_text,
            rules_in_prompt=deliver is None,
            phase_hook=(lambda phase: deliver(phase == "ruled")) if deliver else None,
            delivery=delivery,
        )
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}) if args.json_output else msg["runner_missing"].format(error=exc))
        return 2
    finally:
        if deliver is not None:
            try:
                deliver(True)
            except ProbeError as exc:
                print(msg["restore_failed"].format(id=args.via_target, error=exc), file=sys.stderr)
    receipt = write_receipt(base, report)
    if args.json_output:
        payload = report.to_dict()
        payload["receipt_path"] = str(receipt)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print()
    summary = report.summary
    line = msg["summary"].format(bare=summary["bare_matched"], **summary)
    if report.repeat > 1:
        line += msg["trials"].format(
            trials_held=summary["trials_held"], trials=summary["trials"],
            every=summary["held_every_trial"], cases=summary["cases"],
        )
    print(line)
    print(msg["receipt"].format(path=receipt))
    return 0


_RECONCILE_MSG = {
    "ko": {
        "no_rules": "아직 규칙이 없다 - 먼저 xout을 돌려라.",
        "dup_header": "XOUT.md와 겹치는 줄 {count}건 (중복):",
        "conf_header": "XOUT.md와 반대되는 줄 {count}건 (모순 - 손대지 않는다, 당신이 고른다):",
        "line": "  - [{axis}] {value}  {path}:{line_no}  \"{text}\"",
        "clean": "중복도 모순도 없다 - 기존 규칙 파일과 XOUT.md가 깔끔하게 나뉘어 있다.",
        "patch": "제안 패치: {path}  (적용: xout reconcile --apply --grant)",
        "need_grant": "--apply는 --grant가 있어야 돈다 - xout 폴더 밖 파일을 고치는 일이라서다. 아무것도 바꾸지 않았다.",
        "applied": "중복 줄을 지웠다: {files}",
        "savepoint": "세이브포인트 {id} - 되돌리기: xout savepoint restore {id}",
        "nothing": "지울 중복 줄이 없다.",
        "near_header": "XOUT.md 문장과 거의 같은 줄 {count}건 (점수만 붙여 보고한다 - 지우지 않는다):",
        "near_line": "  - [{axis}] {value}  {path}:{line_no}  겹침 {score}  \"{text}\"",
    },
    "en": {
        "no_rules": "No landed rules yet - run xout first.",
        "dup_header": "{count} line(s) XOUT.md already covers (duplicates):",
        "conf_header": "{count} line(s) that contradict XOUT.md (conflicts - left alone; your call):",
        "line": "  - [{axis}] {value}  {path}:{line_no}  \"{text}\"",
        "clean": "No duplicates, no conflicts - your existing rule files and XOUT.md don't overlap.",
        "patch": "proposed patch: {path}  (apply: xout reconcile --apply --grant)",
        "need_grant": "--apply needs --grant because it edits files outside xout's own directory. Nothing was changed.",
        "applied": "removed duplicate lines in: {files}",
        "savepoint": "savepoint {id} - roll back with: xout savepoint restore {id}",
        "nothing": "No duplicate lines to remove.",
        "near_header": "{count} line(s) that read almost like an XOUT.md sentence (reported with a score, left alone):",
        "near_line": "  - [{axis}] {value}  {path}:{line_no}  overlap {score}  \"{text}\"",
    },
    "ja": {
        "no_rules": "着地した規則がない - 先に xout を実行すること。",
        "dup_header": "XOUT.md ですでにカバーされている行 {count}件 (重複):",
        "conf_header": "XOUT.md と逆のことを言っている行 {count}件 (矛盾 - xout は触らない。判断はあなたに任せる):",
        "line": "  - [{axis}] {value}  {path}:{line_no}  \"{text}\"",
        "clean": "重複も矛盾もない - 既存の規則ファイルと XOUT.md はきれいに分かれている。",
        "patch": "提案パッチ: {path}  (適用するには: xout reconcile --apply --grant)",
        "need_grant": "--apply には --grant が必要 - xout の管理ディレクトリの外を編集するため。まだ何も変更していない。",
        "applied": "重複行を削除した: {files}",
        "savepoint": "セーブポイント {id} - 戻すには: xout savepoint restore {id}",
        "nothing": "削除する重複行はない。",
        "near_header": "XOUT.md の文とほぼ同じ行 {count}件 (スコアを付けて報告するだけ - 削除はしない):",
        "near_line": "  - [{axis}] {value}  {path}:{line_no}  重なり {score}  \"{text}\"",
    },
    "zh": {
        "no_rules": "还没有落地的规则 - 先跑一次 xout。",
        "dup_header": "有 {count} 行 XOUT.md 已经覆盖了 (重复):",
        "conf_header": "有 {count} 行和 XOUT.md 相反 (冲突 - 不会改动，由你决定):",
        "line": "  - [{axis}] {value}  {path}:{line_no}  \"{text}\"",
        "clean": "没有重复，也没有冲突 - 现有规则文件和 XOUT.md 各管各的。",
        "patch": "建议补丁: {path}  (应用: xout reconcile --apply --grant)",
        "need_grant": "--apply 要配合 --grant，因为要改 xout 自有目录之外的文件。这次什么都没改。",
        "applied": "已删除重复行: {files}",
        "savepoint": "存档点 {id} - 回滚: xout savepoint restore {id}",
        "nothing": "没有需要删的重复行。",
        "near_header": "有 {count} 行和 XOUT.md 的句子几乎一样 (只带分数报告，不删除):",
        "near_line": "  - [{axis}] {value}  {path}:{line_no}  重合 {score}  \"{text}\"",
    },
}

_SAVEPOINT_MSG = {
    "ko": {"created": "세이브포인트 {id} ({count}개 파일)", "none": "세이브포인트가 없다.", "restored": "복원: {path} ({action})", "unknown": "그런 세이브포인트가 없다: {id}", "hint": "되돌리기: xout savepoint restore {id}"},
    "en": {"created": "savepoint {id} ({count} file(s))", "none": "No savepoints.", "restored": "restore: {path} ({action})", "unknown": "unknown savepoint: {id}", "hint": "roll back with: xout savepoint restore {id}"},
    "ja": {"created": "セーブポイント {id} ({count} 件のファイル)", "none": "セーブポイントはない。", "restored": "復元: {path} ({action})", "unknown": "該当するセーブポイントがない: {id}", "hint": "戻すには: xout savepoint restore {id}"},
    "zh": {"created": "存档点 {id} ({count} 个文件)", "none": "没有存档点。", "restored": "恢复: {path} ({action})", "unknown": "没有这个存档点: {id}", "hint": "回滚: xout savepoint restore {id}"},
}


def cmd_reconcile(args: argparse.Namespace) -> int:
    """기존 규칙 파일과 XOUT.md의 중복·모순을 보고하고, 허가가 있으면 중복 줄만 지운다."""
    lang = _args_lang(args)
    msg = _RECONCILE_MSG.get(lang, _RECONCILE_MSG["ko"])
    base = Path(args.base_dir)
    manifest = _load_manifest(base)
    if manifest is None:
        print(json.dumps({"error": "no_rules"}) if args.json_output else msg["no_rules"])
        return 1
    roots = [Path(root) for root in (args.roots or ["."])]
    observations = mine(roots, include_user=args.include_user)
    sentences = {
        (entry["axis"], entry["value"]): entry.get("rule", "")
        for entry in manifest.get("rules", [])
        if isinstance(entry.get("axis"), str) and isinstance(entry.get("value"), str)
    }
    plan = reconcile_plan(observations, _manifest_rules_by_axis(manifest), sentences)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    patch_path = None
    patch_text = render_patch(plan.duplicates) if plan.duplicates else ""
    if patch_text:
        patch_path = write_patch(base, patch_text, stamp)
    payload: dict[str, Any] = plan.to_dict()
    payload["patch_path"] = str(patch_path) if patch_path else None
    if args.apply:
        if not args.grant:
            if args.json_output:
                payload["error"] = "grant_required"
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(msg["need_grant"])
            return 1
        if plan.duplicates:
            _persist_consent(
                base,
                ConsentRecord(
                    kind=ConsentKind.RECONCILE_APPLY_GRANTED,
                    subject=", ".join(plan.files_to_edit),
                ),
            )
            savepoint, changed = apply_removals(base, plan.duplicates)
            payload["savepoint_id"] = savepoint.savepoint_id
            payload["changed_files"] = changed
        else:
            payload["savepoint_id"] = None
            payload["changed_files"] = []
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not plan.duplicates and not plan.conflicts and not plan.near_duplicates:
        print(msg["clean"])
        return 0
    if plan.duplicates:
        print(msg["dup_header"].format(count=len(plan.duplicates)))
        for d in plan.duplicates:
            print(msg["line"].format(axis=axis_label(d.axis, lang), value=d.value, path=d.path, line_no=d.line_no, text=d.line))
    if plan.conflicts:
        print(msg["conf_header"].format(count=len(plan.conflicts)))
        for c in plan.conflicts:
            print(msg["line"].format(axis=axis_label(c.axis, lang), value=c.observed_value, path=c.path, line_no=c.line_no, text=c.line))
    if plan.near_duplicates:
        print(msg["near_header"].format(count=len(plan.near_duplicates)))
        for n in plan.near_duplicates:
            print(msg["near_line"].format(axis=axis_label(n.axis, lang), value=n.value, path=n.path, line_no=n.line_no, score=f"{n.score:.2f}", text=n.line))
    if args.apply:
        if payload.get("changed_files"):
            print(msg["applied"].format(files=", ".join(payload["changed_files"])))
            print(msg["savepoint"].format(id=payload["savepoint_id"]))
        else:
            print(msg["nothing"])
    elif patch_path:
        print(msg["patch"].format(path=patch_path))
    return 0


def _default_savepoint_paths() -> list[Path]:
    paths = list(user_rule_files())
    from xout.mine import _iter_rule_files

    paths.extend(_iter_rule_files(Path.cwd()))
    return paths


def cmd_savepoint(args: argparse.Namespace) -> int:
    """소유 디렉토리 밖 규칙 파일의 스냅샷을 만들고, 나열하고, 되돌린다."""
    lang = _args_lang(args)
    msg = _SAVEPOINT_MSG.get(lang, _SAVEPOINT_MSG["ko"])
    base = Path(args.base_dir)
    action = args.action
    if action == "list":
        points = list_savepoints(base)
        if args.json_output:
            print(json.dumps([p.to_dict() for p in points], ensure_ascii=False, indent=2))
            return 0
        if not points:
            print(msg["none"])
            return 0
        for p in points:
            print(f"{p.savepoint_id}  {p.created_at}  {len(p.files)} file(s)  {p.reason}")
        return 0
    if action == "restore":
        try:
            results = restore_savepoint(base, args.savepoint_id)
        except SavepointError as exc:
            print(json.dumps({"error": str(exc)}) if args.json_output else msg["unknown"].format(id=args.savepoint_id))
            return 1
        if args.json_output:
            print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
            return 0
        for r in results:
            print(msg["restored"].format(path=r.path, action=r.action))
        return 0
    paths = [Path(p) for p in args.paths] if args.paths else _default_savepoint_paths()
    savepoint = create_savepoint(base, paths, args.reason or "manual")
    if args.json_output:
        print(json.dumps(savepoint.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(msg["created"].format(id=savepoint.savepoint_id, count=len(savepoint.files)))
    print(msg["hint"].format(id=savepoint.savepoint_id))
    return 0


_MINE_MSG = {
    "ko": {
        "none": "관측 없음 - 훑어본 규칙 파일에 이 축을 다루는 줄이 없다.",
        "header": "기존 규칙 파일에서 찾은 줄 (읽기 전용, 어림 매칭) - 관측 {count}건",
        "hint": "세션을 열면 이 줄들이 해당 페어 옆에 보인다: xout",
        "no_files": "규칙 파일을 찾지 못했다 (CLAUDE.md / AGENTS.md / .cursorrules 류)",
    },
    "en": {
        "none": "nothing found - no scanned line covers this axis.",
        "header": "what your rule files already say (read-only, best-effort) - {count} matching lines",
        "hint": "open a session and these lines show up next to their pairs: xout",
        "no_files": "no rule files found (CLAUDE.md / AGENTS.md / .cursorrules etc.)",
    },
    "ja": {
        "none": "観測なし - スキャンしたルールファイルに、この軸に関する行はなかった。",
        "header": "ローカルのルールファイル調査（読み取り専用・ヒューリスティック） - 観測 {count} 件",
        "hint": "セッションで X をつけるときに、この観測と突き合わせる: xout open",
        "no_files": "ルールファイルが見つからない (CLAUDE.md / AGENTS.md / .cursorrules など)",
    },
    "zh": {
        "none": "没有观测到 - 扫描过的规则文件里没有涉及这个轴的行。",
        "header": "本地规则挖掘报告（只读、启发式） - 观测到 {count} 条",
        "hint": "在会话里打 X 时可以对照这些观测: xout open",
        "no_files": "没有找到规则文件 (CLAUDE.md / AGENTS.md / .cursorrules 等)",
    },
}


def cmd_mine(args: argparse.Namespace) -> int:
    """로컬 규칙 파일에서 축 관측을 채굴한다 - 아무것도 쓰지 않는다."""
    lang = _args_lang(args)
    roots = [Path(root) for root in (args.roots or ["."])]
    observations = mine(roots, include_user=args.include_user)
    meta: dict[str, Any] | None = None
    if getattr(args, "runner", None):
        try:
            observations, meta = _judged_observations(args, lang, roots, observations)
        except (ProbeError, subprocess.TimeoutExpired) as exc:
            text = _JUDGE_MSG.get(lang, _JUDGE_MSG["ko"])["runner_missing"].format(error=exc)
            print(json.dumps({"error": str(exc)}) if args.json_output else text)
            return 2
    if args.json_output:
        source = meta["source"] if meta is not None else None
        payload: dict[str, Any] = {
            "observations": [_obs_dict(obs, source) for obs in observations],
            "summary": summarize(observations),
        }
        if meta is not None:
            payload["agent"] = {k: v for k, v in meta.items() if k != "source"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    msg = _MINE_MSG.get(lang, _MINE_MSG["ko"])
    if not observations:
        print(msg["no_files"] if not any(
            Path(root).exists() for root in (args.roots or ["."])
        ) else msg["header"].format(count=0))
    else:
        print(msg["header"].format(count=len(observations)))
    if meta is not None:
        _print_judged(meta, lang)
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


def _last_session_complete(base: Path) -> bool:
    """마지막 일반 세션이 완주됐고 재개할 것이 없으면 True - pair는 새 세션을 열지 않는다."""
    store = EventStore(base)
    summaries = summarize_sessions(store.load_all())
    product = [s for s in summaries if s.profile == PROFILE_PRODUCT]
    if not product or any(s.resumable for s in product):
        return False
    return True


@_runtime_exclusive
def cmd_pair(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    if not getattr(args, "new_session", False) and _last_session_complete(base):
        print(json.dumps({"pair": None, "session_complete": True, "hint": "xout pair --new"}, ensure_ascii=False, indent=2))
        return 0
    try:
        session = _headless_session(args)
    except (ValueError, SchemaViolation) as exc:
        logger.error("%s", exc)
        return 1
    payload = session.snapshot().to_dict()
    pair = payload.get("pair")
    if pair:
        mined = _mined_by_axis() if getattr(args, "include_user", True) else _mined_by_axis(include_user=False)
        pair["mined"] = [obs.to_dict() for obs in mined.get(pair["axis"], [])[:4]]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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


_STATUS_MSG = {
    "ko": {
        "none": "착지된 규칙이 없다 - xout 으로 첫 세션을 완주해라",
        "dir": "착지 디렉토리: {path}", "landed": "마지막 착지: {when}", "review": "마지막 재심: {when}",
        "remaining": "남은 조합: {count}", "queue": "재심 대기: {count}건", "banner": "배너: {text}",
        "activation": "활성화: {status}", "next": "다음 행동: {action}",
        "sessions": "저장된 세션: {sessions}개, 이벤트 {events}건", "resumable": "재개 가능: {count}건 (xout resume)",
        "judgment": "자기 점검: 유효 검증 세션 {valid}, 판별 인스턴스 {discriminative}, 정복원 {correct}, 오복원 {wrong}",
        "refuted": "핵심 점검 확정 - 긋기만으로는 부족하다는 조건이 성립했다 (직접 편집 전환)",
        "condition_met": "점검 조건 성립 - 확정은 xout acknowledge --actor <이름>",
    },
    "en": {
        "none": "No landed rules yet - run xout to finish a first session",
        "dir": "landing dir: {path}", "landed": "last landed: {when}", "review": "last recheck: {when}",
        "remaining": "remaining combinations: {count}", "queue": "recheck queue: {count}", "banner": "banner: {text}",
        "activation": "activation: {status}", "next": "next: {action}",
        "sessions": "stored sessions: {sessions}, events: {events}", "resumable": "resumable: {count} (xout resume)",
        "judgment": "self-check: valid validation sessions {valid}, discriminative instances {discriminative}, correct restorations {correct}, wrong restorations {wrong}",
        "refuted": "core check confirmed - strikes alone were found insufficient (pivot to direct editing)",
        "condition_met": "check condition met - confirm with xout acknowledge --actor <name>",
    },
    "ja": {
        "none": "着地したルールがない - xout で最初のセッションを完走する",
        "dir": "着地ディレクトリ: {path}", "landed": "最終着地: {when}", "review": "最終再審: {when}",
        "remaining": "残りの組み合わせ: {count}", "queue": "再審待ち: {count}件", "banner": "バナー: {text}",
        "activation": "有効化: {status}", "next": "次の操作: {action}",
        "sessions": "保存済みセッション: {sessions}、イベント {events}件", "resumable": "再開可能: {count}件 (xout resume)",
        "judgment": "自己点検: 有効な検証セッション {valid}、判別インスタンス {discriminative}、正しい復元 {correct}、誤った復元 {wrong}",
        "refuted": "コア点検が確定 - X だけでは足りないという条件が成立した (直接編集へ移行)",
        "condition_met": "点検条件が成立 - 確定は xout acknowledge --actor <名前>",
    },
    "zh": {
        "none": "还没有落地的规则 - 运行 xout 完成第一次会话",
        "dir": "落地目录: {path}", "landed": "上次落地: {when}", "review": "上次复审: {when}",
        "remaining": "剩余组合: {count}", "queue": "待复审: {count} 条", "banner": "横幅: {text}",
        "activation": "启用状态: {status}", "next": "下一步: {action}",
        "sessions": "已保存会话: {sessions} 个，事件 {events} 条", "resumable": "可恢复: {count} 个 (xout resume)",
        "judgment": "自检: 有效验证会话 {valid}，判别实例 {discriminative}，正确还原 {correct}，错误还原 {wrong}",
        "refuted": "核心检查成立 - 仅靠打 X 不够的条件已满足 (转为直接编辑)",
        "condition_met": "检查条件成立 - 用 xout acknowledge --actor <名字> 确认",
    },
}

_REMEDIATION_TEXT = {
    "ko": {"enable": "xout enable --grant", "open": "xout 으로 세션을 먼저 완주해라", "undo_then_enable": "xout undo 뒤 xout enable --grant", "undo_then_open": "xout undo 뒤 xout"},
    "en": {"enable": "xout enable --grant", "open": "finish a session first: xout", "undo_then_enable": "xout undo, then xout enable --grant", "undo_then_open": "xout undo, then xout"},
    "ja": {"enable": "xout enable --grant", "open": "先に xout でセッションを完走する", "undo_then_enable": "xout undo のあと xout enable --grant", "undo_then_open": "xout undo のあと xout"},
    "zh": {"enable": "xout enable --grant", "open": "先用 xout 完成一次会话", "undo_then_enable": "先 xout undo，再 xout enable --grant", "undo_then_open": "先 xout undo，再 xout"},
}


def _remediation_text(code: str, lang: str) -> str:
    table = _REMEDIATION_TEXT.get(lang, _REMEDIATION_TEXT["ko"])
    if code.startswith("unreadable:"):
        return code
    return table.get(code, code)


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
    lang = _args_lang(args)
    msg = _STATUS_MSG.get(lang, _STATUS_MSG["ko"])
    if manifest is None:
        print(msg["none"])
    else:
        print(msg["dir"].format(path=base))
        print(msg["landed"].format(when=manifest.get("generated_at")))
        print(msg["review"].format(when=manifest.get("last_review")))
        print(msg["remaining"].format(count=manifest.get("remaining_combinations")))
        queue = manifest.get("recheck_queue") or ()
        print(msg["queue"].format(count=len(queue)))
        if banner:
            print(msg["banner"].format(text=banner))
    print(msg["activation"].format(status=activation["status"]))
    if activation["remediation"]:
        print(msg["next"].format(action=_remediation_text(activation["remediation"], lang)))
    print(msg["sessions"].format(sessions=len(store.session_ids()), events=len(events)))
    in_progress = [summary for summary in summaries if summary.resumable]
    if in_progress:
        print(msg["resumable"].format(count=len(in_progress)))
    print(
        msg["judgment"].format(
            valid=state.valid_sessions,
            discriminative=state.discriminative_instances,
            correct=state.correct_restorations,
            wrong=state.mis_restorations,
        )
    )
    if state.core_refutation_confirmed:
        print(msg["refuted"])
    elif state.condition_met:
        print(msg["condition_met"])
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


def _selected_targets(args: argparse.Namespace, default: list[str]) -> list:
    ids = list(getattr(args, "targets", None) or default)
    try:
        return targets_by_id(REGISTRY, ids)
    except KeyError as exc:
        logger.error("알 수 없는 타깃: %s (xout targets 로 목록 확인)", exc)
        return []


def _enable_block_target(base: Path, target, lang: str = DEFAULT_LANG) -> int:
    path = target.resolve(Path.home(), Path.cwd())
    xout_md = base / XOUT_MD
    msg = _ENABLE_MSG.get(lang, _ENABLE_MSG["ko"])
    if not xout_md.is_file():
        print(msg["no_rules"])
        return 1
    record = ConsentRecord(kind=ConsentKind.IMPORT_PERMISSION_GRANTED, subject=str(path))
    _persist_consent(base, record)
    outcome = ensure_block(
        base, target.target_id, path, xout_md.read_text(encoding="utf-8"), record,
        preamble=target.preamble,
    )
    print(
        msg["result"].format(id=target.target_id, reason=outcome.reason, path=outcome.path)
        + (msg["rollback"].format(id=outcome.savepoint_id) if outcome.savepoint_id else "")
    )
    return 0 if outcome.reason in ("added", "updated", "already_present") else 1


_ENABLE_MSG = {
    "ko": {"no_rules": "착지된 XOUT.md가 없다 - 먼저 xout을 돌려라", "line": "추가될 한 줄 [{id}]: {what}", "block": "추가될 소유 블록 [{id}]: {what}", "grant": "사용자 파일은 허가 없이는 건드리지 않는다 - --grant 로 허가를 명시해라", "result": "결과 [{id}]: {reason} ({path})", "rollback": " - 되돌리기: xout savepoint restore {id}"},
    "en": {"no_rules": "No landed XOUT.md yet - run xout first", "line": "line to add [{id}]: {what}", "block": "owned block to add [{id}]: {what}", "grant": "Your files are never touched without permission - pass --grant", "result": "result [{id}]: {reason} ({path})", "rollback": " - roll back with: xout savepoint restore {id}"},
    "ja": {"no_rules": "着地した XOUT.md がない - 先に xout を実行する", "line": "追加される 1 行 [{id}]: {what}", "block": "追加される管理ブロック [{id}]: {what}", "grant": "許可なしにユーザーのファイルは触らない - --grant で明示する", "result": "結果 [{id}]: {reason} ({path})", "rollback": " - 戻すには: xout savepoint restore {id}"},
    "zh": {"no_rules": "还没有落地的 XOUT.md - 先运行 xout", "line": "将添加的一行 [{id}]: {what}", "block": "将添加的自有区块 [{id}]: {what}", "grant": "未经许可不会碰你的文件 - 用 --grant 明确授权", "result": "结果 [{id}]: {reason} ({path})", "rollback": " - 回滚: xout savepoint restore {id}"},
}


def cmd_enable(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    lang = _args_lang(args)
    msg = _ENABLE_MSG.get(lang, _ENABLE_MSG["ko"])
    targets = _selected_targets(args, ["claude"])
    if not targets:
        return 1
    if not (base / XOUT_MD).is_file():
        print(msg["no_rules"])
        return 1
    writer = OwnedWriter(base_dir=base)
    if not args.grant:
        for target in targets:
            if target.mode == MODE_IMPORT:
                print(msg["line"].format(id=target.target_id, what=writer.import_line()))
            else:
                print(msg["block"].format(id=target.target_id, what=target.resolve(Path.home(), Path.cwd())))
        print(msg["grant"])
        return 1
    worst = 0
    for target in targets:
        code = _grant_and_enable(base, lang) if target.mode == MODE_IMPORT else _enable_block_target(base, target, lang)
        worst = max(worst, code)
    return worst


def cmd_rollback(args: argparse.Namespace) -> int:
    base = Path(args.base_dir)
    targets = _selected_targets(args, ["all"])
    if not targets:
        return 1
    msg = _ENABLE_MSG.get(_args_lang(args), _ENABLE_MSG["ko"])
    worst = 0
    for target in targets:
        if target.mode == MODE_IMPORT:
            outcome = OwnedWriter(base_dir=base).remove_import()
            print(msg["result"].format(id=target.target_id, reason=outcome.reason, path=outcome.path))
            ok = outcome.reason in ("removed", "not_present", "not_owned")
        else:
            block = remove_block(base, target.target_id, target.resolve(Path.home(), Path.cwd()))
            print(msg["result"].format(id=target.target_id, reason=block.reason, path=block.path))
            ok = block.reason in ("removed", "not_present")
        worst = max(worst, 0 if ok else 1)
    return worst


def cmd_targets(args: argparse.Namespace) -> int:
    """활성화 타깃 목록 - 어느 도구의 어느 파일에 어떤 방식으로 붙는지."""
    base = Path(args.base_dir)
    rows = []
    for target in REGISTRY.values():
        path = target.resolve(Path.home(), Path.cwd())
        if target.mode == MODE_IMPORT:
            state = _activation_state(base)
            active = state["status"] == "active"
        else:
            active = block_state(base, target.target_id, path)["active"]
        rows.append({**target.to_dict(), "resolved_path": str(path), "active": active})
    if args.json_output:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        flag = "active" if row["active"] else "-"
        verified = "" if row["verified"] else "  (unverified)"
        print(f"{row['target_id']:<10} {row['name']:<36} {row['mode']:<7} {flag:<7} {row['resolved_path']}{verified}")
    print()
    print("enable: xout enable --grant --target <id> [<id>...] | undo: xout undo [--target <id>]")
    return 0


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
    body = render_export(events, args.format, _args_lang(args))
    if args.output is None:
        print(body, end="")
    else:
        target = write_export(args.output, body, Path(args.base_dir))
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
    p_mine.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_mine.add_argument("--no-user", dest="include_user", action="store_false", help="~/.claude/CLAUDE.md, ~/.claude/rules 는 읽지 않는다")
    p_mine.add_argument("--runner", default=None, help="옵트인: 이 명령(예: claude -p --output-format text)에게 줄 판정을 맡기고 정규식과 대조한다")
    p_mine.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p_mine.set_defaults(func=cmd_mine)

    p_conflicts = sub.add_parser(
        "conflicts",
        help="컴파일된 규칙과 프로젝트 규칙 파일이 갈리는 줄을 보고 (읽기전용)",
    )
    p_conflicts.add_argument("roots", nargs="*", help="스캔할 루트 (기본: 현재 디렉토리)")
    p_conflicts.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_conflicts.add_argument("--no-user", dest="include_user", action="store_false", help="~/.claude/CLAUDE.md, ~/.claude/rules 는 읽지 않는다")
    p_conflicts.add_argument("--runner", default=None, help="옵트인: 이 명령에게 줄 판정을 맡기고 정규식과 대조한다")
    p_conflicts.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    _add_common(p_conflicts)
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="기존 규칙 파일과 XOUT.md의 중복·모순 보고, 패치 제안, --apply --grant 로 중복 줄 제거",
    )
    p_reconcile.add_argument("roots", nargs="*", help="스캔할 루트 (기본: 현재 디렉토리)")
    p_reconcile.add_argument("--no-user", dest="include_user", action="store_false", help="~/.claude/CLAUDE.md, ~/.claude/rules 는 읽지 않는다")
    p_reconcile.add_argument("--apply", action="store_true", help="중복 줄을 실제로 지운다 (세이브포인트 선행)")
    p_reconcile.add_argument("--grant", action="store_true", help="소유 디렉토리 밖 편집을 허가한다")
    p_reconcile.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    _add_common(p_reconcile)
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_savepoint = sub.add_parser("savepoint", help="바깥 규칙 파일 스냅샷: create(기본) / list / restore <id>")
    p_savepoint.add_argument("action", nargs="?", default="create", choices=("create", "list", "restore"))
    p_savepoint.add_argument("savepoint_id", nargs="?", help="restore 대상 id")
    p_savepoint.add_argument("--paths", nargs="*", help="스냅샷할 파일 (기본: 사용자 규칙 + 현재 디렉토리 규칙 파일)")
    p_savepoint.add_argument("--reason", default=None)
    p_savepoint.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    _add_common(p_savepoint)
    p_savepoint.set_defaults(func=cmd_savepoint)

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
    p_probe.add_argument("--repeat", type=int, default=1, help="케이스마다 시행 횟수 (다수결 판정, 원문은 전부 영수증에)")
    p_probe.add_argument("--context-file", dest="context_file", default=None, help="이 문서를 규칙 앞에 깔아 규칙이 묻힌 상태에서 잰다 (예: 프로젝트 CLAUDE.md)")
    p_probe.add_argument("--via-target", dest="via_target", default=None, help="규칙을 프롬프트에 넣지 않고 이 타깃의 실제 규칙 파일(블록/한 줄)을 뺐다 넣으며 잰다 - 활성 상태여야 한다")
    p_probe.add_argument("--dry-run", dest="dry_run", action="store_true", help="러너 호출 없이 준비된 탐침만 보고")
    p_probe.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
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
    p_pair.add_argument("--new", dest="new_session", action="store_true", help="직전 세션이 완주됐어도 새 세션을 연다")
    p_pair.add_argument("--no-user", dest="include_user", action="store_false", help="~/.claude 규칙은 페어 옆에 보이지 않는다")
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
    p_status.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_status.set_defaults(func=cmd_status)

    p_sessions = sub.add_parser("sessions", help="최근 세션 목록 또는 상세 이벤트")
    _add_common(p_sessions)
    p_sessions.add_argument("session_id", nargs="?", help="상세 조회할 session_id")
    p_sessions.add_argument("--limit", type=int, default=10, help="목록 최대 건수")
    p_sessions.add_argument(
        "--events", type=int, default=10, help="상세 최근 이벤트 건수"
    )
    p_sessions.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_sessions.set_defaults(func=cmd_sessions)

    p_doctor = sub.add_parser("doctor", help="설치와 로컬 데이터 무결성 진단")
    _add_common(p_doctor)
    p_doctor.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
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
    p_inspect.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_inspect.set_defaults(func=cmd_data_inspect)

    p_version = sub.add_parser("version", help="앱/카탈로그/백업 schema 버전")
    _add_common(p_version)
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

    p_targets = sub.add_parser("targets", help="활성화 타깃 목록 (어느 도구의 어느 파일에 어떻게 붙는지)")
    _add_common(p_targets)
    p_targets.add_argument("--json", dest="json_output", action="store_true", help="JSON으로 출력")
    p_targets.set_defaults(func=cmd_targets)

    p_enable = sub.add_parser("enable", help="규칙 활성화: Claude Code는 @import 한 줄, 다른 도구는 소유 블록 (허가 필수)")
    _add_common(p_enable)
    p_enable.add_argument("--target", dest="targets", nargs="*", help="타깃 id (기본 claude; all 가능; 목록: xout targets)")
    p_enable.add_argument(
        "--grant", action="store_true", help="import_permission_granted 허가를 기록한다"
    )
    p_enable.set_defaults(func=cmd_enable)

    p_undo = sub.add_parser("undo", help="활성화 되돌리기: 소유 @import 한 줄/소유 블록만 제거 (기본 all)")
    p_undo.add_argument("--target", dest="targets", nargs="*", help="타깃 id (기본 all)")
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
