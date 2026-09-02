"""채굴 패턴 레이어의 측정 계약.

`MINED_PATTERNS`는 눈대중 휴리스틱이 아니라 측정되는 층이다. 라벨 코퍼스
(tests/data/mine_corpus.json)에 4개 언어의 실제 규칙 파일 줄과 정답 셀이
들어 있고, 이 테스트가 축별 정밀도/재현율 바닥을 지킨다. 바닥을 깨는 변경은
어떤 줄이 어디로 새는지 그대로 출력하고 실패한다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from xout.counter import DEFAULT_CATALOG
from xout.mine import attribute

CORPUS_PATH = Path(__file__).parent / "data" / "mine_corpus.json"

#: 축별 바닥. 정밀도가 재현율보다 높다 - 잘못된 귀속이 놓친 귀속보다 비싸다.
MIN_AXIS_PRECISION = 0.90
MIN_AXIS_RECALL = 0.85
MIN_OVERALL_PRECISION = 0.92

LANGS = ("en", "ko", "ja", "zh")

#: 언어별 셀 최소 양성 수 - 어떤 언어도 다른 언어의 번역판이 아니다.
MIN_POSITIVES_PER_CELL = {"en": 2, "ko": 2, "ja": 1, "zh": 1}


def _corpus() -> list[dict[str, object]]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _cell(entry: dict[str, object]) -> tuple[str, str] | None:
    expect = entry["expect"]
    return None if expect is None else (expect[0], expect[1])


def test_corpus_is_well_formed() -> None:
    corpus = _corpus()
    assert len(corpus) >= 200, f"코퍼스가 너무 작다: {len(corpus)}"
    negatives = [entry for entry in corpus if _cell(entry) is None]
    assert len(negatives) >= 40, f"음성 예시가 너무 적다: {len(negatives)}"
    for entry in corpus:
        assert entry["lang"] in LANGS, entry
        assert entry["text"].strip() == entry["text"] and entry["text"], entry
        cell = _cell(entry)
        if cell is not None:
            assert cell[1] in DEFAULT_CATALOG[cell[0]], entry


def test_every_cell_has_positives_in_every_language() -> None:
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for entry in _corpus():
        cell = _cell(entry)
        if cell is not None:
            counts[(cell[0], cell[1], entry["lang"])] += 1
    thin: list[str] = []
    for axis, values in DEFAULT_CATALOG.items():
        for value in values:
            for lang in LANGS:
                have = counts[(axis, value, lang)]
                want = MIN_POSITIVES_PER_CELL[lang]
                if have < want:
                    thin.append(f"{axis}/{value} [{lang}]: {have} < {want}")
    assert not thin, "라벨이 얇은 셀:\n" + "\n".join(thin)


def test_matcher_holds_the_measured_floor() -> None:
    corpus = _corpus()
    hits: dict[str, int] = defaultdict(int)
    wrong: dict[str, int] = defaultdict(int)
    missed: dict[str, int] = defaultdict(int)
    miss_lines: list[str] = []
    wrong_lines: list[str] = []

    for entry in corpus:
        expect = _cell(entry)
        attributed = set(attribute(entry["text"]))
        if expect is not None:
            if expect in attributed:
                hits[expect[0]] += 1
            else:
                missed[expect[0]] += 1
                got = sorted(f"{axis}/{value}" for axis, value in attributed) or ["없음"]
                miss_lines.append(
                    f"  [{entry['lang']}] {expect[0]}/{expect[1]} 인데 {got}: {entry['text']}"
                )
        for axis, value in attributed:
            if (axis, value) == expect:
                continue
            wrong[axis] += 1
            want = f"{expect[0]}/{expect[1]}" if expect else "귀속 없음"
            wrong_lines.append(
                f"  [{entry['lang']}] {want} 인데 {axis}/{value}: {entry['text']}"
            )

    report: list[str] = []
    for axis in DEFAULT_CATALOG:
        tp, fp, fn = hits[axis], wrong[axis], missed[axis]
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        if precision < MIN_AXIS_PRECISION or recall < MIN_AXIS_RECALL:
            report.append(
                f"{axis}: 정밀도 {precision:.3f} 재현율 {recall:.3f} (맞음 {tp} / 오귀속 {fp} / 놓침 {fn})"
            )
    total_hits = sum(hits.values())
    total_wrong = sum(wrong.values())
    overall = total_hits / (total_hits + total_wrong) if total_hits + total_wrong else 1.0
    if overall < MIN_OVERALL_PRECISION:
        report.append(f"전체 정밀도 {overall:.3f} < {MIN_OVERALL_PRECISION}")

    if report:
        print("\n".join(report))
        print("놓친 줄:\n" + "\n".join(miss_lines))
        print("잘못 귀속한 줄:\n" + "\n".join(wrong_lines))
    assert not report, "\n".join(report) + "\n놓친 줄:\n" + "\n".join(miss_lines) + (
        "\n잘못 귀속한 줄:\n" + "\n".join(wrong_lines)
    )


def test_negatives_stay_unattributed() -> None:
    """가까워 보이는 줄은 귀속하지 않는다 - 전체 정밀도와 별개로 음성만 따로 본다."""
    leaked = [
        f"  [{entry['lang']}] {sorted(attribute(entry['text']))}: {entry['text']}"
        for entry in _corpus()
        if _cell(entry) is None and attribute(entry["text"])
    ]
    ratio = 1 - len(leaked) / max(1, len([e for e in _corpus() if _cell(e) is None]))
    assert ratio >= 0.90, "음성에서 샌 줄:\n" + "\n".join(leaked)
