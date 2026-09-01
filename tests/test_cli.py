"""CLI 경계 검증 - 파서, 재착지, 충돌 opt-in, 상태 요약.

사용자 홈 디렉토리를 건드리는 경로(enable --grant)는 여기서 실행하지 않는다 -
그 계약은 tests/test_ownership_writer.py가 writer 모듈 수준에서 못 박는다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from xout.cli import (
    CONSENT_FILE,
    MANUAL_RULES_FILE,
    _conflicts_for,
    _ensure_seal_event,
    _load_consent,
    build_parser,
    main,
)
from xout.events import Event, EventType
from xout.fixtures import FIXTURES_DIR
from xout.judgment import fold_judgment
from xout.compiler import MANIFEST_JSON, XOUT_MD, compile_rules
from xout.session import DEFAULT_PREREG_PATH
from xout.store import EventStore
from xout.state import ColdOpenSession

PRODUCT_CAP = 15


def land_product_session(base: Path, session_id: str = "cli-prod") -> None:
    session = ColdOpenSession(
        session_id=session_id, store=EventStore(base), land_dir=base
    )
    for _ in range(PRODUCT_CAP):
        session.strike("left")


class ParserTest(unittest.TestCase):
    def test_every_subcommand_parses(self) -> None:
        parser = build_parser()
        for argv in (
            ["open"],
            ["resume"],
            ["validate"],
            ["recheck", "--budget", "6"],
            ["status"],
            ["sessions"],
            ["doctor"],
            ["export", "--format", "json"],
            ["data", "backup", "backup.zip"],
            ["data", "inspect", "backup.zip"],
            ["version"],
            ["update"],
            ["land", "--acknowledge-mismatch"],
            ["enable", "--grant"],
            ["rollback"],
            ["optin", "rule-1"],
            ["acknowledge", "--actor", "hj"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(callable(args.func))


class StatusCommandTest(unittest.TestCase):
    def test_status_runs_with_and_without_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["status", "--base-dir", tmp]), 0)
            land_product_session(Path(tmp))
            self.assertEqual(main(["status", "--base-dir", tmp]), 0)


class LandCommandTest(unittest.TestCase):
    def test_land_refuses_an_empty_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["land", "--base-dir", tmp]), 1)

    def test_manual_edit_blocks_and_acknowledge_relands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            land_product_session(base)

            target = base / XOUT_MD
            target.write_text(
                target.read_text(encoding="utf-8") + "\n- 수기 추가\n",
                encoding="utf-8",
            )
            self.assertEqual(main(["land", "--base-dir", tmp]), 1)

            self.assertEqual(
                main(["land", "--base-dir", tmp, "--acknowledge-mismatch"]), 0
            )
            manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
            records = manifest.get("hash_mismatch_records", ())
            self.assertTrue(records, "감지 기록이 manifest에 남지 않았다")
            self.assertEqual(records[0]["reason"], "manual_edit")


class ConflictOptInTest(unittest.TestCase):
    """수기 룰은 opt-in 전에는 반증 대상이 아니다(default-in 금지)."""

    def _write_manual_rule(self, base: Path, axis: str, value: str) -> None:
        (base / MANUAL_RULES_FILE).write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "rule_id": "manual-1",
                            "axis": axis,
                            "value": value,
                            "text": "수기 룰 문장",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_conflict_surfaces_only_after_optin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            land_product_session(base)
            rules = compile_rules(EventStore(base).load_all())
            landed = {rule.axis: rule.value for rule in rules}
            axis = "autonomy"
            other = next(
                value
                for value in ("ask_first", "propose_then_act", "act_then_report")
                if value != landed[axis]
            )
            self._write_manual_rule(base, axis, other)

            self.assertEqual(_conflicts_for(base)(rules), ())

            self.assertEqual(main(["optin", "manual-1", "--base-dir", tmp]), 0)
            self.assertTrue((base / CONSENT_FILE).exists())
            self.assertEqual(len(_load_consent(base)), 1)

            rows = _conflicts_for(base)(rules)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["axis"], axis)
            sides = {side["side"]: side for side in rows[0]["sides"]}
            self.assertEqual(sides["manual"]["value"], other)
            self.assertEqual(sides["compiled"]["value"], landed[axis])

            self.assertEqual(main(["land", "--base-dir", tmp]), 0)
            manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["conflicts"]), 1)
            conflict_entries = [
                entry
                for entry in manifest["recheck_queue"]
                if entry.get("class") == "conflict"
            ]
            self.assertEqual(len(conflict_entries), 1)

    def test_corrupt_consent_lines_are_skipped_without_hiding_valid_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            valid = {
                "kind": "manual_rule_opted_in",
                "subject": "manual-1",
            }
            (base / CONSENT_FILE).write_text(
                json.dumps(valid) + "\n"
                '{"kind":"future_kind","subject":"ignored"}\n'
                '{"kind":"manual_rule_opted_in","subject"\n',
                encoding="utf-8",
            )
            ledger = _load_consent(base)
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger.records[0].subject, "manual-1")


class SealEventTest(unittest.TestCase):
    """검증 세션 이전에 봉인 기준이 스트림에 정확히 하나 적재된다."""

    def test_seal_event_is_created_once_with_document_owned_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(Path(tmp))
            _ensure_seal_event(store)
            _ensure_seal_event(store)
            seals = [
                e
                for e in store.load_all()
                if getattr(e, "type", None) is EventType.PREREG_SEALED
            ]
            self.assertEqual(len(seals), 1)
            payload = seals[0].payload
            self.assertTrue(payload["digest"])
            self.assertIsInstance(payload["required_valid_sessions"], int)
            self.assertIsInstance(payload["required_discriminative_instances"], int)

            state = fold_judgment(store.load_all())
            self.assertTrue(state.catalog_frozen, "봉인 기준이 판정 fold에 잡히지 않았다")

    def test_packaged_data_copies_match_the_source_artifacts(self) -> None:
        root = Path(__file__).resolve().parent.parent
        packaged_fixtures = root / "xout" / "_data" / "fixtures"
        for source in (root / "fixtures").glob("*.json"):
            self.assertEqual(
                source.read_bytes(),
                (packaged_fixtures / source.name).read_bytes(),
            )
        self.assertEqual(
            (root / "docs" / "prereg" / "prereg_sealed.json").read_bytes(),
            (root / "xout" / "_data" / "prereg" / "prereg_sealed.txt").read_bytes(),
        )
        self.assertEqual(
            (root / "ground_truth" / "ground_truth.jsonl").read_bytes(),
            (
                root / "xout" / "_data" / "ground_truth" / "ground_truth.txt"
            ).read_bytes(),
        )
        self.assertEqual(
            (root / "ground_truth" / "ground_truth.sha256").read_bytes(),
            (
                root / "xout" / "_data" / "ground_truth" / "ground_truth.sha256"
            ).read_bytes(),
        )
        self.assertTrue(FIXTURES_DIR.is_dir())
        self.assertTrue(DEFAULT_PREREG_PATH.is_file())


class SessionAdmissionTest(unittest.TestCase):
    def test_recent_validation_session_blocks_another_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(Path(tmp))
            store.append(
                Event(
                    type=EventType.SESSION_VALIDATED,
                    session_id="recent-validation",
                    at=datetime.now(timezone.utc).isoformat(),
                    payload={"profile": "validation"},
                )
            )
            self.assertEqual(main(["validate", "--base-dir", tmp]), 1)

    def test_empty_recheck_queue_opens_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / MANIFEST_JSON).write_text(
                json.dumps({"recheck_queue": []}),
                encoding="utf-8",
            )
            self.assertEqual(main(["recheck", "--base-dir", tmp]), 0)
            self.assertEqual(EventStore(base).session_ids(), ())

    def test_recent_voided_validation_also_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(Path(tmp))
            store.append(
                Event(
                    type=EventType.SESSION_START,
                    session_id="voided-validation",
                    payload={"profile": "validation"},
                )
            )
            store.append(
                Event(
                    type=EventType.SESSION_VOIDED,
                    session_id="voided-validation",
                    at=datetime.now(timezone.utc).isoformat(),
                    payload={"reason": "probe_missing"},
                )
            )
            self.assertEqual(main(["validate", "--base-dir", tmp]), 1)


class AcknowledgeCommandTest(unittest.TestCase):
    def test_acknowledge_refuses_before_condition_met(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                main(["acknowledge", "--actor", "hj", "--base-dir", tmp]), 1
            )


if __name__ == "__main__":
    unittest.main()
