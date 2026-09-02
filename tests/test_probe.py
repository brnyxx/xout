"""효과 탐침 계약 - 러너는 가짜, 판정은 결정적, 원장은 무접촉."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import EPISTEMIC_TOKENS, MANIFEST_JSON, XOUT_MD
from xout.fixtures import GENERIC_SKIN, load_pack
from xout.probe import RuleSpec, build_cases, build_prompt, parse_choice
from xout.state import ColdOpenSession
from xout.store import EventStore

FAKE_RUNNER = '''import sys
prompt = sys.argv[-1]
# 규칙(XOUT.md 섹션 헤딩)이 앞에 붙었으면 B, 아니면 A - 자리 편향을 흉내 낸 결정적 가짜.
print("B" if "## " in prompt else "A")
'''


@pytest.fixture
def landed(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    session = ColdOpenSession(store=EventStore(base), land_dir=base, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    return base


@pytest.fixture
def fake_runner(tmp_path: Path) -> str:
    script = tmp_path / "fake_runner.py"
    script.write_text(FAKE_RUNNER, encoding="utf-8")
    return f"{sys.executable} {script}"


def _specs(base: Path) -> dict[str, RuleSpec]:
    manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
    return {
        e["axis"]: RuleSpec(e["value"], e.get("irreversible_value"), tuple(e["eliminated_values"]))
        for e in manifest["rules"]
    }


def test_parse_choice_is_strict_about_standalone_letters() -> None:
    assert parse_choice("A") == "A"
    assert parse_choice("**B**\n") == "B"
    assert parse_choice("I would go with option b.") == "B"
    assert parse_choice("Both are fine") is None
    assert parse_choice("") is None
    assert parse_choice("BA") is None


def test_build_cases_covers_every_measured_slot_with_mixed_order(landed: Path) -> None:
    pack = load_pack(lang="en")
    cases = build_cases(pack, _specs(landed), GENERIC_SKIN)
    expected = [(s.scene_id, a) for s in pack.scenes for a in s.slot_axes]
    assert [(c.scene_id, c.axis) for c in cases] == expected
    specs = _specs(landed)
    for case in cases:
        assert case.survivor == specs[case.axis].survivor_for(case.context)
        assert case.alternative != case.survivor
        assert {case.first, case.second} == {case.survivor, case.alternative}
        assert case.a_text != case.b_text
        assert "[User]" in case.task
        assert "{" not in case.task and "{" not in case.a_text
    orders = {case.first == case.survivor for case in cases}
    assert orders == {True, False}, "A/B 순서가 섞여야 자리 편향을 흩는다"


@pytest.mark.parametrize("lang", ("ko", "en", "ja", "zh"))
def test_prompts_carry_no_epistemic_vocabulary(landed: Path, lang: str) -> None:
    pack = load_pack(lang=lang)
    case = build_cases(pack, _specs(landed), GENERIC_SKIN)[0]
    for rules_text in (None, "# xout Rules\n\n- x\n"):
        prompt = build_prompt(case, lang, rules_text).lower()
        for token in EPISTEMIC_TOKENS:
            assert token.lower() not in prompt, (lang, token)
        assert case.a_text.lower() in prompt and case.b_text.lower() in prompt


def test_probe_writes_receipt_and_leaves_the_ledger_alone(
    capsys, landed: Path, fake_runner: str
) -> None:
    before = {
        p: p.read_bytes() for p in landed.rglob("*") if p.is_file()
    }
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", fake_runner, "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "xout_probe_receipt"
    summary = payload["summary"]
    assert summary["cases"] == 15 and summary["unparsed"] == 0
    # 가짜 러너: 규칙 없이는 A, 규칙 앞세우면 B → 생존값이 B 자리였던 케이스만 유지+이동.
    shown_b = [o for o in payload["outcomes"] if o["shown_as_a"] != o["survivor"]]
    assert summary["held"] == len(shown_b) == summary["moved"]
    assert summary["bare_matched"] == 15 - len(shown_b)
    for o in payload["outcomes"]:
        assert o["bare"]["raw"].strip() == "A" and o["ruled"]["raw"].strip() == "B"
    receipt = Path(payload["receipt_path"])
    assert receipt.is_file() and receipt.parent == landed / "probes"
    assert json.loads(receipt.read_text(encoding="utf-8"))["summary"] == summary
    after = {p: p.read_bytes() for p in landed.rglob("*") if p.is_file() and receipt != p}
    assert after == before, "탐침은 영수증 외에 아무것도 쓰지 않는다"
    assert (landed / XOUT_MD).read_bytes() == before[landed / XOUT_MD]


def test_probe_text_output_and_quick_mode(capsys, landed: Path, fake_runner: str) -> None:
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", fake_runner, "--quick"]
    ) == 0
    out = capsys.readouterr().out
    assert "Probing 8 cases" in out
    assert "rule held" in out and "receipt:" in out


def test_probe_dry_run_calls_no_runner(capsys, landed: Path) -> None:
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", "/nonexistent/runner", "--dry-run", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["cases"]) == 15
    assert "A)" in payload["prompt_sample"] and "B)" in payload["prompt_sample"]
    assert not (landed / "probes").exists()


def test_probe_missing_runner_exits_two(capsys, landed: Path) -> None:
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", "definitely-not-a-binary-xyz"]
    ) == 2
    assert "Cannot start the runner" in capsys.readouterr().out


def test_probe_without_rules_exits_one(capsys, tmp_path: Path) -> None:
    assert main(["probe", "--base-dir", str(tmp_path / "empty"), "--lang", "en"]) == 1
    assert "run xout first" in capsys.readouterr().out


def test_alternative_is_the_strongest_contrast_for_every_catalog_cell() -> None:
    from xout.counter import DEFAULT_CATALOG
    from xout.probe import OPPOSITE, _alternative

    for axis, values in DEFAULT_CATALOG.items():
        for value in values:
            assert (axis, value) in OPPOSITE, (axis, value)
            assert OPPOSITE[(axis, value)] in values and OPPOSITE[(axis, value)] != value
            spec = RuleSpec(value=value, irreversible_value=None, eliminated=())
            assert _alternative(axis, value, spec, "routine") == OPPOSITE[(axis, value)]
    # 양립 가능한 쌍은 대안으로 쓰지 않는다
    assert OPPOSITE[("dependency_policy", "ask_first")] == "free"
    assert OPPOSITE[("dependency_policy", "prefer_existing")] == "free"
