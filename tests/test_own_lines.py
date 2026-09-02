"""사용자가 자기 말로 적은 줄 - fold, 렌더, CLI 계약.

8축 카탈로그는 얼어 있고 이 줄들은 그 바깥에 산다. xout이 문안을 고르지 않으므로
측정 대상도 아니다: 페어도, 등급도, 반증 이력도 붙지 않는다.

EPISTEMIC_TOKENS 가드는 xout 자신이 쓴 골격 문구에만 걸린다. 사용자가 적은
문장은 원문 그대로 실리며 검사하지 않는다 - 아래
test_a_user_line_is_verbatim_and_never_scanned가 그 경계를 고정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import (
    EPISTEMIC_TOKENS,
    MANIFEST_JSON,
    XOUT_DOC,
    XOUT_MD,
    compile_rules,
    render_xout_md,
)
from xout.events import Event, EventType
from xout.exporter import render_export
from xout.fixtures import SUPPORTED_LANGS
from xout.own import (
    MAX_LENGTH,
    OwnLine,
    OwnLineError,
    added_event,
    dropped_event,
    fold_own_lines,
    normalize,
)
from xout.state import ColdOpenSession
from xout.store import EventStore

LANGS = tuple(SUPPORTED_LANGS)


def _land(base: Path, lang: str = "ko") -> EventStore:
    store = EventStore(base)
    session = ColdOpenSession(store=store, land_dir=base, lang=lang)
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    return store


def _added(text: str, existing: tuple[OwnLine, ...] = ()) -> Event:
    return added_event(text, existing, now="2026-09-02T00:00:00+00:00")


# ---------------------------------------------------------------------------
# fold - 추가에서 tombstone을 뺀 순수 파생
# ---------------------------------------------------------------------------


def test_lines_fold_in_insertion_order() -> None:
    stream: list[Event] = []
    for text in ("첫 줄", "둘째 줄", "셋째 줄"):
        stream.append(_added(text, fold_own_lines(stream)))
    assert [line.text for line in fold_own_lines(stream)] == ["첫 줄", "둘째 줄", "셋째 줄"]


def test_a_tombstone_removes_exactly_one_line_and_keeps_the_rest_in_order() -> None:
    stream: list[Event] = []
    for text in ("첫 줄", "둘째 줄", "셋째 줄"):
        stream.append(_added(text, fold_own_lines(stream)))
    middle = fold_own_lines(stream)[1]
    event, dropped = dropped_event(middle.line_id, fold_own_lines(stream))
    stream.append(event)
    assert dropped.text == "둘째 줄"
    assert [line.text for line in fold_own_lines(stream)] == ["첫 줄", "셋째 줄"]


def test_fold_ignores_a_tombstone_for_an_unknown_id() -> None:
    stream = [_added("한 줄")]
    stream.append(
        Event(
            type=EventType.OWN_LINE_DROPPED,
            session_id="own-lines",
            payload={"id": "deadbeef"},
        )
    )
    assert len(fold_own_lines(stream)) == 1


def test_fold_ignores_events_that_are_not_own_lines(tmp_path: Path) -> None:
    store = _land(tmp_path)
    assert fold_own_lines(store.load_all()) == ()


# ---------------------------------------------------------------------------
# 입력 계약 - 1..240자, 한 줄, 중복 금지
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    (
        ("", "empty"),
        ("   ", "empty"),
        ("\n", "empty"),
        ("첫 줄\n둘째 줄", "multiline"),
        ("x" * (MAX_LENGTH + 1), "too_long"),
    ),
)
def test_refused_inputs_carry_a_reason(text: str, code: str) -> None:
    with pytest.raises(OwnLineError) as exc:
        _added(text)
    assert exc.value.code == code


def test_the_longest_allowed_line_is_accepted_and_trimmed() -> None:
    event = _added("  " + "x" * MAX_LENGTH + "  ")
    assert event.payload["text"] == "x" * MAX_LENGTH


def test_the_same_sentence_twice_is_refused_across_case_and_spacing() -> None:
    first = _added("Never  Force  Push")
    existing = fold_own_lines([first])
    with pytest.raises(OwnLineError) as exc:
        _added("never force push", existing)
    assert exc.value.code == "duplicate"
    assert normalize("Never  Force  Push") == normalize("never force push")


def test_a_dropped_line_can_be_written_again() -> None:
    stream = [_added("never force push")]
    event, _ = dropped_event(fold_own_lines(stream)[0].line_id, fold_own_lines(stream))
    stream.append(event)
    stream.append(_added("never force push", fold_own_lines(stream)))
    assert [line.text for line in fold_own_lines(stream)] == ["never force push"]


def test_dropping_an_unknown_id_is_refused() -> None:
    with pytest.raises(OwnLineError) as exc:
        dropped_event("nope", fold_own_lines([_added("한 줄")]))
    assert exc.value.code == "unknown_id"


# ---------------------------------------------------------------------------
# 원장 - 덧붙이기만 한다
# ---------------------------------------------------------------------------


def test_the_ledger_only_grows_by_the_new_events(tmp_path: Path, capsys) -> None:
    store = _land(tmp_path)
    session_files = {
        path: path.read_bytes() for path in sorted((tmp_path / "sessions").iterdir())
    }
    assert main(["own", "add", "커밋 전에 항상 로컬에서 전체 테스트를 돌린다", "--base-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    for path, body in session_files.items():
        assert path.read_bytes() == body, path.name

    own_path = tmp_path / "sessions" / "own-lines.jsonl"
    first = own_path.read_bytes()
    line_id = fold_own_lines(store.load_all())[0].line_id
    assert main(["own", "drop", line_id, "--base-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    after = own_path.read_bytes()
    assert after.startswith(first)
    records = [json.loads(line) for line in after.decode("utf-8").splitlines()]
    assert [record["type"] for record in records] == ["own_line_added", "own_line_dropped"]
    assert records[1]["payload"] == {"id": line_id}


# ---------------------------------------------------------------------------
# XOUT.md 렌더
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_section_renders_verbatim_in_every_language(lang: str) -> None:
    doc = XOUT_DOC[lang]
    lines = fold_own_lines([_added("스테이징 DB에는 절대 붙지 않는다")])
    body = render_xout_md(compile_rules((), lang=lang), lang, lines)
    assert doc["own"] in body
    assert doc["own_intro"] in body
    assert "- 스테이징 DB에는 절대 붙지 않는다" in body
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings[-1] == doc["own"], headings


@pytest.mark.parametrize("lang", LANGS)
def test_without_own_lines_the_output_is_byte_identical(lang: str) -> None:
    rules = compile_rules((), lang=lang)
    assert render_xout_md(rules, lang, ()) == render_xout_md(rules, lang)
    assert XOUT_DOC[lang]["own"] not in render_xout_md(rules, lang)


@pytest.mark.parametrize("lang", LANGS)
def test_the_skeleton_wording_stays_free_of_epistemic_tokens(lang: str) -> None:
    joined = (XOUT_DOC[lang]["own"] + " " + XOUT_DOC[lang]["own_intro"]).lower()
    for token in EPISTEMIC_TOKENS:
        assert token.lower() not in joined, (lang, token)


def test_a_user_line_is_verbatim_and_never_scanned() -> None:
    """가드는 xout이 쓴 골격 문구에만 걸린다 - 사용자 문장은 검사 대상이 아니다."""
    sentence = "내 가설은 내가 알아서 검증한다"
    assert any(token in sentence for token in EPISTEMIC_TOKENS)
    body = render_xout_md(compile_rules(()), "ko", fold_own_lines([_added(sentence)]))
    assert f"- {sentence}" in body


# ---------------------------------------------------------------------------
# 착지 - manifest와 XOUT.md가 같이 움직인다
# ---------------------------------------------------------------------------


def test_manifest_carries_an_empty_list_without_own_lines(tmp_path: Path) -> None:
    _land(tmp_path)
    manifest = json.loads((tmp_path / MANIFEST_JSON).read_text(encoding="utf-8"))
    assert manifest["own_lines"] == []


def test_landing_folds_the_lines_into_both_outputs(tmp_path: Path, capsys) -> None:
    store = _land(tmp_path)
    store.append(_added("리뷰 없이는 main에 올리지 않는다"))
    assert main(["land", "--base-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    manifest = json.loads((tmp_path / MANIFEST_JSON).read_text(encoding="utf-8"))
    assert [entry["text"] for entry in manifest["own_lines"]] == ["리뷰 없이는 main에 올리지 않는다"]
    assert set(manifest["own_lines"][0]) == {"id", "text", "created_at"}
    assert "- 리뷰 없이는 main에 올리지 않는다" in (tmp_path / XOUT_MD).read_text(encoding="utf-8")


def test_export_renders_the_section_too(tmp_path: Path) -> None:
    store = _land(tmp_path)
    store.append(_added("리뷰 없이는 main에 올리지 않는다"))
    events = store.load_completed()
    assert "- 리뷰 없이는 main에 올리지 않는다" in render_export(events, "markdown")
    assert XOUT_DOC["ko"]["own"] in render_export(events, "agents")
    payload = json.loads(render_export(events, "json"))
    assert [entry["text"] for entry in payload["own_lines"]] == ["리뷰 없이는 main에 올리지 않는다"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_add_before_any_session_stores_the_line_and_says_so(tmp_path: Path, capsys) -> None:
    assert main(["own", "add", "no force pushes", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "lands with the first one you complete" in out
    assert not (tmp_path / XOUT_MD).exists()
    assert [line.text for line in fold_own_lines(EventStore(tmp_path).load_all())] == ["no force pushes"]


def test_add_relands_and_list_and_drop_round_trip(tmp_path: Path, capsys) -> None:
    _land(tmp_path)
    assert main(["own", "add", "no force pushes", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    assert "rewrote XOUT.md" in capsys.readouterr().out
    landed = (tmp_path / XOUT_MD).read_text(encoding="utf-8")
    assert "- no force pushes" in landed

    assert main(["own", "list", "--base-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact"] == "popper_own_lines"
    assert [entry["text"] for entry in payload["own_lines"]] == ["no force pushes"]
    line_id = payload["own_lines"][0]["id"]

    assert main(["own", "list", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    assert f"- [{line_id}] no force pushes" in capsys.readouterr().out

    assert main(["own", "drop", line_id, "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    capsys.readouterr()
    assert "- no force pushes" not in (tmp_path / XOUT_MD).read_text(encoding="utf-8")
    assert XOUT_DOC["ko"]["own"] not in (tmp_path / XOUT_MD).read_text(encoding="utf-8")


def test_add_reports_a_refusal_without_writing(tmp_path: Path, capsys) -> None:
    _land(tmp_path)
    assert main(["own", "add", "no force pushes", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    capsys.readouterr()
    assert main(["own", "add", "  NO FORCE PUSHES ", "--base-dir", str(tmp_path), "--lang", "en"]) == 1
    assert "already wrote that one" in capsys.readouterr().out
    assert len(fold_own_lines(EventStore(tmp_path).load_all())) == 1

    assert main(["own", "add", "x" * (MAX_LENGTH + 1), "--base-dir", str(tmp_path), "--lang", "en"]) == 1
    assert "too long" in capsys.readouterr().out
    assert main(["own", "drop", "nope", "--base-dir", str(tmp_path), "--lang", "en"]) == 1
    assert "no such line" in capsys.readouterr().out


def test_list_is_empty_before_anything_is_written(tmp_path: Path, capsys) -> None:
    assert main(["own", "list", "--base-dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["own_lines"] == []


@pytest.mark.parametrize("lang", LANGS)
def test_every_language_answers_the_own_commands(lang: str, tmp_path: Path, capsys) -> None:
    base = tmp_path / lang
    base.mkdir()
    assert main(["own", "add", "sentence one", "--base-dir", str(base), "--lang", lang]) == 0
    assert main(["own", "list", "--base-dir", str(base), "--lang", lang]) == 0
    out = capsys.readouterr().out
    assert "sentence one" in out
    assert "{" not in out, out


def test_status_counts_the_lines(tmp_path: Path, capsys) -> None:
    _land(tmp_path)
    assert main(["own", "add", "no force pushes", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    capsys.readouterr()
    assert main(["status", "--base-dir", str(tmp_path), "--lang", "en"]) == 0
    assert "your own lines: 1" in capsys.readouterr().out
    assert main(["status", "--base-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["text"] for entry in payload["own_lines"]] == ["no force pushes"]
