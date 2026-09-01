"""Popper 충돌 리포트 - conflict_id 단일 실체, write-time 자동 해소 없음.

기존 수기 룰과 신규 컴파일 룰이 같은 축에서 어긋나도 어느 쪽도 고르지 않는다.
충돌은 conflict_id(축 + 수기룰 id + catalog_version)를 키로 한 번만 생성되고,
같은 키로 (1) 충돌 리포트 행 (2) 재심 큐 충돌 클래스 (3) 채점의 '교정' 셀이
조인된다 - 셋은 표현이 다른 하나의 실체이며 중복 생성되지 않는다.

수기 룰은 consent ledger에 manual_rule_opted_in 레코드가 있기 전에는 반증
대상이 아니다(default-in 금지). consent ledger는 긋기 세션 이벤트 스트림과
분리된 원장이며 가설/판정 fold에 불활성이다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, Mapping


class ConflictViolation(ValueError):
    """충돌 리포트 스키마/불변식 위반."""


class ConsentViolation(RuntimeError):
    """consent ledger 변경 시도 또는 동의 없는 반증 대상 취급."""


# conflict_id 구성 요소 구분자. 요소 안에 등장하면 키가 모호해지므로 금지한다.
ID_SEPARATOR: str = "::"

# 재심 큐 클래스 문자열 - recovery.RECHECK_CLASS_PRIORITY의 마지막 원소와 동일하다.
CONFLICT_CLASS: str = "conflict"
CONFLICT_REASON_PREFIX: str = "conflict:"

# 충돌은 write-time에 해소되지 않는다. 상태값은 이 하나뿐이다.
UNRESOLVED: str = "unresolved"

# corroboration 3등급 + 플립 관측 시 '불안정'.
DISCRIMINATED: str = "discriminated"        # 판별시험 통과
INDISCRIMINATE: str = "indiscriminate"      # 무차별 생존(비-corroborating)
UNTESTED: str = "untested"                  # 완전 미시험
UNSTABLE: str = "unstable"                  # 프로브 플립 관측
CORROBORATION_GRADES: tuple[str, ...] = (DISCRIMINATED, INDISCRIMINATE, UNTESTED, UNSTABLE)

# 등급과 직교하는 값 출처.
VALUE_SOURCES: tuple[str, ...] = ("elicited", "mined-prior")

# 채점 5분류와 코어 지표 분모.
CORRECTION_CELL: str = "교정"
SCORING_CELLS: tuple[str, ...] = ("정복원", "오복원", "미판별", "unmappable", CORRECTION_CELL)
CORE_DENOMINATOR_CELLS: tuple[str, ...] = ("정복원", "오복원")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _require(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConflictViolation(f"{label}는 비울 수 없다")
    if ID_SEPARATOR in value:
        raise ConflictViolation(f"{label}에 구분자 {ID_SEPARATOR!r}를 넣을 수 없다: {value!r}")
    return value


def conflict_id(axis: str, manual_rule_id: str, catalog_version: str) -> str:
    """축 + 수기룰 id + catalog_version의 결정론적 합성 키."""
    return ID_SEPARATOR.join(
        (
            _require(axis, "axis"),
            _require(manual_rule_id, "manual_rule_id"),
            _require(catalog_version, "catalog_version"),
        )
    )


def parse_conflict_id(value: str) -> tuple[str, str, str]:
    """conflict_id를 (축, 수기룰 id, catalog_version)으로 되돌린다."""
    parts = value.split(ID_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        raise ConflictViolation(f"해석할 수 없는 conflict_id: {value!r}")
    return parts[0], parts[1], parts[2]


def conflict_id_from_reason(reason: str) -> str:
    """재심 큐 reason 문자열에서 conflict_id를 복원한다."""
    if not reason.startswith(CONFLICT_REASON_PREFIX):
        raise ConflictViolation(f"충돌 클래스 reason이 아니다: {reason!r}")
    return reason[len(CONFLICT_REASON_PREFIX) :]


@dataclass(frozen=True, slots=True)
class ManualRule:
    """세션 이전부터 존재하던 사용자 수기 룰 - 반증 이력이 없는 미시험 prior."""

    rule_id: str
    axis: str
    value: str
    text: str
    source_path: str | None = None

    def __post_init__(self) -> None:
        for name in ("rule_id", "axis", "value"):
            _require(getattr(self, name), f"ManualRule.{name}")
        if not self.text.strip():
            raise ConflictViolation("ManualRule.text는 비울 수 없다")

    @property
    def corroboration_grade(self) -> str:
        """수기 룰은 긋기를 겪은 적이 없다 - 항상 완전 미시험."""
        return UNTESTED

    @property
    def strike_provenance(self) -> tuple[str, ...]:
        """수기 룰에는 strike 근거가 없다."""
        return ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "axis": self.axis,
            "value": self.value,
            "text": self.text,
            "source_path": self.source_path,
            "corroboration_grade": self.corroboration_grade,
            "strike_provenance": list(self.strike_provenance),
        }


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """긋기에서 컴파일된 신규 룰 - 등급과 strike provenance를 항상 동반한다."""

    rule_id: str
    axis: str
    value: str
    text: str
    corroboration_grade: str
    value_source: str
    strike_provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("rule_id", "axis", "value"):
            _require(getattr(self, name), f"CompiledRule.{name}")
        if not self.text.strip():
            raise ConflictViolation("CompiledRule.text는 비울 수 없다")
        if self.corroboration_grade not in CORROBORATION_GRADES:
            raise ConflictViolation(f"알 수 없는 등급: {self.corroboration_grade!r}")
        if self.value_source not in VALUE_SOURCES:
            raise ConflictViolation(f"알 수 없는 value_source: {self.value_source!r}")
        if not isinstance(self.strike_provenance, tuple):
            raise ConflictViolation("strike_provenance는 tuple이어야 한다")
        if self.corroboration_grade == DISCRIMINATED and not self.strike_provenance:
            raise ConflictViolation("strike 근거 없이 판별시험 통과 등급을 붙일 수 없다")
        if self.corroboration_grade == UNTESTED and self.strike_provenance:
            raise ConflictViolation("완전 미시험 등급에는 strike provenance가 없어야 한다")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "axis": self.axis,
            "value": self.value,
            "text": self.text,
            "corroboration_grade": self.corroboration_grade,
            "value_source": self.value_source,
            "strike_provenance": list(self.strike_provenance),
        }


class ConsentKind(str, Enum):
    """긋기 화면 밖 CLI 단계의 동의 종류 - 세션 이벤트 타입이 아니다."""

    IMPORT_PERMISSION_GRANTED = "import_permission_granted"
    MANUAL_RULE_OPTED_IN = "manual_rule_opted_in"


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """동의 레코드 한 건. 가설/판정 fold에 불활성이다."""

    kind: ConsentKind
    subject: str
    record_id: str = field(default_factory=_new_id)
    at: str = field(default_factory=_now)
    seq: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConsentKind):
            raise ConsentViolation(f"알 수 없는 동의 종류: {self.kind!r}")
        if not self.subject.strip():
            raise ConsentViolation("ConsentRecord.subject는 비울 수 없다")

    @property
    def fold_contribution(self) -> int:
        """동의는 가설 공간을 건드리지 않는다."""
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind.value,
            "subject": self.subject,
            "at": self.at,
            "seq": self.seq,
        }


class ConsentLedger:
    """append-only 동의 원장. 세션 스트림과 분리되어 있고 취소 경로가 없다."""

    __slots__ = ("_records",)

    def __init__(self, records: Iterable[ConsentRecord] = ()) -> None:
        self._records: list[ConsentRecord] = []
        for record in records:
            self.append(record)

    def append(self, record: ConsentRecord) -> ConsentRecord:
        if not isinstance(record, ConsentRecord):
            raise ConsentViolation(f"알 수 없는 동의 레코드: {type(record).__name__}")
        stamped = replace(record, seq=len(self._records))
        self._records.append(stamped)
        return stamped

    def grant_import_permission(self, target: str) -> ConsentRecord:
        return self.append(
            ConsentRecord(kind=ConsentKind.IMPORT_PERMISSION_GRANTED, subject=target)
        )

    def opt_in_manual_rule(self, rule_id: str) -> ConsentRecord:
        return self.append(
            ConsentRecord(kind=ConsentKind.MANUAL_RULE_OPTED_IN, subject=rule_id)
        )

    def is_opted_in(self, rule_id: str) -> bool:
        return any(
            r.kind is ConsentKind.MANUAL_RULE_OPTED_IN and r.subject == rule_id
            for r in self._records
        )

    @property
    def records(self) -> tuple[ConsentRecord, ...]:
        return tuple(self._records)

    def __iter__(self) -> Iterator[ConsentRecord]:
        return iter(tuple(self._records))

    def __len__(self) -> int:
        return len(self._records)

    def __setitem__(self, index: int, value: Any) -> None:
        raise ConsentViolation("consent ledger는 append-only다 - 덮어쓸 수 없다")

    def __delitem__(self, index: int) -> None:
        raise ConsentViolation("consent ledger는 append-only다 - 삭제할 수 없다")


def is_falsification_target(rule: ManualRule, consent: ConsentLedger) -> bool:
    """수기 룰은 manual_rule_opted_in 이전에는 반증 대상이 아니다(default-in 금지)."""
    return consent.is_opted_in(rule.rule_id)


@dataclass(frozen=True, slots=True)
class ConflictEntry:
    """축 단위 충돌 하나. 세 표면이 공유하는 유일한 실체다."""

    conflict_id: str
    axis: str
    catalog_version: str
    manual: ManualRule
    compiled: CompiledRule
    resolution: str = UNRESOLVED

    def __post_init__(self) -> None:
        if self.resolution != UNRESOLVED:
            raise ConflictViolation("충돌은 write-time에 해소되지 않는다")
        if self.manual.axis != self.axis or self.compiled.axis != self.axis:
            raise ConflictViolation("충돌 양측의 축이 일치하지 않는다")
        if self.manual.value == self.compiled.value:
            raise ConflictViolation("값이 같으면 충돌이 아니다")
        expected = conflict_id(self.axis, self.manual.rule_id, self.catalog_version)
        if self.conflict_id != expected:
            raise ConflictViolation(f"conflict_id가 키 규칙과 다르다: {self.conflict_id!r}")

    def manual_side(self) -> dict[str, Any]:
        """수기 = 미시험 prior. 등급은 완전 미시험이고 strike 근거가 없다."""
        return {
            "side": "manual",
            "rule_id": self.manual.rule_id,
            "value": self.manual.value,
            "text": self.manual.text,
            "corroboration_grade": self.manual.corroboration_grade,
            "strike_provenance": list(self.manual.strike_provenance),
        }

    def compiled_side(self) -> dict[str, Any]:
        """신규 = corroboration 등급 + strike provenance."""
        return {
            "side": "compiled",
            "rule_id": self.compiled.rule_id,
            "value": self.compiled.value,
            "text": self.compiled.text,
            "corroboration_grade": self.compiled.corroboration_grade,
            "value_source": self.compiled.value_source,
            "strike_provenance": list(self.compiled.strike_provenance),
        }

    def report_row(self) -> dict[str, Any]:
        """충돌 리포트 행 - 양측을 나란히 싣고 승자를 고르지 않는다."""
        return {
            "conflict_id": self.conflict_id,
            "axis": self.axis,
            "catalog_version": self.catalog_version,
            "resolution": self.resolution,
            "sides": [self.manual_side(), self.compiled_side()],
        }

    def to_recheck_entry(self, factory: Callable[..., Any] | None = None) -> Any:
        """재심 큐 충돌 클래스 항목. factory를 주면 그 스키마로 조립한다."""
        payload = {
            "axis": self.axis,
            "klass": CONFLICT_CLASS,
            "reason": f"{CONFLICT_REASON_PREFIX}{self.conflict_id}",
            "value": None,
        }
        if factory is None:
            return dict(payload, conflict_id=self.conflict_id)
        return factory(**payload)

    def correction_cell(self) -> dict[str, Any]:
        """채점의 '교정' 셀 - 같은 conflict_id로 조인되며 코어 분모에서 빠진다."""
        return {
            "axis": self.axis,
            "cell": CORRECTION_CELL,
            "conflict_id": self.conflict_id,
            "in_core_denominator": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.report_row()


class ConflictReport:
    """conflict_id 유일성을 보장하는 append-only 충돌 리포트."""

    __slots__ = ("_entries", "_index")

    def __init__(self, entries: Iterable[ConflictEntry] = ()) -> None:
        self._entries: list[ConflictEntry] = []
        self._index: dict[str, ConflictEntry] = {}
        for entry in entries:
            self.add(entry)

    def add(self, entry: ConflictEntry) -> ConflictEntry:
        if not isinstance(entry, ConflictEntry):
            raise ConflictViolation(f"알 수 없는 충돌 항목: {type(entry).__name__}")
        if entry.conflict_id in self._index:
            raise ConflictViolation(f"conflict_id 중복 표현 금지: {entry.conflict_id}")
        self._entries.append(entry)
        self._index[entry.conflict_id] = entry
        return entry

    @property
    def entries(self) -> tuple[ConflictEntry, ...]:
        return tuple(self._entries)

    @property
    def conflict_ids(self) -> tuple[str, ...]:
        return tuple(e.conflict_id for e in self._entries)

    def get(self, key: str) -> ConflictEntry | None:
        return self._index.get(key)

    def report_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(e.report_row() for e in self._entries)

    def recheck_entries(self, factory: Callable[..., Any] | None = None) -> tuple[Any, ...]:
        return tuple(e.to_recheck_entry(factory) for e in self._entries)

    def correction_cells(self) -> tuple[dict[str, Any], ...]:
        return tuple(e.correction_cell() for e in self._entries)

    def __iter__(self) -> Iterator[ConflictEntry]:
        return iter(tuple(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __setitem__(self, index: int, value: Any) -> None:
        raise ConflictViolation("충돌 리포트는 append-only다 - 덮어쓸 수 없다")

    def __delitem__(self, index: int) -> None:
        raise ConflictViolation("충돌 리포트는 append-only다 - 삭제할 수 없다")


def detect_conflicts(
    manual_rules: Iterable[ManualRule],
    compiled_rules: Iterable[CompiledRule],
    catalog_version: str,
    consent: ConsentLedger,
) -> ConflictReport:
    """수기 룰과 신규 룰의 축 단위 충돌을 기록만 한다 - 자동 해소 없음."""
    _require(catalog_version, "catalog_version")
    if not isinstance(consent, ConsentLedger):
        raise ConsentViolation("consent ledger 없이는 수기 룰을 반증 대상으로 볼 수 없다")

    by_axis: dict[str, CompiledRule] = {}
    for compiled in compiled_rules:
        if compiled.axis in by_axis:
            raise ConflictViolation(f"축당 컴파일 룰은 하나여야 한다: {compiled.axis}")
        by_axis[compiled.axis] = compiled

    report = ConflictReport()
    for manual in manual_rules:
        if not is_falsification_target(manual, consent):
            continue
        compiled = by_axis.get(manual.axis)
        if compiled is None or compiled.value == manual.value:
            continue
        report.add(
            ConflictEntry(
                conflict_id=conflict_id(manual.axis, manual.rule_id, catalog_version),
                axis=manual.axis,
                catalog_version=catalog_version,
                manual=manual,
                compiled=compiled,
            )
        )
    return report


def core_denominator(cells: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """코어 반증 지표 분모 - '교정' 셀과 out-of-catalog는 빠진다."""
    return tuple(c for c in cells if c.get("cell") in CORE_DENOMINATOR_CELLS)


def excluded_from_core(cells: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """분모에서 제외되어 별도 리포트로 가는 셀들."""
    return tuple(c for c in cells if c.get("cell") not in CORE_DENOMINATOR_CELLS)
