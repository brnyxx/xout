"""탐침 강화 모드 계약 - 반복 다수결, 방해 문서, 타깃 파일 경유 전달."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import XOUT_MD
from xout.probe import ProbeCase, ProbeOutcome, build_prompt, majority, probe
from xout.state import ColdOpenSession
from xout.store import EventStore
from xout.targets import find_block

# 호출 횟수를 파일에 세고, 세 번째 호출마다 다른 답을 내는 가짜 - 다수결을 검사한다.
FLAKY_RUNNER = '''import sys
from pathlib import Path
counter = Path(sys.argv[1])
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
prompt = sys.argv[-1]
ruled = "## " in prompt
# 규칙이 있으면 대체로 B, 3회 중 1회는 A - 규칙 없으면 항상 A
print("A" if (not ruled or n % 3 == 0) else "B")
'''

# 규칙 파일(타깃)에 xout 블록이 있는지를 보고 답하는 가짜 - 프롬프트에는 규칙이 없다.
FILE_RUNNER = '''import sys
from pathlib import Path
target = Path(sys.argv[1])
prompt = sys.argv[-1]
assert "## " not in prompt, "rules must not be in the prompt in via-target mode"
present = target.exists() and "<!-- xout:begin" in target.read_text(encoding="utf-8")
print("B" if present else "A")
'''

# 방해 문서가 앞에 있는지 확인하는 가짜.
CONTEXT_RUNNER = '''import sys
prompt = sys.argv[-1]
assert prompt.startswith("Instructions already in place"), prompt[:60]
assert "DISTRACTOR-MARK" in prompt
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


def _script(tmp_path: Path, name: str, body: str, *args: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return " ".join([sys.executable, str(script), *args])


def test_majority_ignores_unparsed_and_refuses_ties() -> None:
    assert majority(["x", "x", "y"]) == "x"
    assert majority(["x", None, None]) == "x"
    assert majority(["x", "y"]) is None
    assert majority([None, None]) is None
    assert majority([]) is None


def test_outcome_trial_accounting() -> None:
    case = ProbeCase("s", "routine", "autonomy", "ask_first", "act_then_report",
                     "ask_first", "act_then_report", "task", "a", "b")
    outcome = ProbeOutcome(case, ("A", "A"), ("A", "B", "A"))
    assert outcome.trials == 3 and outcome.held_trials == 2
    assert outcome.held and not outcome.held_every_trial and not outcome.moved
    assert outcome.bare_raw == "A" and outcome.ruled_raw == "A"
    payload = outcome.to_dict()
    assert payload["ruled"]["raws"] == ["A", "B", "A"] and payload["trials"] == 3


def test_repeat_runs_every_trial_and_reports_by_trial(capsys, landed: Path, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    runner = _script(tmp_path, "flaky.py", FLAKY_RUNNER, str(counter))
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--quick", "--axes", "autonomy", "--repeat", "3", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repeat"] == 3 and payload["delivery"] == "prompt"
    assert int(counter.read_text()) == 6  # 1 케이스 x (3 bare + 3 ruled)
    (outcome,) = payload["outcomes"]
    assert outcome["trials"] == 3 and len(outcome["ruled"]["raws"]) == 3
    assert payload["summary"]["trials"] == 3
    assert payload["summary"]["trials_held"] == outcome["held_trials"]


def test_repeat_text_output_mentions_trials(capsys, landed: Path, tmp_path: Path) -> None:
    runner = _script(tmp_path, "flaky.py", FLAKY_RUNNER, str(tmp_path / "count"))
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--quick", "--axes", "autonomy", "--repeat", "3"]
    ) == 0
    out = capsys.readouterr().out
    assert "x 3 trials" in out and "by trial" in out and "held every trial" in out


def test_context_file_is_prepended_to_both_prompts(capsys, landed: Path, tmp_path: Path) -> None:
    distractor = tmp_path / "PROJECT.md"
    distractor.write_text("# Project rules\nDISTRACTOR-MARK\nUse tabs.\n", encoding="utf-8")
    runner = _script(tmp_path, "ctx.py", CONTEXT_RUNNER)
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--quick", "--axes", "autonomy", "--context-file", str(distractor), "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["context_sha256"] and payload["summary"]["unparsed"] == 0
    case = ProbeCase("s", "routine", "autonomy", "a", "b", "a", "b", "task", "x", "y")
    prompt = build_prompt(case, "en", "## rules", "CTX")
    assert prompt.index("CTX") < prompt.index("## rules") < prompt.index("task")


def test_via_target_toggles_the_real_block_and_restores_it(
    capsys, landed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    target_file = home / ".codex" / "AGENTS.md"
    runner = _script(tmp_path, "file.py", FILE_RUNNER, str(target_file))

    # 활성 상태가 아니면 거절한다.
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--quick", "--axes", "autonomy", "--via-target", "codex", "--json"]
    ) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "target_not_active"

    assert main(["enable", "--grant", "--target", "codex", "--base-dir", str(landed)]) == 0
    capsys.readouterr()
    before = target_file.read_text(encoding="utf-8")
    assert find_block(before) is not None

    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--quick", "--axes", "autonomy", "scope_adherence", "--via-target", "codex", "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"] == "target:codex"
    for outcome in payload["outcomes"]:
        assert outcome["bare"]["raw"].strip() == "A"   # 블록을 뺀 상태에서 물었다
        assert outcome["ruled"]["raw"].strip() == "B"  # 블록을 다시 넣은 상태에서 물었다
    assert target_file.read_text(encoding="utf-8") == before, "끝나면 원래 파일로 돌아온다"
    assert (landed / XOUT_MD).is_file()
    savepoints = list((landed / "savepoints").iterdir())
    assert savepoints, "타깃을 건드리기 전에 세이브포인트를 남긴다"


def test_via_target_restores_even_when_the_runner_fails(
    capsys, landed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    assert main(["enable", "--grant", "--target", "opencode", "--base-dir", str(landed)]) == 0
    capsys.readouterr()
    target_file = home / ".config" / "opencode" / "AGENTS.md"
    before = target_file.read_text(encoding="utf-8")
    crash = _script(tmp_path, "crash.py", "import sys; sys.exit(3)\n")
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", crash,
         "--quick", "--axes", "autonomy", "--via-target", "opencode", "--json"]
    ) == 2
    assert target_file.read_text(encoding="utf-8") == before


def test_via_target_unknown_id(capsys, landed: Path, tmp_path: Path) -> None:
    runner = _script(tmp_path, "any.py", "print('A')\n")
    assert main(
        ["probe", "--base-dir", str(landed), "--lang", "en", "--runner", runner,
         "--via-target", "nope", "--json"]
    ) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_target"


def test_probe_two_pass_order_with_phase_hook() -> None:
    phases: list[str] = []
    seen: list[str] = []
    case = ProbeCase("s", "routine", "autonomy", "a", "b", "a", "b", "task", "x", "y")

    def runner(prompt: str) -> str:
        seen.append(phases[-1])
        return "A"

    report = probe(
        [case, case], "## r", runner, "en", ("fake",),
        rules_in_prompt=False, phase_hook=phases.append, delivery="target:t", repeat=2,
    )
    assert phases == ["bare", "ruled"]
    assert seen == ["bare"] * 4 + ["ruled"] * 4
    assert report.delivery == "target:t" and report.summary["trials"] == 4
