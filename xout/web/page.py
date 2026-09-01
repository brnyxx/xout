"""단일 페이지 렌더러.

첫 응답 한 번으로 화면이 완성되도록 서버가 페어와 카운터와 룰을 미리 박아
넣는다. 브라우저가 추가 왕복을 해야 첫 페어가 보이는 구조가 아니다.

템플릿은 popper/web/index.html 한 장뿐이고 외부 자산을 전혀 참조하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Sequence

from xout.web.state import LandingView, RuleView, Snapshot

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parent / "index.html"

TOKEN_BOOT = "{{BOOT_JSON}}"
TOKEN_AXIS_LABEL = "{{AXIS_LABEL}}"
TOKEN_LEFT_TEXT = "{{LEFT_TEXT}}"
TOKEN_RIGHT_TEXT = "{{RIGHT_TEXT}}"
TOKEN_REMAINING = "{{REMAINING}}"
TOKEN_ELIMINATED = "{{ELIMINATED}}"
TOKEN_RULES = "{{RULES}}"
TOKEN_PROFILE = "{{PROFILE_LABEL}}"
TOKEN_SLOTS = "{{SLOTS}}"
TOKEN_SLOTS_TOTAL = "{{SLOTS_TOTAL}}"
TOKEN_SLOTS_USED = "{{SLOTS_USED}}"
TOKEN_PROGRESS = "{{PROGRESS_MARKS}}"
TOKEN_BANNER = "{{BANNER}}"
TOKEN_BODY_STATE = "{{BODY_STATE}}"
TOKEN_LANDING = "{{LANDING}}"

#: 프로파일의 화면 표기 - 검증 요구가 일반 세션 UX를 훼손하지 않도록 이름만 다르다.
PROFILE_LABELS: dict[str, str] = {
    "product": "일반 세션",
    "validation": "검증 세션",
    "recheck": "재심 세션",
}

#: corroboration 등급의 화면 표기.
GRADE_LABELS: dict[str, str] = {
    "discriminated": "판별시험 통과",
    "indiscriminate": "무차별 생존",
    "untested": "미시험",
    "unstable": "불안정",
}

LANDING_HEADLINES: dict[str, str] = {
    "landed": "산출물이 착지했다",
    "blocked": "수기 편집이 감지되어 착지를 멈췄다",
    "skipped": "이 세션은 산출물을 착지시키지 않는다",
    "voided": "세션이 무효로 판정되어 착지하지 않았다",
    "failed": "착지 중 오류가 났다",
}


class TemplateMissing(RuntimeError):
    """단일 페이지 템플릿을 찾지 못했다."""


@lru_cache(maxsize=None)
def _template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        logger.exception("단일 페이지 템플릿을 읽지 못했다: %s", TEMPLATE_PATH)
        raise TemplateMissing(f"템플릿을 읽지 못했다: {TEMPLATE_PATH}") from e


def _script_safe_json(payload: object) -> str:
    """script 블록 안에서 조기 종료를 일으키지 않도록 꺾쇠와 앰퍼샌드를 이스케이프한다."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_rules(rules: Sequence[RuleView]) -> str:
    """컴파일 페인 목록의 서버 사이드 마크업."""
    items: list[str] = []
    for rule in rules:
        grade = GRADE_LABELS.get(rule.corroboration_grade, rule.corroboration_grade)
        items.append(
            '<li data-grade="{raw}"><span class="rule-axis">{axis}'
            '<em class="rule-grade">{grade}</em></span>'
            '<span class="rule-text">{text}</span></li>'.format(
                raw=escape(rule.corroboration_grade, quote=True),
                axis=escape(rule.axis_label),
                grade=escape(grade),
                text=escape(rule.text),
            )
        )
    return "".join(items)


def render_progress(slots_used: int, slots_total: int) -> str:
    """슬롯 진행 눈금 - 입력 어포던스가 아니라 읽기 전용 표식이다."""
    marks: list[str] = []
    for slot in range(1, slots_total + 1):
        state = "used" if slot <= slots_used else "open"
        marks.append(f'<i class="slot slot-{state}"></i>')
    return "".join(marks)


def render_landing(landing: LandingView | None) -> str:
    """세션 종료 착지 요약 - 링크/버튼 없는 순수 텍스트 마크업."""
    if landing is None:
        return ""
    headline = LANDING_HEADLINES.get(landing.status, landing.status)
    lines: list[str] = [f'<h3 class="landing-head">{escape(headline)}</h3>']
    if landing.detail:
        lines.append(f'<p class="landing-detail">{escape(landing.detail)}</p>')
    if landing.written:
        rows = "".join(f"<li><code>{escape(path)}</code></li>" for path in landing.written)
        lines.append(f'<ul class="landing-paths">{rows}</ul>')
    if landing.import_line:
        lines.append(
            '<p class="landing-import">CLAUDE.md 활성화는 허가가 있어야 한다 - '
            "<code>popper enable --grant</code> 실행 시 아래 한 줄만 추가된다</p>"
            f"<pre class=\"landing-line\">{escape(landing.import_line)}</pre>"
        )
    if landing.detections:
        rows = "".join(
            "<li><code>{path}</code> <span>{reason}</span></li>".format(
                path=escape(str(d.get("path", ""))),
                reason=escape(str(d.get("reason", ""))),
            )
            for d in landing.detections
        )
        lines.append(f'<ul class="landing-detections">{rows}</ul>')
        lines.append(
            '<p class="landing-detail">다시 착지하려면 <code>popper land'
            " --acknowledge-mismatch</code> - 감지 기록은 manifest에 남는다</p>"
        )
    return "".join(lines)


def render_page(snapshot: Snapshot) -> str:
    """스냅샷 하나를 완성된 단일 페이지 HTML로 만든다."""
    pair = snapshot.pair
    profile_label = PROFILE_LABELS.get(snapshot.profile, snapshot.profile)
    replacements = (
        (TOKEN_BOOT, _script_safe_json(snapshot.to_dict())),
        (TOKEN_AXIS_LABEL, escape(pair.axis_label) if pair else ""),
        (TOKEN_LEFT_TEXT, escape(pair.left_text) if pair else ""),
        (TOKEN_RIGHT_TEXT, escape(pair.right_text) if pair else ""),
        (TOKEN_REMAINING, f"{snapshot.remaining_combinations:,}"),
        (TOKEN_ELIMINATED, str(snapshot.eliminated_pairs)),
        (TOKEN_RULES, render_rules(snapshot.rules)),
        (TOKEN_PROFILE, escape(profile_label)),
        (TOKEN_SLOTS, f"{snapshot.slots_used}/{snapshot.slots_total}"),
        (TOKEN_SLOTS_TOTAL, str(snapshot.slots_total)),
        (TOKEN_SLOTS_USED, str(snapshot.slots_used)),
        (TOKEN_PROGRESS, render_progress(snapshot.slots_used, snapshot.slots_total)),
        (TOKEN_BANNER, escape(snapshot.banner) if snapshot.banner else ""),
        (TOKEN_BODY_STATE, "complete" if snapshot.session_complete else "live"),
        (TOKEN_LANDING, render_landing(snapshot.landing)),
    )
    # Replace against the original template in one pass.  A replacement value is
    # user-controlled text and must never be scanned again for template tokens.
    values = dict(replacements)
    token_pattern = re.compile("|".join(re.escape(token) for token, _ in replacements))
    return token_pattern.sub(lambda match: values[match.group(0)], _template())
