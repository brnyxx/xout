"""영문 런타임 팩 계약.

언어는 렌더 계층에만 존재한다: 이벤트 원장은 축/값/fragment_id만 기록하고,
페어 본문/축 라벨/컴파일된 규칙 문안만 언어별 테이블에서 나온다. 영문 팩은
한국어 팩과 구조(장면/맥락/슬롯 축)가 동일해야 하며, 영문 산출물에는 한국어가
남으면 안 된다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from xout.compiler import (
    IRREVERSIBLE_CLAUSE,
    RULE_LANG_TABLES,
    RULE_TEXT,
    RULE_TEXT_EN,
    XOUT_MD,
    compile_rules,
    conditional_rule_text,
)
from xout.counter import DEFAULT_CATALOG
from xout.fixtures import SUPPORTED_LANGS, FixtureViolation, load_pack
from xout.state import ColdOpenSession
from xout.store import EventStore

_HANGUL = re.compile(r"[가-힣]")
_FOREIGN = tuple(lang for lang in SUPPORTED_LANGS if lang != "ko")


@pytest.mark.parametrize("lang", _FOREIGN)
def test_foreign_pack_mirrors_ko_pack_structure(lang: str) -> None:
    ko = load_pack()
    en = load_pack(lang=lang)
    assert en.scene_ids == ko.scene_ids
    for scene_id in ko.scene_ids:
        ko_scene = ko.scene(scene_id)
        en_scene = en.scene(scene_id)
        assert en_scene.context == ko_scene.context
        assert en_scene.slot_axes == ko_scene.slot_axes
        for axis in ko_scene.slot_axes:
            for value in DEFAULT_CATALOG[axis]:
                en_text = en_scene.axis_slots[axis][value]
                assert not _HANGUL.search(en_text), (scene_id, axis, value)


def test_unsupported_lang_is_rejected() -> None:
    with pytest.raises(FixtureViolation):
        load_pack(lang="fr")


@pytest.mark.parametrize("lang", _FOREIGN)
def test_foreign_rule_tables_cover_ko_tables(lang: str) -> None:
    rule_text, clauses, prefix = RULE_LANG_TABLES[lang]
    assert set(rule_text) == set(RULE_TEXT)
    assert set(clauses) == set(IRREVERSIBLE_CLAUSE)
    assert prefix
    for text in list(rule_text.values()) + list(clauses.values()) + [prefix]:
        assert not _HANGUL.search(text)
    assert set(RULE_LANG_TABLES) == set(SUPPORTED_LANGS)


@pytest.mark.parametrize("lang", _FOREIGN)
def test_compile_rules_foreign_emits_no_hangul(lang: str) -> None:
    rules = compile_rules((), lang=lang)
    assert len(rules) == len(DEFAULT_CATALOG)
    for rule in rules:
        assert not _HANGUL.search(rule.text), rule.axis


@pytest.mark.parametrize("lang", _FOREIGN)
def test_foreign_session_lands_without_hangul(lang: str, tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(store=store, land_dir=tmp_path, lang=lang)
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        assert not _HANGUL.search(snap.pair.left_text)
        assert not _HANGUL.search(snap.pair.axis_label)
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    assert not _HANGUL.search((tmp_path / XOUT_MD).read_text(encoding="utf-8"))


def test_conditional_rule_text_en() -> None:
    text = conditional_rule_text(
        "autonomy", "propose_then_act", "ask_first", lang="en"
    )
    assert text.startswith(RULE_TEXT_EN[("autonomy", "propose_then_act")])
    assert "hard-to-reverse" in text
    assert not _HANGUL.search(text)


def test_en_session_lands_english_rules(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    session = ColdOpenSession(store=store, land_dir=tmp_path, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        assert not _HANGUL.search(snap.pair.left_text)
        assert not _HANGUL.search(snap.pair.axis_label)
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    final = session.snapshot()
    assert final.session_complete
    body = (tmp_path / XOUT_MD).read_text(encoding="utf-8")
    assert body.startswith("# xout Rules")
    assert not _HANGUL.search(body)


def test_headless_pair_lang_en(capsys, tmp_path: Path) -> None:
    from xout.cli import main

    assert main(["pair", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pair"] is not None
    assert not _HANGUL.search(payload["pair"]["left_text"])
    assert not _HANGUL.search(payload["pair"]["axis_label"])


def test_why_prints_rule_body_not_none(capsys, tmp_path: Path) -> None:
    """manifest 키는 'rule'이다 - 'text'를 읽어 None이 찍히던 회귀 방지."""
    from xout.cli import main

    store = EventStore(tmp_path)
    session = ColdOpenSession(store=store, land_dir=tmp_path, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    assert main(["why", "autonomy", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "rule: None" not in out
    assert "rule: " in out
    assert not _HANGUL.search(out)


@pytest.mark.parametrize("lang", ("ja", "zh"))
def test_cjk_conditional_rule_ends_with_ideographic_full_stop(lang: str) -> None:
    """일본어/중국어 조건부 규칙은 ASCII 마침표가 아니라 '。'로 끝나야 한다."""
    text = conditional_rule_text("autonomy", "propose_then_act", "ask_first", lang=lang)
    assert text.endswith("。"), text
    assert not text.endswith("."), text
    assert " " not in text, text


@pytest.mark.parametrize("lang", ("ja", "zh"))
def test_why_grade_label_is_localized_for_cjk(capsys, lang: str, tmp_path: Path) -> None:
    from xout.cli import main
    from xout.compiler import GRADE_LABELS_BY_LANG

    store = EventStore(tmp_path)
    session = ColdOpenSession(store=store, land_dir=tmp_path, lang=lang)
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    assert main(["why", "autonomy", "--base-dir", str(tmp_path), "--lang", lang]) == 0
    out = capsys.readouterr().out
    assert "discriminated" not in out
    assert any(label in out for label in GRADE_LABELS_BY_LANG[lang].values())
    assert set(GRADE_LABELS_BY_LANG[lang]) == set(GRADE_LABELS_BY_LANG["ko"])
