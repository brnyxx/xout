"""AC8 - 콜드 오픈 로컬 UI 검증.

확인하는 것은 넷이다.

  (a) 질문 0개로 열려서 첫 응답 안에 이미 자율성 축 첫 페어가 들어 있다.
  (b) 화면의 유일한 입력 어포던스가 긋기다 - 확인/승인 컨트롤이 없다.
  (c) 긋는 순간 가설 카운터와 컴파일 페인의 룰이 함께 갱신된다.
  (d) 런타임이 픽스처 밖으로 나가는 호출을 하지 않는다.

서버는 포트 0으로 띄워 urllib로 두드린다. 대기는 전부 소켓 backlog와 응답
수신으로 처리하며 time.sleep()은 쓰지 않는다.
"""

from __future__ import annotations

import ast
import json
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from xout.counter import DEFAULT_CATALOG, INITIAL_COMBINATIONS
from xout.web.page import TEMPLATE_PATH, render_page
from xout.web.server import build_server
from xout.web.state import (
    COLD_OPEN_AXIS,
    STRIKE_TARGETS,
    ColdOpenSession,
    PairView,
    RuleView,
    Snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "xout" / "web"

#: AC8이 요구하는 콜드 오픈 상한 - 기동부터 첫 페어까지.
COLD_OPEN_BUDGET_SECONDS = 10.0

#: 요청 하나가 매달릴 수 있는 최대 시간. 초과하면 테스트가 걸리지 않고 깨진다.
REQUEST_TIMEOUT_SECONDS = 10.0

#: 확인/승인 계열 컨트롤 어휘 - 어포던스 라벨에 하나라도 있으면 AC 위반이다.
FORBIDDEN_CONTROL_KO = (
    "확인",
    "승인",
    "취소",
    "제출",
    "저장",
    "적용",
    "완료",
    "동의",
    "계속",
    "다음",
    "시작하기",
)
FORBIDDEN_CONTROL_EN = re.compile(
    r"\b(submit|confirm|approve|accept|cancel|ok|okay|save|next|apply|agree|continue)\b",
    re.IGNORECASE,
)

#: 온보딩/설문 화면을 의심하게 하는 구조 요소.
QUESTION_TAGS = ("form", "input", "select", "textarea", "dialog", "fieldset")

#: popper/web 이 기대는 표준 라이브러리 루트. 그 밖의 최상위 모듈은 허용하지 않는다.
ALLOWED_IMPORT_ROOTS = frozenset(
    {
        "__future__",
        "argparse",
        "contextlib",
        "dataclasses",
        "functools",
        "hashlib",
        "html",
        "http",
        "ipaddress",
        "json",
        "logging",
        "pathlib",
        "re",
        "threading",
        "typing",
        "uuid",
        "xout",
    }
)

#: 바깥으로 나가는 호출이나 LLM 클라이언트를 의미하는 어휘.
OUTBOUND_LEXEMES = (
    "urllib",
    "http.client",
    "httplib",
    "requests.",
    "httpx",
    "aiohttp",
    "smtplib",
    "ftplib",
    "telnetlib",
    "socket.create_connection",
    "subprocess",
    "openai",
    "anthropic",
    "urlopen",
)

_LOOPBACK_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\{host\})")
_ANY_URL = re.compile(r"https?://[^\s\"'`)>]*")


@dataclass(frozen=True, slots=True)
class Affordance:
    """페이지에서 사용자가 건드릴 수 있는 요소 하나."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def label(self) -> str:
        """접근 가능한 이름 - aria-label이 있으면 본문 텍스트를 덮어쓴다."""
        return (self.attrs.get("aria-label") or self.text).strip()

    @property
    def strike_target(self) -> str | None:
        return self.attrs.get("data-strike-target")

    @property
    def recovery_channel(self) -> str | None:
        """AC3의 명시 복구 채널 표식 - 긍정 입력이 아니라 오긋기 복구다."""
        return self.attrs.get("data-recovery-channel")


class AffordanceScan(HTMLParser):
    """입력 어포던스가 될 수 있는 요소만 골라 담는 파서."""

    INTERACTIVE_TAGS = frozenset(
        {"form", "input", "select", "textarea", "button", "dialog", "fieldset"}
    )
    VOID_TAGS = frozenset(
        {"meta", "link", "br", "hr", "img", "input", "source", "col", "area"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.affordances: list[Affordance] = []
        self.tags: Counter[str] = Counter()
        self.hyperlinks: int = 0
        self.nested_affordance: bool = False
        self._stack: list[str] = []
        self._open: list | None = None

    @staticmethod
    def _is_affordance(tag: str, attrs: dict[str, str]) -> bool:
        if tag in AffordanceScan.INTERACTIVE_TAGS:
            return True
        if "data-strike-target" in attrs:
            return True
        if attrs.get("role") in ("button", "link", "checkbox", "radio", "switch"):
            return True
        return "contenteditable" in attrs

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrib = {key: (value or "") for key, value in attrs}
        self.tags[tag] += 1
        if tag == "a" and "href" in attrib:
            self.hyperlinks += 1

        if tag in self.VOID_TAGS:
            if self._is_affordance(tag, attrib):
                self.affordances.append(Affordance(tag=tag, attrs=attrib))
            return

        self._stack.append(tag)
        if not self._is_affordance(tag, attrib):
            return
        if self._open is not None:
            self.nested_affordance = True
            return
        self._open = [tag, attrib, [], len(self._stack)]

    def handle_endtag(self, tag: str) -> None:
        if self._open is not None and len(self._stack) == self._open[3]:
            open_tag, attrib, buffer, _ = self._open
            self.affordances.append(
                Affordance(tag=open_tag, attrs=attrib, text=" ".join(buffer).strip())
            )
            self._open = None
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._open is None or not self._stack:
            return
        if self._stack[-1] in ("script", "style"):
            return
        chunk = data.strip()
        if chunk:
            self._open[2].append(chunk)


def scan_page(html: str) -> AffordanceScan:
    parser = AffordanceScan()
    parser.feed(html)
    parser.close()
    return parser


def boot_payload(html: str) -> dict:
    """서버가 첫 응답에 박아 넣은 부트 상태를 꺼낸다."""
    match = re.search(
        r'<script id="xout-boot" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("첫 응답에 부트 상태가 들어 있지 않다")
    return json.loads(match.group(1))


def web_sources() -> tuple[Path, ...]:
    return tuple(sorted(WEB_DIR.glob("*.py")))


class ServerCase(unittest.TestCase):
    """포트 0으로 띄운 로컬 서버를 두드리는 공통 베이스."""

    #: shutdown() 응답 지연을 줄이는 accept 루프 폴링 간격.
    POLL_INTERVAL_SECONDS = 0.02

    def launch(self, session: ColdOpenSession | None = None):
        server = build_server(session=session)
        thread = threading.Thread(
            target=server.serve_forever,
            args=(self.POLL_INTERVAL_SECONDS,),
            name="popper-cold-open",
            daemon=True,
        )
        # 생성자에서 bind/listen이 끝나 있으므로 스레드 기동 직후 바로 두드려도 된다.
        thread.start()
        self.addCleanup(thread.join, REQUEST_TIMEOUT_SECONDS)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def _url(server, path: str) -> str:
        return server.url.rstrip("/") + path

    def get(self, server, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(
            self._url(server, path), timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, server, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self._url(server, path),
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def state(self, server) -> dict:
        status, body = self.get(server, "/state")
        self.assertEqual(status, 200)
        return json.loads(body)


class ColdOpenTest(ServerCase):
    """(a) 질문 0개로 열려 첫 페어가 곧바로 뜬다."""

    def test_first_paint_is_the_autonomy_pair(self) -> None:
        started = time.monotonic()
        server = self.launch()
        status, html = self.get(server, "/")
        elapsed = time.monotonic() - started

        self.assertEqual(status, 200)
        self.assertLess(
            elapsed,
            COLD_OPEN_BUDGET_SECONDS,
            f"콜드 오픈이 {COLD_OPEN_BUDGET_SECONDS}초를 넘겼다: {elapsed:.3f}초",
        )

        boot = boot_payload(html)
        self.assertEqual(boot["pair"]["axis"], COLD_OPEN_AXIS)
        self.assertEqual(boot["pair"]["axis_label"], "자율성")
        self.assertIn(boot["pair"]["left_value"], DEFAULT_CATALOG[COLD_OPEN_AXIS])
        self.assertIn(boot["pair"]["right_value"], DEFAULT_CATALOG[COLD_OPEN_AXIS])
        self.assertNotEqual(boot["pair"]["left_value"], boot["pair"]["right_value"])

    def test_first_response_already_carries_the_transcripts(self) -> None:
        """추가 왕복 없이 첫 응답 한 장으로 페어가 보인다."""
        server = self.launch()
        _, html = self.get(server, "/")
        boot = boot_payload(html)

        head_left = boot["pair"]["left_text"].splitlines()[0]
        self.assertIn(head_left, html)
        self.assertIn('id="left-text"', html)
        self.assertIn('id="right-text"', html)
        self.assertNotIn("{{", html)

    def test_nothing_is_asked_before_the_pair(self) -> None:
        """온보딩/설문 구조가 아예 없다 - 답을 넣을 자리가 없다."""
        server = self.launch()
        _, html = self.get(server, "/")
        scan = scan_page(html)

        for tag in QUESTION_TAGS:
            with self.subTest(tag=tag):
                self.assertEqual(scan.tags[tag], 0, f"<{tag}> 요소가 존재한다")
        self.assertEqual(scan.hyperlinks, 0, "다른 화면으로 새는 링크가 있다")
        self.assertNotIn("onsubmit", html)
        self.assertNotIn("contenteditable", html)

    def test_cold_open_needs_no_prior_events(self) -> None:
        """세션은 이벤트 0건에서 시작하고 카운터는 초기값 그대로다."""
        server = self.launch()
        snapshot = self.state(server)

        self.assertEqual(snapshot["strike_count"], 0)
        self.assertEqual(snapshot["remaining_combinations"], INITIAL_COMBINATIONS)
        self.assertEqual(snapshot["eliminated_pairs"], 0)
        self.assertIsNone(snapshot["last_strike"])
        self.assertEqual(len(snapshot["rules"]), len(DEFAULT_CATALOG))


class StrikeOnlyAffordanceTest(ServerCase):
    """(b) 화면의 유일한 입력 어포던스가 긋기다."""

    def setUp(self) -> None:
        server = self.launch()
        _, self.html = self.get(server, "/")
        self.scan = scan_page(self.html)

    def test_every_affordance_is_a_strike_or_the_undo_channel(self) -> None:
        """어포던스는 긋기 아니면 AC3의 undo_tombstone 명시 복구 채널뿐이다."""
        self.assertFalse(self.scan.nested_affordance, "어포던스가 중첩됐다")
        self.assertTrue(self.scan.affordances, "어포던스를 하나도 찾지 못했다")
        for affordance in self.scan.affordances:
            with self.subTest(tag=affordance.tag, label=affordance.label):
                self.assertTrue(
                    affordance.strike_target is not None
                    or affordance.recovery_channel == "undo_tombstone",
                    f"긋기도 복구 채널도 아닌 어포던스가 있다: "
                    f"<{affordance.tag}> {affordance.label!r}",
                )

    def test_recovery_channel_is_single_and_explicit(self) -> None:
        """복구 채널 어포던스는 undo_tombstone 하나뿐이고 긋기와 겹치지 않는다."""
        channels = [
            a for a in self.scan.affordances if a.recovery_channel is not None
        ]
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].recovery_channel, "undo_tombstone")
        self.assertIsNone(channels[0].strike_target)

    def test_strike_targets_are_exactly_the_four(self) -> None:
        targets = [
            a.strike_target
            for a in self.scan.affordances
            if a.strike_target is not None
        ]
        self.assertEqual(sorted(targets), sorted(STRIKE_TARGETS))
        self.assertEqual(len(targets), len(set(targets)))

    def test_no_confirm_or_approve_control(self) -> None:
        for affordance in self.scan.affordances:
            label = affordance.label
            with self.subTest(label=label):
                self.assertIn("긋기", label, "긋기라고 말하지 않는 컨트롤이 있다")
                for word in FORBIDDEN_CONTROL_KO:
                    self.assertNotIn(word, label)
                self.assertIsNone(FORBIDDEN_CONTROL_EN.search(label))

    def test_buttons_never_submit(self) -> None:
        for affordance in self.scan.affordances:
            if affordance.tag != "button":
                continue
            with self.subTest(label=affordance.label):
                self.assertEqual(affordance.attrs.get("type"), "button")
        self.assertNotIn('type="submit"', self.html)

    def test_panes_are_reachable_by_keyboard(self) -> None:
        panes = [
            affordance
            for affordance in self.scan.affordances
            if affordance.tag == "button"
            and "pane" in affordance.attrs.get("class", "").split()
        ]
        self.assertEqual(len(panes), 2)
        for pane in panes:
            with self.subTest(target=pane.strike_target):
                self.assertEqual(pane.attrs.get("type"), "button")
                self.assertIsNone(pane.attrs.get("role"))
                self.assertEqual(pane.attrs.get("tabindex"), "0")
                self.assertIn("-text", pane.attrs.get("aria-labelledby", ""))

    def test_server_accepts_no_verb_other_than_striking(self) -> None:
        """쓰기 경로는 /strike 하나뿐이고 승인 경로는 존재하지 않는다."""
        server = self.launch()
        for path in ("/approve", "/confirm", "/commit", "/settings"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post(server, path, {"target": "left"})
                self.assertEqual(caught.exception.code, 404)


class LiveUpdateTest(ServerCase):
    """(c) 긋는 순간 카운터와 컴파일 페인이 함께 갱신된다."""

    def setUp(self) -> None:
        self.server = self.launch()
        self.before = self.state(self.server)

    @staticmethod
    def _rule(snapshot: dict, axis: str) -> dict:
        for rule in snapshot["rules"]:
            if rule["axis"] == axis:
                return rule
        raise AssertionError(f"룰에 {axis} 축이 없다")

    def test_left_strike_moves_counter_and_rewrites_the_rule(self) -> None:
        status, after = self.post(self.server, "/strike", {"target": "left"})

        self.assertEqual(status, 200)
        self.assertLess(
            after["remaining_combinations"], self.before["remaining_combinations"]
        )
        self.assertEqual(after["eliminated_pairs"], self.before["eliminated_pairs"] + 1)
        self.assertEqual(after["strike_count"], 1)
        self.assertEqual(after["last_strike"], "left")

        struck = self.before["pair"]["left_value"]
        rule = self._rule(after, COLD_OPEN_AXIS)
        self.assertNotEqual(rule["value"], struck)
        self.assertNotEqual(rule["text"], self._rule(self.before, COLD_OPEN_AXIS)["text"])
        self.assertEqual(len(after["rules"]), len(DEFAULT_CATALOG))

    def test_both_strike_removes_two_values_at_once(self) -> None:
        _, after = self.post(self.server, "/strike", {"target": "both"})

        self.assertEqual(after["eliminated_pairs"], 2)
        self.assertLess(
            after["remaining_combinations"], self.before["remaining_combinations"]
        )
        self.assertEqual(after["last_strike"], "both")

    def test_pair_strike_is_recorded_without_eliminating(self) -> None:
        """페어 긋기는 판별력-없음 이벤트다 - 카운터는 그대로 남는다."""
        _, after = self.post(self.server, "/strike", {"target": "pair"})

        self.assertEqual(
            after["remaining_combinations"], self.before["remaining_combinations"]
        )
        self.assertEqual(after["eliminated_pairs"], self.before["eliminated_pairs"])
        self.assertEqual(after["strike_count"], 1)

    def test_strike_advances_to_the_next_pair(self) -> None:
        _, after = self.post(self.server, "/strike", {"target": "left"})
        self.assertNotEqual(after["pair"]["pair_id"], self.before["pair"]["pair_id"])

    def test_strike_response_matches_the_state_endpoint(self) -> None:
        _, after = self.post(self.server, "/strike", {"target": "right"})
        self.assertEqual(after, self.state(self.server))

    def test_counter_is_a_replay_of_the_event_stream(self) -> None:
        """카운터는 저장된 값이 아니라 스트림 fold다 - 같은 스트림이면 같은 값이다."""
        for target in ("left", "pair", "right"):
            self.post(self.server, "/strike", {"target": target})
        served = self.state(self.server)

        session = ColdOpenSession(session_id="replay")
        for target in ("left", "pair", "right"):
            replayed = session.strike(target)

        self.assertEqual(
            served["remaining_combinations"], replayed.remaining_combinations
        )
        self.assertEqual(served["eliminated_pairs"], replayed.eliminated_pairs)
        self.assertEqual(
            [rule["text"] for rule in served["rules"]],
            [rule.text for rule in replayed.rules],
        )

    def test_unknown_strike_target_is_rejected(self) -> None:
        for target in ("approve", "undo", ""):
            with self.subTest(target=target):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post(self.server, "/strike", {"target": target})
                self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.state(self.server)["strike_count"], 0)

    def test_malformed_body_is_rejected(self) -> None:
        request = urllib.request.Request(
            self._url(self.server, "/strike"),
            method="POST",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(caught.exception.code, 400)

    def test_unknown_path_is_not_served(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(self.server, "/admin")
        self.assertEqual(caught.exception.code, 404)


class DeterminismTest(unittest.TestCase):
    """콜드 오픈 순서는 결정론적이다 - 매 기동이 같은 첫 페어를 낸다."""

    def test_pair_order_is_stable_across_instances(self) -> None:
        first = ColdOpenSession(session_id="a")
        second = ColdOpenSession(session_id="b")
        self.assertEqual(
            first.snapshot().pair.pair_id, second.snapshot().pair.pair_id
        )

    def test_cold_open_axis_leads_the_whole_queue(self) -> None:
        session = ColdOpenSession(session_id="c")
        seen: list[str] = []
        for _ in range(len(DEFAULT_CATALOG[COLD_OPEN_AXIS])):
            seen.append(session.snapshot().pair.axis)
            session.strike("pair")
        self.assertEqual(seen, [COLD_OPEN_AXIS] * len(seen))


class OfflineRuntimeTest(unittest.TestCase):
    """(d) 런타임이 픽스처 밖으로 나가지 않는다."""

    def test_web_package_imports_only_stdlib_and_popper(self) -> None:
        for path in web_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    roots = [(node.module or "").split(".")[0]]
                else:
                    continue
                for root in roots:
                    with self.subTest(path=path.name, module=root):
                        self.assertIn(root, ALLOWED_IMPORT_ROOTS)

    def test_no_outbound_client_in_runtime_sources(self) -> None:
        for path in web_sources() + (TEMPLATE_PATH,):
            text = path.read_text(encoding="utf-8")
            for lexeme in OUTBOUND_LEXEMES:
                with self.subTest(path=path.name, lexeme=lexeme):
                    self.assertNotIn(lexeme, text)

    def test_no_external_asset_or_endpoint(self) -> None:
        for path in web_sources() + (TEMPLATE_PATH,):
            text = path.read_text(encoding="utf-8")
            for url in _ANY_URL.findall(text):
                with self.subTest(path=path.name, url=url):
                    self.assertIsNotNone(
                        _LOOPBACK_URL.match(url), f"외부 주소를 참조한다: {url}"
                    )

    def test_template_is_the_only_asset(self) -> None:
        self.assertTrue(TEMPLATE_PATH.is_file())
        assets = sorted(p.name for p in WEB_DIR.iterdir() if p.is_file())
        self.assertEqual(
            assets,
            ["__init__.py", "__main__.py", "index.html", "page.py", "server.py", "state.py"],
        )


class RenderingHardeningTest(unittest.TestCase):
    """사용자 텍스트가 템플릿/스크립트 경계를 넘지 않는지 고정한다."""

    def test_token_like_and_script_text_are_not_reinterpreted(self) -> None:
        text = '</script><script>alert("x")</script>{{RIGHT_TEXT}}'
        pair = PairView(
            pair_id="p",
            scene_id="s",
            axis="a",
            axis_label="{{LEFT_TEXT}}",
            left_value="l",
            right_value="r",
            left_text=text,
            right_text="{{LEFT_TEXT}}",
        )
        snapshot = Snapshot(
            session_id="session",
            profile="product",
            pair=pair,
            remaining_combinations=1,
            eliminated_pairs=0,
            strike_count=0,
            slots_used=0,
            slots_total=3,
            rules=(RuleView("a", "axis", "v", text),),
        )
        rendered = render_page(snapshot)
        payload = boot_payload(rendered)
        self.assertEqual(payload["pair"]["left_text"], text)
        self.assertEqual(payload["pair"]["right_text"], "{{LEFT_TEXT}}")
        self.assertNotIn("</script><script>", rendered)
        self.assertIn("&lt;/script&gt;&lt;script&gt;", rendered)

    def test_progress_and_feedback_accessibility_contract(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn('role="progressbar"', template)
        self.assertIn('aria-valuemin="0"', template)
        self.assertIn('aria-valuemax="{{SLOTS_TOTAL}}"', template)
        self.assertIn('role="status"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn("clearStriking();", template)


if __name__ == "__main__":
    unittest.main()
