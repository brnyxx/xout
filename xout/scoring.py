"""AC12 - 블라인드 복원 채점: 봉인 프로토콜 정답지 기반 축별 5분류 정성 리포트.

정답지는 봉인 프로토콜(타 모델 계열 초안 -> 세션 전 해시 봉인 -> 세션 로그 동결 ->
개봉 + 본인 검수)로 만든 불변 JSONL 아티팩트이며, 이 모듈은 그것을 읽기 전용으로만
다룬다. 채점은 축별 5분류(정복원/오복원/미판별/unmappable/교정) 정성 리포트를 내고,
어떤 합/불 판정 문자열이나 단일 비율 컷도 만들지 않는다.

- '교정' 셀(수기값 != 판별시험 생존값인 충돌 축)은 conflict_id로 충돌 리포트와
  조인되며, core 지표 분모에서 제외되어 별도 보고된다.
- 정확도 분모는 매핑된 축만이다. 커버리지(매핑축/카탈로그 축 수)와 LLM 초안-본인
  확정 불일치율은 각각 별도 지표로 보고된다.
- out-of-catalog 문장은 채점에서 제외하되 원문째 JSONL 반증 로그로 보존한다.
- 정답지 파일 해시가 기대 해시와 다르면 채점을 거부한다(봉인 위반).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from xout.compiler import CATALOG_VERSION, METRIC_SPEC_VERSION, content_hash
from xout.conflict import (
    CORE_DENOMINATOR_CELLS,
    CORRECTION_CELL,
    CORROBORATION_GRADES,
    DISCRIMINATED,
    SCORING_CELLS,
    core_denominator,
)
from xout.counter import DEFAULT_CATALOG, LEGACY_AXES

logger = logging.getLogger(__name__)

# 채점 5분류 - conflict.SCORING_CELLS 순서(정복원/오복원/미판별/unmappable/교정)를 재사용한다.
CELL_RESTORED = SCORING_CELLS[0]
CELL_MIS_RESTORED = SCORING_CELLS[1]
CELL_UNDISCRIMINATED = SCORING_CELLS[2]
CELL_UNMAPPABLE = SCORING_CELLS[3]
CELL_CORRECTED = CORRECTION_CELL

GROUND_TRUTH_ARTIFACT = "popper_ground_truth"
SCORING_REPORT_ARTIFACT = "popper_scoring_report"
SEAL_RECORD = "seal"
LABEL_RECORD = "label"
OUT_OF_CATALOG_RECORD = "out_of_catalog_refutation"
HASH_PREFIX = "sha256:"

_SOURCE_GROUND_TRUTH_PATH = (
    Path(__file__).resolve().parent.parent / "ground_truth" / "ground_truth.jsonl"
)
_PACKAGED_GROUND_TRUTH_PATH = (
    Path(__file__).resolve().parent / "_data" / "ground_truth" / "ground_truth.txt"
)
DEFAULT_GROUND_TRUTH_PATH = (
    _SOURCE_GROUND_TRUTH_PATH
    if _SOURCE_GROUND_TRUTH_PATH.is_file()
    else _PACKAGED_GROUND_TRUTH_PATH
)
_SOURCE_GROUND_TRUTH_HASH_PATH = (
    Path(__file__).resolve().parent.parent / "ground_truth" / "ground_truth.sha256"
)
_PACKAGED_GROUND_TRUTH_HASH_PATH = (
    Path(__file__).resolve().parent / "_data" / "ground_truth" / "ground_truth.sha256"
)
DEFAULT_GROUND_TRUTH_HASH_PATH = (
    _SOURCE_GROUND_TRUTH_HASH_PATH
    if _SOURCE_GROUND_TRUTH_HASH_PATH.is_file()
    else _PACKAGED_GROUND_TRUTH_HASH_PATH
)

# 봉인 프로토콜 4단계의 증빙 필드 - seal 레코드에 전부 실려 있어야 한다.
SEAL_PROTOCOL_FIELDS = (
    "draft_model_family",       # 타 모델 계열 초안
    "draft_digest",             # 세션 전 해시 봉인
    "session_log_frozen_hash",  # 세션 로그 동결
    "opened_at",                # 개봉
    "reviewed_by",              # 본인 검수
)


class ScoringViolation(ValueError):
    """정답지/복원 입력이 채점 스키마를 위반했을 때."""


class SealViolation(RuntimeError):
    """봉인 위반 - 정답지 해시가 기대값과 다르면 채점을 거부한다."""


@dataclass(frozen=True, slots=True)
class GroundTruthLabel:
    """정답지 JSONL 한 행 - 룰 문장, 확정 축x값, 봉인 참조, LLM 초안 라벨."""

    label_id: str
    rule_text: str
    axis: str | None
    value: str | None
    catalog_version: str
    seal_ref: str
    llm_draft_axis: str | None
    llm_draft_value: str | None
    confirmed_axis: str | None
    confirmed_value: str | None

    def __post_init__(self) -> None:
        for name in ("label_id", "rule_text", "catalog_version", "seal_ref"):
            if not getattr(self, name):
                raise ScoringViolation(f"GroundTruthLabel.{name}는 비울 수 없다")
        if (self.axis, self.value) != (self.confirmed_axis, self.confirmed_value):
            raise ScoringViolation(
                f"정답지 축/값은 본인 확정 라벨과 같아야 한다: {self.label_id}"
            )
        if (self.axis is None) != (self.value is None):
            raise ScoringViolation(f"축과 값은 함께 비거나 함께 있어야 한다: {self.label_id}")

    @property
    def mappable(self) -> bool:
        """본인 검수가 카탈로그 축으로 확정한 문장인가."""
        return self.confirmed_axis is not None

    @property
    def llm_disagrees(self) -> bool:
        """LLM 초안 라벨과 본인 확정 라벨이 어긋나는가(불일치율 기록용)."""
        return (self.llm_draft_axis, self.llm_draft_value) != (
            self.confirmed_axis,
            self.confirmed_value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_id": self.label_id,
            "rule_text": self.rule_text,
            "axis": self.axis,
            "value": self.value,
            "catalog_version": self.catalog_version,
            "seal_ref": self.seal_ref,
            "llm_draft": {"axis": self.llm_draft_axis, "value": self.llm_draft_value},
            "confirmed": {"axis": self.confirmed_axis, "value": self.confirmed_value},
        }


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """봉인 검증을 통과한 불변 정답지 - scoring은 여기에 아무것도 쓰지 않는다."""

    path: Path
    file_hash: str
    catalog_version: str
    draft_digest: str
    seal: dict[str, Any]
    labels: tuple[GroundTruthLabel, ...]


@dataclass(frozen=True, slots=True)
class ScoringReport:
    """(catalog_version, metric_spec_version)으로 파라미터화된 축별 5분류 정성 리포트."""

    catalog_version: str
    metric_spec_version: str
    generated_at: str
    ground_truth_path: str
    ground_truth_hash: str
    cells: tuple[dict[str, Any], ...]
    corrected: tuple[dict[str, Any], ...]
    core: dict[str, Any]
    accuracy: dict[str, Any]
    coverage: dict[str, Any]
    llm_review_disagreement: dict[str, Any]
    out_of_catalog: tuple[dict[str, Any], ...]
    refutation_log_path: str | None = None

    def cell_counts(self) -> dict[str, int]:
        counts = {cell: 0 for cell in SCORING_CELLS}
        for row in self.cells:
            counts[str(row["cell"])] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": SCORING_REPORT_ARTIFACT,
            "qualitative_only": True,
            "catalog_version": self.catalog_version,
            "metric_spec_version": self.metric_spec_version,
            "generated_at": self.generated_at,
            "ground_truth_path": self.ground_truth_path,
            "ground_truth_hash": self.ground_truth_hash,
            "cell_counts": self.cell_counts(),
            "cells": [dict(row) for row in self.cells],
            "corrected": [dict(row) for row in self.corrected],
            "core": dict(self.core),
            "accuracy": dict(self.accuracy),
            "coverage": dict(self.coverage),
            "llm_review_disagreement": dict(self.llm_review_disagreement),
            "out_of_catalog": [dict(row) for row in self.out_of_catalog],
            "refutation_log_path": self.refutation_log_path,
        }


def _now(now: str | None = None) -> str:
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


def _ratio(numerator: int, denominator: int) -> float | None:
    """정성 리포트용 비율 - 분모 0이면 None. 어떤 컷과도 비교하지 않는다."""
    if denominator == 0:
        return None
    return numerator / denominator


def draft_digest(draft_labels: Sequence[Mapping[str, Any]]) -> str:
    """세션 전 봉인과 동일한 정준화로 LLM 초안 라벨 목록을 해시한다.

    prereg 봉인과 같은 기계다: json.dumps(sort_keys=True, separators=(',', ':'),
    ensure_ascii=False) 바이트의 sha256 hexdigest.
    """
    canonical = json.dumps(
        [dict(label) for label in draft_labels],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def ground_truth_file_hash(path: Path | str) -> str:
    """정답지 파일 바이트의 봉인 해시 - 봉인 시점에 한 번 계산해 기대값으로 고정한다."""
    return content_hash(Path(path).read_bytes())


def _parse_label(payload: Mapping[str, Any]) -> GroundTruthLabel:
    llm_draft = payload.get("llm_draft") or {}
    confirmed = payload.get("confirmed") or {}
    if not isinstance(llm_draft, Mapping) or not isinstance(confirmed, Mapping):
        raise ScoringViolation("llm_draft/confirmed는 객체여야 한다")
    return GroundTruthLabel(
        label_id=str(payload.get("label_id") or ""),
        rule_text=str(payload.get("rule_text") or ""),
        axis=payload.get("axis"),
        value=payload.get("value"),
        catalog_version=str(payload.get("catalog_version") or ""),
        seal_ref=str(payload.get("seal_ref") or ""),
        llm_draft_axis=llm_draft.get("axis"),
        llm_draft_value=llm_draft.get("value"),
        confirmed_axis=confirmed.get("axis"),
        confirmed_value=confirmed.get("value"),
    )


def load_ground_truth(path: Path | str, *, expected_file_hash: str) -> GroundTruth:
    """봉인된 정답지를 읽기 전용으로 적재한다.

    파일 해시가 기대 해시와 다르면 SealViolation으로 채점을 거부한다(봉인 위반).
    적재 중 초안 봉인 해시(draft_digest)도 같은 기계로 재계산해 대조한다.
    """
    source = Path(path)
    raw = source.read_bytes()
    actual_hash = content_hash(raw)
    if actual_hash != expected_file_hash:
        logger.warning(
            "정답지 봉인 위반 - 채점 거부: %s expected=%s actual=%s",
            source,
            expected_file_hash,
            actual_hash,
        )
        raise SealViolation(
            f"정답지 파일 해시가 기대 해시와 다르다: expected={expected_file_hash} actual={actual_hash}"
        )

    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScoringViolation(f"정답지 JSONL 해석 불가: {source}:{lineno}") from exc
        if not isinstance(payload, dict):
            raise ScoringViolation(f"정답지 행은 객체여야 한다: {source}:{lineno}")
        records.append(payload)

    if not records or records[0].get("record") != SEAL_RECORD:
        raise ScoringViolation("정답지 첫 행은 seal 레코드여야 한다")
    seal = records[0]
    if seal.get("artifact") != GROUND_TRUTH_ARTIFACT:
        raise ScoringViolation(f"알 수 없는 정답지 아티팩트: {seal.get('artifact')!r}")
    if seal.get("algorithm") != "sha256":
        raise ScoringViolation(f"알 수 없는 봉인 알고리즘: {seal.get('algorithm')!r}")
    for field_name in SEAL_PROTOCOL_FIELDS:
        if not seal.get(field_name):
            raise ScoringViolation(f"봉인 프로토콜 필드 누락: {field_name}")
    catalog_version = str(seal.get("catalog_version") or "")
    if not catalog_version:
        raise ScoringViolation("seal 레코드에 catalog_version 스탬프가 없다")
    sealed_digest = str(seal["draft_digest"])

    labels: list[GroundTruthLabel] = []
    draft_rows: list[dict[str, Any]] = []
    mapped_axes: set[str] = set()
    for payload in records[1:]:
        if payload.get("record") != LABEL_RECORD:
            raise ScoringViolation(f"알 수 없는 정답지 레코드: {payload.get('record')!r}")
        label = _parse_label(payload)
        if label.catalog_version != catalog_version:
            raise ScoringViolation(
                f"라벨 catalog_version 스탬프 불일치: {label.label_id} "
                f"({label.catalog_version!r} != {catalog_version!r})"
            )
        if label.seal_ref != HASH_PREFIX + sealed_digest:
            raise SealViolation(f"라벨 봉인 참조가 seal과 다르다: {label.label_id}")
        if label.mappable:
            if label.confirmed_axis in mapped_axes:
                raise ScoringViolation(f"매핑축 중복 라벨: {label.confirmed_axis}")
            mapped_axes.add(str(label.confirmed_axis))
        labels.append(label)
        draft_rows.append(
            {
                "label_id": label.label_id,
                "rule_text": label.rule_text,
                "axis": label.llm_draft_axis,
                "value": label.llm_draft_value,
            }
        )

    recomputed = draft_digest(draft_rows)
    if recomputed != sealed_digest:
        raise SealViolation(
            f"초안 봉인 해시 재계산 불일치: sealed={sealed_digest} recomputed={recomputed}"
        )

    logger.info("정답지 봉인 검증 완료: %s (%d개 라벨)", source, len(labels))
    return GroundTruth(
        path=source,
        file_hash=actual_hash,
        catalog_version=catalog_version,
        draft_digest=sealed_digest,
        seal=dict(seal),
        labels=tuple(labels),
    )


def _normalize_restored(
    restored_rules: Iterable[Any], catalog_version: str
) -> dict[str, dict[str, Any]]:
    """세션이 복원한 축별 값 - manifest 룰 dict나 CompiledRule 겸용."""
    by_axis: dict[str, dict[str, Any]] = {}
    for rule in restored_rules:
        if isinstance(rule, Mapping):
            axis = rule.get("axis")
            value = rule.get("value")
            grade = rule.get("corroboration_grade")
            stamped = rule.get("catalog_version")
        else:
            axis = getattr(rule, "axis", None)
            value = getattr(rule, "value", None)
            grade = getattr(rule, "corroboration_grade", None)
            stamped = getattr(rule, "catalog_version", None)
        if not axis or not value:
            raise ScoringViolation("복원 룰에는 axis와 value가 있어야 한다")
        if grade not in CORROBORATION_GRADES:
            raise ScoringViolation(f"알 수 없는 corroboration 등급: {grade!r}")
        if stamped is not None and str(stamped) != catalog_version:
            raise ScoringViolation(
                f"복원 룰 catalog_version 스탬프 불일치: {axis} ({stamped!r} != {catalog_version!r})"
            )
        if axis in by_axis:
            raise ScoringViolation(f"축당 복원 룰은 하나여야 한다: {axis}")
        by_axis[str(axis)] = {"value": str(value), "corroboration_grade": str(grade)}
    return by_axis


def _normalize_corrections(corrections: Iterable[Any]) -> dict[str, str]:
    """교정 셀 입력 - ConflictReport.correction_cells() 행이나 동형 객체를 받는다."""
    by_axis: dict[str, str] = {}
    for cell in corrections:
        if isinstance(cell, Mapping):
            axis = cell.get("axis")
            joined = cell.get("conflict_id")
            kind = cell.get("cell", CELL_CORRECTED)
        else:
            axis = getattr(cell, "axis", None)
            joined = getattr(cell, "conflict_id", None)
            kind = getattr(cell, "cell", CELL_CORRECTED)
        if kind != CELL_CORRECTED:
            raise ScoringViolation(f"교정 셀이 아닌 항목이 corrections에 들어왔다: {kind!r}")
        if not axis or not joined:
            raise ScoringViolation("교정 셀에는 axis와 conflict_id가 있어야 한다")
        if axis in by_axis:
            raise ScoringViolation(f"축당 교정 셀은 하나여야 한다: {axis}")
        by_axis[str(axis)] = str(joined)
    return by_axis


def _cell_row(
    label: GroundTruthLabel,
    cell: str,
    *,
    restored_value: str | None = None,
    grade: str | None = None,
    joined_conflict_id: str | None = None,
) -> dict[str, Any]:
    return {
        "label_id": label.label_id,
        "axis": label.confirmed_axis,
        "cell": cell,
        "handwritten_value": label.confirmed_value,
        "restored_value": restored_value,
        "corroboration_grade": grade,
        "conflict_id": joined_conflict_id,
        "in_core_denominator": cell in CORE_DENOMINATOR_CELLS,
    }


def _append_refutation_log(target: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """out-of-catalog 문장을 원문째 JSONL 반증 로그로 보존한다(append-only)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    logger.info("out-of-catalog 반증 로그 %d건 보존: %s", len(rows), target)


def score_restoration(
    ground_truth: GroundTruth,
    restored_rules: Iterable[Any],
    *,
    corrections: Iterable[Any] = (),
    catalog: Mapping[str, Sequence[str]] | None = None,
    catalog_version: str = CATALOG_VERSION,
    metric_spec_version: str = METRIC_SPEC_VERSION,
    refutation_log_path: Path | str | None = None,
    now: str | None = None,
) -> ScoringReport:
    """블라인드 복원 채점 - 축별 5분류 정성 리포트를 만든다.

    - 정복원/오복원: 판별시험 통과 축에서 복원값과 수기값을 대조한다.
    - 미판별: 판별시험을 통과하지 못한(또는 복원 안 된) 매핑축.
    - unmappable: 본인 검수가 장외로 확정한 문장 - 채점 제외, 반증 로그로 보존.
    - 교정: 수기값 != 판별시험 생존값인 충돌 축 - conflict_id로 조인되며
      core 지표 분모에서 제외되어 별도 보고된다.
    """
    active = dict(catalog) if catalog is not None else dict(DEFAULT_CATALOG)
    if ground_truth.catalog_version not in {"v1", catalog_version}:
        # v1 봉인 정답지는 이관 없이 존중한다 - 그 밖의 불일치는 여전히 거부.
        raise ScoringViolation(
            f"정답지 catalog_version과 채점 catalog_version이 다르다: "
            f"{ground_truth.catalog_version!r} != {catalog_version!r}"
        )
    restored = _normalize_restored(restored_rules, catalog_version)
    corrected_by_axis = _normalize_corrections(corrections)
    stamp = _now(now)

    cells: list[dict[str, Any]] = []
    corrected_rows: list[dict[str, Any]] = []
    out_rows: list[dict[str, Any]] = []
    for label in ground_truth.labels:
        if not label.mappable:
            cells.append(_cell_row(label, CELL_UNMAPPABLE))
            out_rows.append(
                {
                    "record": OUT_OF_CATALOG_RECORD,
                    "label_id": label.label_id,
                    "rule_text": label.rule_text,
                    "llm_draft": {
                        "axis": label.llm_draft_axis,
                        "value": label.llm_draft_value,
                    },
                    "catalog_version": catalog_version,
                    "seal_ref": label.seal_ref,
                    "ground_truth_hash": ground_truth.file_hash,
                    "at": stamp,
                }
            )
            continue

        axis = str(label.confirmed_axis)
        if axis in LEGACY_AXES:
            # v1 봉인 라벨의 은퇴 축 - 채점 분모 밖 반증 로그로만 보존한다.
            cells.append(_cell_row(label, CELL_UNMAPPABLE))
            out_rows.append(
                {
                    "record": OUT_OF_CATALOG_RECORD,
                    "label_id": label.label_id,
                    "rule_text": label.rule_text,
                    "llm_draft": {
                        "axis": label.llm_draft_axis,
                        "value": label.llm_draft_value,
                    },
                    "catalog_version": catalog_version,
                    "legacy_axis": axis,
                    "seal_ref": label.seal_ref,
                    "ground_truth_hash": ground_truth.file_hash,
                    "at": stamp,
                }
            )
            continue
        if axis not in active:
            raise ScoringViolation(f"카탈로그 밖 축이 매핑 라벨에 있다: {axis}")
        if label.confirmed_value not in active[axis]:
            raise ScoringViolation(
                f"카탈로그 밖 값이 매핑 라벨에 있다: {axis}={label.confirmed_value!r}"
            )

        rule = restored.get(axis)
        if axis in corrected_by_axis:
            row = _cell_row(
                label,
                CELL_CORRECTED,
                restored_value=rule["value"] if rule else None,
                grade=rule["corroboration_grade"] if rule else None,
                joined_conflict_id=corrected_by_axis[axis],
            )
            cells.append(row)
            corrected_rows.append(row)
            continue
        if rule is None or rule["corroboration_grade"] != DISCRIMINATED:
            cells.append(
                _cell_row(
                    label,
                    CELL_UNDISCRIMINATED,
                    restored_value=rule["value"] if rule else None,
                    grade=rule["corroboration_grade"] if rule else None,
                )
            )
            continue
        cell = (
            CELL_RESTORED
            if rule["value"] == label.confirmed_value
            else CELL_MIS_RESTORED
        )
        cells.append(
            _cell_row(label, cell, restored_value=rule["value"], grade=DISCRIMINATED)
        )

    core_cells = core_denominator(cells)
    restored_count = sum(1 for row in core_cells if row["cell"] == CELL_RESTORED)
    mis_restored_count = sum(1 for row in core_cells if row["cell"] == CELL_MIS_RESTORED)
    core = {
        "denominator_cells": list(CORE_DENOMINATOR_CELLS),
        "denominator": len(core_cells),
        "restored": restored_count,
        "mis_restored": mis_restored_count,
        "mis_restored_ratio": _ratio(mis_restored_count, len(core_cells)),
        "corrected_excluded": len(corrected_rows),
        "note": "교정 셀은 core 지표 분모에서 제외되어 별도 보고된다",
    }

    mapped_cells = [row for row in cells if row["cell"] != CELL_UNMAPPABLE]
    accuracy_cells = [
        row
        for row in mapped_cells
        if row["cell"] in (CELL_RESTORED, CELL_MIS_RESTORED, CELL_UNDISCRIMINATED)
    ]
    accuracy = {
        "denominator_rule": "매핑된 축만 - unmappable 문장과 교정 셀은 분모 밖",
        "denominator": len(accuracy_cells),
        "restored": restored_count,
        "ratio": _ratio(restored_count, len(accuracy_cells)),
    }

    mapped_axes = sorted({str(row["axis"]) for row in mapped_cells})
    coverage = {
        "mapped_axes": mapped_axes,
        "mapped_axis_total": len(mapped_axes),
        "catalog_axis_total": len(active),
        "ratio": _ratio(len(mapped_axes), len(active)),
        "note": "커버리지는 정확도와 별개 지표로 보고된다",
    }

    disagreement_rows = [
        {
            "label_id": label.label_id,
            "llm_draft": {"axis": label.llm_draft_axis, "value": label.llm_draft_value},
            "confirmed": {"axis": label.confirmed_axis, "value": label.confirmed_value},
        }
        for label in ground_truth.labels
        if label.llm_disagrees
    ]
    llm_review_disagreement = {
        "total_labels": len(ground_truth.labels),
        "disagreements": len(disagreement_rows),
        "ratio": _ratio(len(disagreement_rows), len(ground_truth.labels)),
        "rows": disagreement_rows,
    }

    log_path: str | None = None
    if refutation_log_path is not None:
        target = Path(refutation_log_path)
        _append_refutation_log(target, out_rows)
        log_path = str(target)

    return ScoringReport(
        catalog_version=catalog_version,
        metric_spec_version=metric_spec_version,
        generated_at=stamp,
        ground_truth_path=str(ground_truth.path),
        ground_truth_hash=ground_truth.file_hash,
        cells=tuple(cells),
        corrected=tuple(corrected_rows),
        core=core,
        accuracy=accuracy,
        coverage=coverage,
        llm_review_disagreement=llm_review_disagreement,
        out_of_catalog=tuple(out_rows),
        refutation_log_path=log_path,
    )
