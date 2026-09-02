"""AC10 - 해시 봉인된 사전등록 문서가 판정 영향 수치를 독점하는지 검증한다."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREREG_PATH = REPO_ROOT / "docs" / "prereg" / "prereg_sealed.json"

SCANNED_SUFFIXES = frozenset(
    {".py", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".env", ".js", ".ts"}
)

# 스캔 제외 - docs/는 봉인 문서가 사는 곳, tests/는 그 값을 검증해야 하는 코드,
# seed.yaml은 이 프로젝트가 런타임에 읽지 않는 상위 Seed 규격서다.
NON_RUNTIME_PREFIXES = ("build/", "docs/", "tests/", "video/")
NON_RUNTIME_FILES = frozenset({"seed.yaml"})

REQUIRED_SECTIONS = (
    "frozen_parameters",
    "session_slot_layout",
    "threats_to_validity",
    "three_culprit_exclusion_procedure",
    "sacrifice_axis_rule",
    "probe_selection_rule",
    "consequence_ladder",
    "refutation_condition",
    "catalog_revision_policy",
    "scoring_spec",
    "deadline",
    "limitations",
)

EXPECTED_FROZEN_VALUES = {
    "strike_cap_per_session": 15,
    "validation_session_count_n_val": 2,
    "fixture_scenario_count_s_scn": 1,
    "discriminated_axis_floor_per_session": 5,
    "cumulative_discriminative_instance_floor": 6,
    "label_reversal_void_threshold_percent": 30,
}


def load_payload() -> dict:
    return json.loads(PREREG_PATH.read_text(encoding="utf-8"))


def canonical_bytes(document: dict) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def runtime_files() -> list[Path]:
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(".") or "/." in rel:
            continue
        if rel in NON_RUNTIME_FILES or rel.startswith(NON_RUNTIME_PREFIXES):
            continue
        found.append(path)
    return found


class ArtifactExistsTest(unittest.TestCase):
    """사전등록 문서가 최상위 불변 아티팩트로 실재한다."""

    def test_sealed_prereg_file_exists(self) -> None:
        self.assertTrue(PREREG_PATH.is_file())

    def test_payload_has_document_and_seal(self) -> None:
        payload = load_payload()
        self.assertEqual(payload["artifact"], "popper_preregistration")
        self.assertIn("document", payload)
        self.assertIn("seal", payload)


class SealIntegrityTest(unittest.TestCase):
    """라벨 봉인과 동일한 해싱 기계로 봉인되어 재계산이 일치한다."""

    def setUp(self) -> None:
        self.payload = load_payload()

    def test_seal_declares_sha256_over_document(self) -> None:
        seal = self.payload["seal"]
        self.assertEqual(seal["algorithm"], "sha256")
        self.assertEqual(seal["sealed_field"], "document")
        self.assertEqual(seal["same_machinery_as"], "ground_truth_label_seal")
        self.assertRegex(seal["digest"], r"^[0-9a-f]{64}$")

    def test_recomputed_digest_matches_stored_digest(self) -> None:
        recomputed = hashlib.sha256(canonical_bytes(self.payload["document"])).hexdigest()
        self.assertEqual(recomputed, self.payload["seal"]["digest"])

    def test_tampering_breaks_the_seal(self) -> None:
        tampered = copy.deepcopy(self.payload["document"])
        tampered["frozen_parameters"]["strike_cap_per_session"]["value"] = 14
        recomputed = hashlib.sha256(canonical_bytes(tampered)).hexdigest()
        self.assertNotEqual(recomputed, self.payload["seal"]["digest"])


class SealedBeforeSessionOneTest(unittest.TestCase):
    """세션 1 이전 봉인 - 관측 데이터 0건 상태에서 동결됐다."""

    def setUp(self) -> None:
        self.document = load_payload()["document"]

    def test_sealed_before_first_session(self) -> None:
        self.assertIs(self.document["sealed_before_session_1"], True)
        self.assertEqual(self.document["sessions_observed_at_seal_time"], 0)

    def test_deadline_forbids_session_one_before_seal(self) -> None:
        deadline = self.document["deadline"]
        self.assertEqual(
            deadline["prereg_sealed_by"], deadline["session_1_not_before"]
        )
        self.assertEqual(self.document["sealed_at"], deadline["prereg_sealed_by"])

    def test_catalog_and_metric_versions_are_stamped(self) -> None:
        self.assertEqual(self.document["catalog_version"], "v1")
        self.assertEqual(self.document["metric_spec_version"], "core-metric-v1")


class RequiredSectionsTest(unittest.TestCase):
    """AC가 요구한 12개 요소가 모두 실려 있다."""

    def setUp(self) -> None:
        self.document = load_payload()["document"]

    def test_every_required_section_present(self) -> None:
        for section in REQUIRED_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, self.document)
                self.assertTrue(self.document[section])

    def test_frozen_numbers_exact(self) -> None:
        frozen = self.document["frozen_parameters"]
        for key, expected in EXPECTED_FROZEN_VALUES.items():
            with self.subTest(parameter=key):
                self.assertEqual(frozen[key]["value"], expected)

    def test_s_scn_reuse_and_memory_effect_is_fourth_threat(self) -> None:
        threats = self.document["threats_to_validity"]
        self.assertEqual(len(threats), 4)
        fourth = threats[3]
        self.assertEqual(fourth["id"], "t4_memory")
        self.assertEqual(fourth["inter_session_gap_hours_min"], 48)
        self.assertGreaterEqual(len(fourth["countermeasure"]), 4)

    def test_three_culprit_exclusion_covers_all_three(self) -> None:
        procedure = self.document["three_culprit_exclusion_procedure"]
        for defense in ("fixture_defense", "caprice_defense", "label_defense"):
            with self.subTest(defense=defense):
                self.assertIn("admissible_only_if", procedure[defense])
        self.assertIn(
            "label_reversal_void_threshold_percent",
            procedure["label_defense"]["hard_limit"],
        )

    def test_sacrifice_rule_pins_two_mined_prior_axes(self) -> None:
        rule = self.document["sacrifice_axis_rule"]
        self.assertEqual(rule["count"], 2)
        self.assertEqual(len(rule["fixed_sacrificed_axes"]), 2)
        self.assertIn("재량이 아니라 사전 고정", rule["statement"])

    def test_probe_selection_is_deterministic_and_terminal(self) -> None:
        rule = self.document["probe_selection_rule"]
        self.assertEqual(rule["fixed_slots"], [9, 13])
        self.assertIs(rule["properties"]["mirrored"], True)
        self.assertIs(rule["properties"]["inert_in_compile_fold"], True)
        self.assertIn("재추첨 불가", rule["properties"]["terminal"])
        self.assertIn("이벤트 prefix의 순수 함수", rule["statement"])

    def test_slot_layout_splits_validation_cap(self) -> None:
        layout = self.document["session_slot_layout"]
        self.assertEqual(layout["product"]["probe_slots"], [])
        self.assertEqual(layout["product"]["discriminative_slots"], 15)
        self.assertEqual(layout["validation"]["discriminative_slots"], 13)
        self.assertEqual(layout["validation"]["probe_slots"], [9, 13])
        self.assertEqual(
            layout["validation"]["discriminative_slots"]
            + len(layout["validation"]["probe_slots"]),
            EXPECTED_FROZEN_VALUES["strike_cap_per_session"],
        )

    def test_consequence_ladder_is_ordered_and_gated(self) -> None:
        ladder = self.document["consequence_ladder"]
        rungs = ladder["rungs"]
        self.assertEqual([rung["order"] for rung in rungs], [1, 2, 3, 4])
        self.assertEqual(
            [rung["verdict"] for rung in rungs],
            ["core_refutation", "core_refutation", "demotion", "survival"],
        )
        condition = self.document["refutation_condition"]
        self.assertEqual(condition["machine_event"], "refutation_condition_met")
        self.assertEqual(condition["human_gate"], "refutation_acknowledged")

    def test_catalog_revision_capped_at_one_with_crosswalk(self) -> None:
        policy = self.document["catalog_revision_policy"]
        self.assertEqual(policy["max_revisions"], 1)
        self.assertIs(policy["crosswalk_required"], True)
        self.assertIn("리셋 금지", policy["counter_carryover"])

    def test_limitations_section_is_substantive(self) -> None:
        self.assertGreaterEqual(len(self.document["limitations"]), 5)


class JudgmentNumbersAbsentFromCodeTest(unittest.TestCase):
    """판정 영향 수치는 코드/config에 존재하지 않는다."""

    def setUp(self) -> None:
        self.guard = load_payload()["document"]["code_scan_guard"]
        self.targets = runtime_files()

    def test_scan_actually_covers_runtime_code(self) -> None:
        scanned = {p.relative_to(REPO_ROOT).as_posix() for p in self.targets}
        self.assertIn("xout/events.py", scanned)
        self.assertNotIn("seed.yaml", scanned)
        self.assertFalse({s for s in scanned if s.startswith(NON_RUNTIME_PREFIXES)})

    def test_frozen_parameter_keys_absent_from_runtime(self) -> None:
        for path in self.targets:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for key in EXPECTED_FROZEN_VALUES:
                with self.subTest(path=path.name, parameter=key):
                    self.assertNotIn(key, text)

    def test_no_judgment_lexeme_carries_a_numeric_literal(self) -> None:
        pattern = re.compile(
            r"\b(" + "|".join(self.guard["forbidden_parameter_lexemes"]) + r")\b",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in self.targets:
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                if pattern.search(line) and re.search(r"\d", line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
