"""프로덕션 런타임 검증 - 세션 완주, 착지, 복구 채널, 프로파일, 영속화.

시드의 exit condition을 코드로 못 박는다:
  - 일반 세션 1회가 cap 15로 완주해 POPPER.md/manifest.json/settings.popper.json이
    소유 디렉토리에 착지한다.
  - 재심 진입 경로(배너 + 수동)가 동작한다.
  - 검증 세션은 판별 13 + 슬롯 9/13 미러 프로브 2로 완주하고 착지하지 않는다.
  - 오긋기 복구는 undo_tombstone 명시 채널뿐이고 슬롯을 돌려주지 않는다.
"""

from __future__ import annotations

import json
import http.client
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from xout.compiler import MANIFEST_JSON, POPPER_MD, SETTINGS_JSON
from xout.counter import INITIAL_COMBINATIONS, fold
from xout.events import EventType, SchemaViolation, StrikeEvent
from xout.session import PROFILE_VALIDATION, fold_session, load_session_specs
from xout.store import EventStore
from xout.web.server import build_server
from xout.web.state import (
    PROFILE_RECHECK,
    ColdOpenSession,
    RecoveryUnavailable,
    SessionComplete,
    StalePresentation,
)

REQUEST_TIMEOUT_SECONDS = 10.0

PRODUCT_CAP = 15
VALIDATION_PROBE_SLOTS = (9, 13)


def make_session(base: Path, **kwargs) -> ColdOpenSession:
    return ColdOpenSession(
        store=EventStore(base),
        land_dir=base,
        session_id=kwargs.pop("session_id", "prod-1"),
        **kwargs,
    )


class ProductCompletionTest(unittest.TestCase):
    """일반 세션 - 정확히 15긋기로 닫히고 산출물이 착지한다."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.session = make_session(self.base)

    def run_to_cap(self, target: str = "left") -> None:
        for _ in range(PRODUCT_CAP):
            self.session.strike(target)

    def test_fifteen_strikes_close_the_session_and_land(self) -> None:
        self.run_to_cap()
        snapshot = self.session.snapshot()

        self.assertTrue(snapshot.session_complete)
        self.assertEqual(snapshot.slots_used, PRODUCT_CAP)
        self.assertIsNone(snapshot.voided_reason)
        self.assertIsNotNone(snapshot.landing)
        self.assertEqual(snapshot.landing.status, "landed")
        for name in (POPPER_MD, MANIFEST_JSON, SETTINGS_JSON):
            self.assertTrue((self.base / name).exists(), f"{name} 미착지")
        self.assertTrue(snapshot.landing.import_line.startswith("@"))

    def test_session_validated_event_is_emitted_and_persisted(self) -> None:
        self.run_to_cap()
        stored = EventStore(self.base).load_session("prod-1")
        types = [event.type for event in stored]
        self.assertIn(EventType.SESSION_VALIDATED, types)
        self.assertEqual(
            sum(1 for event in stored if isinstance(event, StrikeEvent)), PRODUCT_CAP
        )

    def test_sixteenth_strike_is_rejected_without_extension(self) -> None:
        self.run_to_cap()
        with self.assertRaises(SessionComplete):
            self.session.strike("left")

    def test_manifest_carries_recheck_queue_and_last_review(self) -> None:
        self.run_to_cap()
        manifest = json.loads((self.base / MANIFEST_JSON).read_text(encoding="utf-8"))
        self.assertIn("recheck_queue", manifest)
        self.assertIsNotNone(manifest.get("last_review"))
        self.assertEqual(manifest.get("session_id"), "prod-1")

    def test_store_replay_reproduces_the_served_counter(self) -> None:
        self.run_to_cap()
        served = self.session.snapshot()
        stored = EventStore(self.base).load_session("prod-1")
        replayed = fold(stored)
        self.assertEqual(
            served.remaining_combinations, replayed.remaining_combinations
        )
        self.assertEqual(served.eliminated_pairs, replayed.eliminated_pairs)


class VoidedSessionTest(unittest.TestCase):
    """축 하한 미달 세션 - session_voided 방출, 착지 없음."""

    def test_indiscriminate_session_is_voided_and_not_landed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            session = make_session(base, session_id="void-1")
            for _ in range(PRODUCT_CAP):
                session.strike("pair")
            snapshot = session.snapshot()

            self.assertTrue(snapshot.session_complete)
            self.assertEqual(snapshot.voided_reason, "axis_shortfall")
            self.assertEqual(snapshot.landing.status, "voided")
            for name in (POPPER_MD, MANIFEST_JSON, SETTINGS_JSON):
                self.assertFalse((base / name).exists(), f"{name}이 착지됐다")
            types = [e.type for e in EventStore(base).load_session("void-1")]
            self.assertIn(EventType.SESSION_VOIDED, types)
            self.assertNotIn(EventType.SESSION_VALIDATED, types)


class UndoChannelTest(unittest.TestCase):
    """오긋기 복구 - undo_tombstone 명시 채널, 슬롯 미반환, 페어 재등판."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = make_session(Path(self.tmp.name), session_id="undo-1")

    def test_undo_restores_the_counter_and_reserves_the_pair(self) -> None:
        first = self.session.snapshot()
        struck = self.session.strike("left")
        self.assertLess(
            struck.remaining_combinations, first.remaining_combinations
        )
        self.assertNotEqual(struck.pair.pair_id, first.pair.pair_id)

        undone = self.session.undo()
        self.assertEqual(undone.remaining_combinations, INITIAL_COMBINATIONS)
        self.assertEqual(undone.eliminated_pairs, 0)
        self.assertEqual(undone.pair.pair_id, first.pair.pair_id)

    def test_undo_does_not_refund_the_slot(self) -> None:
        self.session.strike("left")
        undone = self.session.undo()
        self.assertEqual(undone.slots_used, 1)
        restruck = self.session.strike("left")
        self.assertEqual(restruck.slots_used, 2)

    def test_undo_event_is_explicit_and_references_the_strike(self) -> None:
        struck_pair = self.session.snapshot().pair.pair_id
        self.session.strike("left")
        self.session.undo()
        events = self.session.log.events
        tombstones = [
            e for e in events if getattr(e, "type", None) is EventType.UNDO_TOMBSTONE
        ]
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0].payload.get("pair_id"), struck_pair)
        origin = tombstones[0].payload.get("strike_event_id")
        self.assertTrue(
            any(
                isinstance(e, StrikeEvent) and e.event_id == origin for e in events
            )
        )

    def test_undo_without_any_strike_is_rejected(self) -> None:
        with self.assertRaises(RecoveryUnavailable):
            self.session.undo()

    def test_undoable_flag_follows_the_stream(self) -> None:
        self.assertFalse(self.session.snapshot().undoable)
        self.session.strike("left")
        self.assertTrue(self.session.snapshot().undoable)
        self.session.undo()
        self.assertFalse(self.session.snapshot().undoable)


class ValidationProbeTest(unittest.TestCase):
    """검증 세션 - 판별 13 + 슬롯 9/13 미러 프로브 2, 착지 없음."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.session = make_session(
            self.base, session_id="val-1", profile=PROFILE_VALIDATION
        )

    def test_probe_slots_serve_mirrored_first_half_pairs(self) -> None:
        served: list[tuple[str, str, str]] = []
        for _ in range(8):
            snapshot = self.session.snapshot()
            served.append(
                (
                    snapshot.pair.pair_id,
                    snapshot.pair.left_text,
                    snapshot.pair.right_text,
                )
            )
            self.session.strike("left")

        probe = self.session.snapshot()
        self.assertEqual(probe.slots_used, VALIDATION_PROBE_SLOTS[0])
        origin_id, origin_left, origin_right = served[0]
        self.assertEqual(probe.pair.pair_id, origin_id)
        self.assertEqual(probe.pair.left_text, origin_right)
        self.assertEqual(probe.pair.right_text, origin_left)

    def test_full_validation_run_records_probe_results_and_skips_landing(self) -> None:
        # 슬롯 1-8 판별 긋기.
        for _ in range(8):
            self.session.strike("left")
        # 슬롯 9 프로브 - 원본 left 긋기의 미러 일관 반응은 right.
        self.session.strike("right")
        # 슬롯 10-12 판별 긋기.
        for _ in range(3):
            self.session.strike("left")
        # 슬롯 13 프로브 - 미러 기대와 다른 left 긋기는 flip.
        self.session.strike("left")
        # 슬롯 14-15 판별 긋기.
        for _ in range(2):
            self.session.strike("left")

        snapshot = self.session.snapshot()
        self.assertTrue(snapshot.session_complete)
        self.assertEqual(snapshot.landing.status, "skipped")
        for name in (POPPER_MD, MANIFEST_JSON, SETTINGS_JSON):
            self.assertFalse((self.base / name).exists(), f"{name}이 착지됐다")

        events = self.session.log.events
        strikes = [e for e in events if isinstance(e, StrikeEvent)]
        shown = [e for e in events if getattr(e, "type", None) is EventType.PROBE_SHOWN]
        results = [
            e for e in events if getattr(e, "type", None) is EventType.PROBE_RESULT
        ]
        self.assertEqual(len(strikes), 13)
        self.assertEqual([e.payload["slot"] for e in shown], list(VALIDATION_PROBE_SLOTS))
        self.assertEqual(
            [e.payload["result"] for e in results], ["consistent", "flip"]
        )
        self.assertTrue(all(e.payload.get("mirrored") is True for e in shown))

        judgment = fold_session(
            events, {PROFILE_VALIDATION: load_session_specs()[PROFILE_VALIDATION]}
        )
        self.assertTrue(judgment.complete)
        self.assertTrue(judgment.stream_valid, judgment.reasons)
        self.assertIn(EventType.SESSION_VALIDATED, [e.type for e in events[-2:]])

    def test_probe_is_visually_indistinguishable_in_the_payload(self) -> None:
        """프로브 스냅샷에 프로브 표식 키가 없다 - 일반 페어와 같은 스키마다."""
        for _ in range(8):
            self.session.strike("left")
        probe_dict = self.session.snapshot().to_dict()
        self.assertNotIn("probe", json.dumps(probe_dict))

    def test_validation_event_carries_discriminative_evidence(self) -> None:
        for _ in range(15):
            self.session.strike("left")
        validated = [
            e for e in self.session.log.events
            if e.type is EventType.SESSION_VALIDATED
        ]
        self.assertEqual(len(validated), 1)
        self.assertGreater(validated[0].payload["discriminative_instances"], 0)
        self.assertGreater(
            validated[0].payload["correct_restorations"]
            + validated[0].payload["mis_restorations"],
            0,
        )

    def test_probe_response_is_not_undoable(self) -> None:
        for _ in range(8):
            self.session.strike("left")
        self.session.strike("right")
        self.assertFalse(self.session.snapshot().undoable)
        with self.assertRaises(RecoveryUnavailable):
            self.session.undo()

    def test_stale_presentation_is_rejected(self) -> None:
        with self.assertRaises(StalePresentation):
            self.session.strike(
                "left", expected_pair_id="stale", expected_slot=1
            )
        self.assertEqual(len(self.session.log.strikes()), 0)

    def test_validation_sessions_use_distinct_deterministic_sequences(self) -> None:
        first = ColdOpenSession(session_id="val-sequence-a", profile=PROFILE_VALIDATION)
        second = ColdOpenSession(session_id="val-sequence-b", profile=PROFILE_VALIDATION)
        first_ids = [first.snapshot().pair.pair_id]
        second_ids = [second.snapshot().pair.pair_id]
        for _ in range(10):
            first.strike("pair")
            second.strike("pair")
            first_ids.append(first.snapshot().pair.pair_id)
            second_ids.append(second.snapshot().pair.pair_id)
        self.assertNotEqual(first_ids, second_ids)

    def test_product_history_does_not_starve_validation_pair_coverage(self) -> None:
        product = make_session(self.base, session_id="history-product")
        for _ in range(PRODUCT_CAP):
            product.strike("right")
        store = EventStore(self.base)
        validation = ColdOpenSession(
            session_id="history-validation",
            profile=PROFILE_VALIDATION,
            store=store,
            land_dir=self.base,
            history=store.load_all(),
        )
        for _ in range(validation.snapshot().slots_total):
            validation.strike("left")

        self.assertIsNone(validation.snapshot().voided_reason)
        self.assertTrue(
            any(
                event.type is EventType.SESSION_VALIDATED
                for event in validation.log.events
            )
        )

    def test_missing_probe_closes_as_voided_session(self) -> None:
        for _ in range(15):
            self.session.strike("pair")
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot.voided_reason, "probe_missing")
        self.assertEqual(snapshot.landing.status, "voided")
        self.assertTrue(
            any(
                e.type is EventType.SESSION_VOIDED
                for e in self.session.log.events
            )
        )


class RecheckSessionTest(unittest.TestCase):
    """4막 경량 재심 - 큐 선두 축을 5긋기로 재시험하고 last_review를 갱신한다."""

    def _land_product(self, base: Path) -> dict:
        session = make_session(base, session_id="prod-r")
        for _ in range(PRODUCT_CAP):
            session.strike("left")
        manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
        manifest["recheck_queue"] = [
            {
                "axis": "scope_adherence",
                "class": "unstable",
                "rule_id": "test-rule",
            }
        ]
        return manifest

    def test_recheck_serves_queued_axes_and_relands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = self._land_product(base)
            queue_axes = [
                str(entry.get("axis"))
                for entry in manifest.get("recheck_queue", ())
                if entry.get("axis")
            ]
            store = EventStore(base)
            history = store.load_all()
            session = ColdOpenSession(
                session_id="recheck-1",
                profile=PROFILE_RECHECK,
                store=store,
                land_dir=base,
                history=history,
                recheck_manifest=manifest,
            )
            first = session.snapshot()
            self.assertEqual(first.slots_total, 5)
            if queue_axes:
                self.assertIn(first.pair.axis, queue_axes)

            for _ in range(5):
                session.strike("right")
            snapshot = session.snapshot()
            self.assertTrue(snapshot.session_complete)
            self.assertEqual(snapshot.landing.status, "landed")

            relanded = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
            self.assertGreaterEqual(
                relanded["last_review"], manifest["last_review"]
            )
            self.assertEqual(relanded.get("session_id"), "recheck-1")

    def test_recheck_opening_uses_the_recheck_session_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            manifest = self._land_product(base)
            session = ColdOpenSession(
                session_id="recheck-2",
                profile=PROFILE_RECHECK,
                store=EventStore(base),
                land_dir=base,
                history=EventStore(base).load_all(),
                recheck_manifest=manifest,
            )
            opening = session.log.events[0]
            self.assertIs(opening.type, EventType.SESSION_START)
            self.assertEqual(opening.payload.get("session_kind"), "recheck")

    def test_empty_recheck_queue_is_rejected(self) -> None:
        with self.assertRaises(SchemaViolation):
            ColdOpenSession(
                session_id="recheck-empty",
                profile=PROFILE_RECHECK,
                recheck_manifest={"recheck_queue": []},
            )


class LandingBlockTest(unittest.TestCase):
    """수기 편집 감지 - silent overwrite 대신 착지 차단."""

    def test_manual_edit_blocks_the_next_landing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = make_session(base, session_id="prod-a")
            for _ in range(PRODUCT_CAP):
                first.strike("left")
            self.assertEqual(first.snapshot().landing.status, "landed")

            target = base / POPPER_MD
            target.write_text(
                target.read_text(encoding="utf-8") + "\n- 수기 추가 룰\n",
                encoding="utf-8",
            )

            second = make_session(base, session_id="prod-b")
            for _ in range(PRODUCT_CAP):
                second.strike("left")
            landing = second.snapshot().landing
            self.assertEqual(landing.status, "blocked")
            self.assertTrue(landing.detections)
            self.assertEqual(landing.detections[0]["reason"], "manual_edit")


class ServerEndpointTest(unittest.TestCase):
    """서버 계약 - /undo 채널과 닫힌 세션의 409."""

    POLL_INTERVAL_SECONDS = 0.02

    def launch(
        self,
        session: ColdOpenSession,
        *,
        shutdown_on_complete: bool = True,
    ):
        server = build_server(
            session=session,
            shutdown_on_complete=shutdown_on_complete,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            args=(self.POLL_INTERVAL_SECONDS,),
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, REQUEST_TIMEOUT_SECONDS)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            server.url.rstrip("/") + path,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def test_undo_endpoint_is_an_explicit_recovery_channel(self) -> None:
        session = ColdOpenSession(session_id="srv-undo")
        server = self.launch(session)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(server, "/undo", {})
        self.assertEqual(caught.exception.code, 409)

        _, struck = self.post(server, "/strike", {"target": "left"})
        self.assertTrue(struck["undoable"])
        status, undone = self.post(server, "/undo", {})
        self.assertEqual(status, 200)
        self.assertEqual(undone["remaining_combinations"], INITIAL_COMBINATIONS)

    def test_stale_presentation_is_rejected_without_a_strike(self) -> None:
        session = ColdOpenSession(session_id="srv-stale")
        server = self.launch(session)
        before = session.snapshot()

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(
                server,
                "/strike",
                {
                    "target": "left",
                    "pair_id": "not-the-presented-pair",
                    "slot": before.slots_used + 1,
                },
            )
        self.assertEqual(caught.exception.code, 409)
        detail = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(detail["error"], "stale_presentation")
        self.assertEqual(session.snapshot().strike_count, 0)

    def test_closed_session_rejects_strikes_with_409(self) -> None:
        session = ColdOpenSession(session_id="srv-cap")
        server = self.launch(session, shutdown_on_complete=False)
        for _ in range(PRODUCT_CAP):
            status, body = self.post(server, "/strike", {"target": "pair"})
            self.assertEqual(status, 200)
        self.assertTrue(body["session_complete"])

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(server, "/strike", {"target": "left"})
        self.assertEqual(caught.exception.code, 409)
        detail = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(detail["error"], "session_complete")

    def test_completed_session_stops_the_background_server(self) -> None:
        session = ColdOpenSession(session_id="srv-auto-stop")
        server = build_server(session=session)
        thread = threading.Thread(
            target=server.serve_forever,
            args=(self.POLL_INTERVAL_SECONDS,),
            daemon=True,
        )
        thread.start()
        self.addCleanup(server.server_close)
        for _ in range(PRODUCT_CAP):
            status, body = self.post(server, "/strike", {"target": "pair"})
            self.assertEqual(status, 200)
        self.assertTrue(body["session_complete"])

        thread.join(REQUEST_TIMEOUT_SECONDS)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.shutdown_requested.is_set())

    def test_server_refuses_non_loopback_bindings(self) -> None:
        with self.assertRaisesRegex(ValueError, "루프백"):
            build_server(host="0.0.0.0")

    def test_server_emits_browser_security_headers(self) -> None:
        server = self.launch(ColdOpenSession(session_id="srv-headers"))
        with urllib.request.urlopen(server.url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_server_rejects_untrusted_host_header(self) -> None:
        server = self.launch(ColdOpenSession(session_id="srv-host"))
        connection = http.client.HTTPConnection(
            server.server_address[0],
            server.server_address[1],
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self.addCleanup(connection.close)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 421)
        self.assertEqual(json.loads(response.read())["error"], "untrusted_origin")


if __name__ == "__main__":
    unittest.main()
