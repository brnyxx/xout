"""AC12 - 블라인드 복원 채점이 봉인 정답지 기반 축별 5분류 정성 리포트를 내는지 검증한다.

- 5분류(정복원/오복원/미판별/unmappable/교정) 각각 최소 1케이스
- 교정 셀은 core 지표 분모에서 제외 + conflict_id로 별도 보고
- 정확도 분모는 매핑축만, 커버리지는 별도 지표
- LLM 초안 vs 본인 확정 불일치율 기록
- out-of-catalog 문장은 원문째 JSONL 반증 로그로 보존
- 정답지 파일 해시 불일치 시 채점 거부(봉인 위반)
- 코드 어디에도 70% pass/fail 판정이 없다(정적 검사)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from xout.conflict import CORE_DENOMINATOR_CELLS, core_denominator
from xout.counter import DEFAULT_CATALOG
from xout.scoring import (
    CELL_CORRECTED,
    CELL_MIS_RESTORED,
    CELL_RESTORED,
    CELL_UNDISCRIMINATED,
    CELL_UNMAPPABLE,
    OUT_OF_CATALOG_RECORD,
    ScoringViolation,
    SealViolation,
    draft_digest,
    ground_truth_file_hash,
    load_ground_truth,
    score_restoration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCORING_SOURCE_PATH = REPO_ROOT / "xout" / "scoring.py"
EXAMPLE_GROUND_TRUTH = REPO_ROOT / "ground_truth" / "ground_truth.jsonl"

NOW = "2026-08-28T12:00:00+00:00"
OUT_OF_CATALOG_TEXT = "커밋 메시지와 PR 본문에 이모지를 쓰지 않는다."

# (label_id, rule_text, draft_axis, draft_value, confirmed_axis, confirmed_value)
LABEL_SPECS = (
    ("gt-001", "모든 응답과 설명은 반드시 한국어로 작성한다.",
     "response_language", "korean", "response_language", "korean"),
    ("gt-002", "결론과 코드만 제시하고 장황한 부연 설명은 생략한다.",
     "verbosity", "terse", "verbosity", "terse"),
    ("gt-003", "코드를 고치기 전에는 계획을 먼저 보여주고 승인을 받는다.",
     "autonomy", "ask_first", "autonomy", "ask_first"),
    ("gt-004", "커밋 메시지는 conventional prefix로 시작한다.",
     "commit_style", "conventional", "commit_style", "conventional"),
    ("gt-005", OUT_OF_CATALOG_TEXT,
     "commit_style", "narrative", None, None),
)

RESTORED_RULES = (
    {"axis": "response_language", "value": "korean",
     "corroboration_grade": "discriminated", "catalog_version": "v1"},
    {"axis": "verbosity", "value": "balanced",
     "corroboration_grade": "discriminated", "catalog_version": "v1"},
    {"axis": "autonomy", "value": "ask_first",
     "corroboration_grade": "untested", "catalog_version": "v1"},
    {"axis": "commit_style", "value": "no_auto_commit",
     "corroboration_grade": "discriminated", "catalog_version": "v1"},
)

CORRECTIONS = (
    {"axis": "commit_style", "cell": CELL_CORRECTED,
     "conflict_id": "commit_style::user-commit::v1", "in_core_denominator": False},
)


def write_ground_truth(target: Path, specs=LABEL_SPECS, catalog_version: str = "v1") -> Path:
    """봉인 프로토콜 산출물 형태의 정답지 JSONL을 만든다(초안 봉인 해시 정합 보장)."""
    draft_rows = [
        {"label_id": lid, "rule_text": text, "axis": d_axis, "value": d_value}
        for lid, text, d_axis, d_value, _, _ in specs
    ]
    digest = draft_digest(draft_rows)
    seal = {
        "record": "seal",
        "artifact": "popper_ground_truth",
        "protocol": ["타 모델 계열 초안", "세션 전 해시 봉인", "세션 로그 동결", "개봉 + 본인 검수"],
        "algorithm": "sha256",
        "sealed_field": "llm_draft_labels",
        "same_machinery_as": "prereg_document_seal",
        "catalog_version": catalog_version,
        "draft_model_family": "non-claude-family",
        "draft_sealed_at": "2026-08-28T00:00:00+00:00",
        "draft_digest": digest,
        "session_log_frozen_at": "2026-08-28T02:00:00+00:00",
        "session_log_frozen_hash": "sha256:" + "0" * 64,
        "opened_at": "2026-08-28T03:00:00+00:00",
        "reviewed_by": "user_self",
    }
    lines = [json.dumps(seal, ensure_ascii=False, sort_keys=True)]
    for lid, text, d_axis, d_value, c_axis, c_value in specs:
        lines.append(
            json.dumps(
                {
                    "record": "label",
                    "label_id": lid,
                    "rule_text": text,
                    "axis": c_axis,
                    "value": c_value,
                    "catalog_version": catalog_version,
                    "seal_ref": "sha256:" + digest,
                    "llm_draft": {"axis": d_axis, "value": d_value},
                    "confirmed": {"axis": c_axis, "value": c_value},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


@pytest.fixture
def ground_truth_path(tmp_path: Path) -> Path:
    return write_ground_truth(tmp_path / "ground_truth.jsonl")


@pytest.fixture
def report(ground_truth_path: Path, tmp_path: Path):
    gt = load_ground_truth(
        ground_truth_path, expected_file_hash=ground_truth_file_hash(ground_truth_path)
    )
    return score_restoration(
        gt,
        RESTORED_RULES,
        corrections=CORRECTIONS,
        refutation_log_path=tmp_path / "refutation_log.jsonl",
        now=NOW,
    )


class TestFiveCellClassification:
    """축별 5분류 각각이 최소 1케이스씩 채점된다."""

    def test_each_of_five_cells_present(self, report) -> None:
        counts = report.cell_counts()
        assert counts == {
            CELL_RESTORED: 1,
            CELL_MIS_RESTORED: 1,
            CELL_UNDISCRIMINATED: 1,
            CELL_UNMAPPABLE: 1,
            CELL_CORRECTED: 1,
        }

    def test_cells_carry_axis_and_values(self, report) -> None:
        by_axis = {row["axis"]: row for row in report.cells}
        assert by_axis["response_language"]["cell"] == CELL_RESTORED
        assert by_axis["verbosity"]["cell"] == CELL_MIS_RESTORED
        assert by_axis["verbosity"]["handwritten_value"] == "terse"
        assert by_axis["verbosity"]["restored_value"] == "balanced"
        assert by_axis["autonomy"]["cell"] == CELL_UNDISCRIMINATED
        assert by_axis["commit_style"]["cell"] == CELL_CORRECTED
        assert by_axis[None]["cell"] == CELL_UNMAPPABLE

    def test_report_dict_always_lists_all_five_cells(self, report) -> None:
        payload = report.to_dict()
        assert set(payload["cell_counts"]) == {
            CELL_RESTORED,
            CELL_MIS_RESTORED,
            CELL_UNDISCRIMINATED,
            CELL_UNMAPPABLE,
            CELL_CORRECTED,
        }


class TestCorrectedCellExclusion:
    """교정 셀(수기값 != 판별시험 생존값)은 core 분모에서 빠지고 별도 보고된다."""

    def test_core_denominator_excludes_corrected(self, report) -> None:
        assert report.core["denominator"] == 2
        assert report.core["restored"] == 1
        assert report.core["mis_restored"] == 1
        assert report.core["corrected_excluded"] == 1
        core_cells = core_denominator(report.cells)
        assert {row["cell"] for row in core_cells} == set(CORE_DENOMINATOR_CELLS)
        assert all(row["cell"] != CELL_CORRECTED for row in core_cells)

    def test_corrected_reported_separately_with_conflict_id_join(self, report) -> None:
        assert len(report.corrected) == 1
        row = report.corrected[0]
        assert row["axis"] == "commit_style"
        assert row["conflict_id"] == "commit_style::user-commit::v1"
        assert row["in_core_denominator"] is False
        assert row["handwritten_value"] == "conventional"
        assert row["restored_value"] == "no_auto_commit"

    def test_correction_input_requires_conflict_id(self, ground_truth_path: Path) -> None:
        gt = load_ground_truth(
            ground_truth_path,
            expected_file_hash=ground_truth_file_hash(ground_truth_path),
        )
        with pytest.raises(ScoringViolation):
            score_restoration(
                gt, RESTORED_RULES, corrections=({"axis": "commit_style"},), now=NOW
            )


class TestAccuracyAndCoverage:
    """정확도 분모는 매핑축만이고 커버리지는 별도 지표다."""

    def test_accuracy_denominator_is_mapped_axes_only(self, report) -> None:
        # 라벨 5개 중 unmappable 1개와 교정 1개는 분모 밖 - 분모는 3(매핑축만)
        assert report.accuracy["denominator"] == 3
        assert report.accuracy["restored"] == 1
        assert report.accuracy["ratio"] == pytest.approx(1 / 3)

    def test_accuracy_denominator_never_counts_unmappable(self, report) -> None:
        unmappable = [row for row in report.cells if row["cell"] == CELL_UNMAPPABLE]
        assert len(unmappable) == 1
        assert report.accuracy["denominator"] + len(unmappable) + len(report.corrected) == len(
            report.cells
        )

    def test_coverage_is_a_separate_metric(self, report) -> None:
        assert report.coverage["mapped_axes"] == [
            "autonomy",
            "commit_style",
            "response_language",
            "verbosity",
        ]
        assert report.coverage["mapped_axis_total"] == 4
        assert report.coverage["catalog_axis_total"] == len(DEFAULT_CATALOG)
        assert report.coverage["ratio"] == pytest.approx(4 / len(DEFAULT_CATALOG))
        # 커버리지와 정확도는 서로 다른 분모를 쓰는 별개 지표다
        assert report.coverage["ratio"] != report.accuracy["ratio"]


class TestLlmReviewDisagreement:
    """LLM 초안 라벨 vs 본인 확정 라벨 불일치율이 별도 기록된다."""

    def test_disagreement_ratio_recorded(self, report) -> None:
        record = report.llm_review_disagreement
        assert record["total_labels"] == 5
        assert record["disagreements"] == 1
        assert record["ratio"] == pytest.approx(1 / 5)

    def test_disagreement_rows_carry_both_labels(self, report) -> None:
        (row,) = report.llm_review_disagreement["rows"]
        assert row["label_id"] == "gt-005"
        assert row["llm_draft"] == {"axis": "commit_style", "value": "narrative"}
        assert row["confirmed"] == {"axis": None, "value": None}


class TestOutOfCatalogRefutationLog:
    """out-of-catalog 문장은 채점 제외되지만 원문째 JSONL 반증 로그로 보존된다."""

    def test_refutation_log_preserves_verbatim_text(self, report) -> None:
        log_path = Path(report.refutation_log_path)
        lines = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        entry = lines[0]
        assert entry["record"] == OUT_OF_CATALOG_RECORD
        assert entry["rule_text"] == OUT_OF_CATALOG_TEXT
        assert entry["label_id"] == "gt-005"
        assert entry["catalog_version"] == "v1"
        assert entry["ground_truth_hash"].startswith("sha256:")

    def test_report_keeps_out_of_catalog_rows(self, report) -> None:
        assert len(report.out_of_catalog) == 1
        assert report.out_of_catalog[0]["rule_text"] == OUT_OF_CATALOG_TEXT

    def test_log_is_append_only_across_runs(self, ground_truth_path: Path, tmp_path: Path) -> None:
        gt = load_ground_truth(
            ground_truth_path,
            expected_file_hash=ground_truth_file_hash(ground_truth_path),
        )
        log = tmp_path / "refutations.jsonl"
        for _ in range(2):
            score_restoration(
                gt, RESTORED_RULES, corrections=CORRECTIONS,
                refutation_log_path=log, now=NOW,
            )
        lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 2


class TestSealRefusal:
    """정답지 파일 해시가 기대 해시와 다르면 채점을 거부한다(봉인 위반)."""

    def test_wrong_expected_hash_refuses_loading(self, ground_truth_path: Path) -> None:
        with pytest.raises(SealViolation):
            load_ground_truth(
                ground_truth_path, expected_file_hash="sha256:" + "f" * 64
            )

    def test_tampered_file_breaks_the_seal(self, ground_truth_path: Path) -> None:
        expected = ground_truth_file_hash(ground_truth_path)
        body = ground_truth_path.read_text(encoding="utf-8")
        ground_truth_path.write_text(
            body.replace('"value": "terse"', '"value": "balanced"'), encoding="utf-8"
        )
        with pytest.raises(SealViolation):
            load_ground_truth(ground_truth_path, expected_file_hash=expected)

    def test_draft_label_tamper_detected_by_recomputed_digest(
        self, ground_truth_path: Path
    ) -> None:
        # 파일 해시를 위조본 기준으로 다시 계산해 파일 게이트를 통과시켜도,
        # 초안 봉인 해시 재계산이 어긋나 봉인 위반으로 거부된다.
        body = ground_truth_path.read_text(encoding="utf-8")
        tampered = body.replace(
            '"llm_draft": {"axis": "verbosity", "value": "terse"}',
            '"llm_draft": {"axis": "verbosity", "value": "balanced"}',
        )
        assert tampered != body
        ground_truth_path.write_text(tampered, encoding="utf-8")
        with pytest.raises(SealViolation):
            load_ground_truth(
                ground_truth_path,
                expected_file_hash=ground_truth_file_hash(ground_truth_path),
            )

    def test_ground_truth_is_read_only_for_scoring(
        self, ground_truth_path: Path, tmp_path: Path
    ) -> None:
        before = ground_truth_path.read_bytes()
        gt = load_ground_truth(
            ground_truth_path, expected_file_hash=ground_truth_file_hash(ground_truth_path)
        )
        score_restoration(
            gt, RESTORED_RULES, corrections=CORRECTIONS,
            refutation_log_path=tmp_path / "log.jsonl", now=NOW,
        )
        assert ground_truth_path.read_bytes() == before


class TestVersionParameterization:
    """리포트는 (catalog_version, metric_spec_version)으로 파라미터화된다."""

    def test_versions_are_stamped_in_report(self, report) -> None:
        assert report.catalog_version == "v1"
        assert report.metric_spec_version
        payload = report.to_dict()
        assert payload["catalog_version"] == "v1"
        assert payload["metric_spec_version"] == report.metric_spec_version

    def test_catalog_version_mismatch_refused(self, ground_truth_path: Path) -> None:
        gt = load_ground_truth(
            ground_truth_path,
            expected_file_hash=ground_truth_file_hash(ground_truth_path),
        )
        with pytest.raises(ScoringViolation):
            score_restoration(gt, RESTORED_RULES, catalog_version="v2", now=NOW)

    def test_restored_rule_catalog_stamp_mismatch_refused(
        self, ground_truth_path: Path
    ) -> None:
        gt = load_ground_truth(
            ground_truth_path,
            expected_file_hash=ground_truth_file_hash(ground_truth_path),
        )
        stale = ({"axis": "verbosity", "value": "terse",
                  "corroboration_grade": "discriminated", "catalog_version": "v0"},)
        with pytest.raises(ScoringViolation):
            score_restoration(gt, stale, now=NOW)

    def test_duplicate_mapped_axis_label_refused(self, tmp_path: Path) -> None:
        specs = LABEL_SPECS + (
            ("gt-006", "응답은 영어로도 병기한다.",
             "response_language", "english", "response_language", "english"),
        )
        path = write_ground_truth(tmp_path / "dup.jsonl", specs=specs)
        with pytest.raises(ScoringViolation):
            load_ground_truth(path, expected_file_hash=ground_truth_file_hash(path))


class TestSealedExampleArtifact:
    """리포지토리에 동봉된 예시 정답지가 봉인 프로토콜 산출물 형태를 갖춘다."""

    def test_example_artifact_loads_with_its_own_seal(self) -> None:
        gt = load_ground_truth(
            EXAMPLE_GROUND_TRUTH,
            expected_file_hash=ground_truth_file_hash(EXAMPLE_GROUND_TRUTH),
        )
        assert gt.catalog_version == "v1"
        assert len(gt.labels) >= 5
        assert any(label.llm_disagrees for label in gt.labels)
        assert any(not label.mappable for label in gt.labels)
        assert all(label.catalog_version == "v1" for label in gt.labels)
        assert gt.seal["draft_model_family"]
        assert gt.seal["reviewed_by"]
        assert gt.seal["session_log_frozen_hash"].startswith("sha256:")

    def test_example_artifact_is_scorable(self, tmp_path: Path) -> None:
        gt = load_ground_truth(
            EXAMPLE_GROUND_TRUTH,
            expected_file_hash=ground_truth_file_hash(EXAMPLE_GROUND_TRUTH),
        )
        result = score_restoration(
            gt, (), refutation_log_path=tmp_path / "log.jsonl", now=NOW
        )
        counts = result.cell_counts()
        # 복원 룰이 없으면 매핑축은 전부 미판별, 장외 문장은 unmappable로만 남는다
        assert counts[CELL_UNDISCRIMINATED] == sum(
            1 for label in gt.labels if label.mappable
        )
        assert counts[CELL_UNMAPPABLE] == sum(
            1 for label in gt.labels if not label.mappable
        )
        assert counts[CELL_RESTORED] == 0
        assert counts[CELL_MIS_RESTORED] == 0


class TestNoBinaryVerdictAnywhere:
    """70% 임계값이나 pass/fail 판정 문자열이 어디에도 없다(정적 검사)."""

    def test_scoring_source_has_no_seventy_and_no_binary_verdict(self) -> None:
        source = SCORING_SOURCE_PATH.read_text(encoding="utf-8")
        assert "70" not in source
        assert re.search(r"(?i)\bpass\b", source) is None
        assert re.search(r"(?i)\bfail\w*\b", source) is None

    def test_example_ground_truth_has_no_seventy_and_no_binary_verdict(self) -> None:
        body = EXAMPLE_GROUND_TRUTH.read_text(encoding="utf-8")
        assert "70" not in body
        assert re.search(r"(?i)\bpass\b", body) is None
        assert re.search(r"(?i)\bfail\w*\b", body) is None

    def test_report_payload_has_no_binary_verdict(self, report) -> None:
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        assert "70%" not in payload
        assert re.search(r"(?i)\bpass\b", payload) is None
        assert re.search(r"(?i)\bfail\w*\b", payload) is None
        assert "verdict" not in report.to_dict()
