"""에이전트 판정 계층 계약 - 러너는 가짜, 정규식과의 대조는 결정적, 영수증은 소유 디렉토리 안."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import EPISTEMIC_TOKENS
from xout.counter import DEFAULT_CATALOG
from xout.fixtures import SUPPORTED_LANGS
from xout.judge import (
    Candidate,
    build_prompt,
    candidates,
    glossary,
    judge,
    merge,
    parse_verdicts,
)
from xout.mine import Observation, mine
from xout.state import ColdOpenSession
from xout.store import EventStore

# 프롬프트의 줄 목록을 읽어 "ask before" 가 있는 줄은 autonomy/ask_first,
# "hands off git" 은 commit_style/no_auto_commit 으로 답하는 결정적 가짜 판정자.
FAKE_JUDGE = '''import json, re, sys
prompt = sys.argv[-1]
out = []
for m in re.finditer(r"^(\\d+)\\. (.*)$", prompt, re.M):
    n, text = int(m.group(1)), m.group(2).lower()
    if "ask before" in text or "물어" in text:
        out.append({"n": n, "axis": "autonomy", "value": "ask_first"})
    if "hands off git" in text:
        out.append({"n": n, "axis": "commit_style", "value": "no_auto_commit"})
    if "bogus" in text:
        out.append({"n": n, "axis": "autonomy", "value": "not_a_value"})
print("Here you go:\\n" + json.dumps(out))
'''


@pytest.fixture
def fake_judge(tmp_path: Path) -> str:
    script = tmp_path / "fake_judge.py"
    script.write_text(FAKE_JUDGE, encoding="utf-8")
    return f"{sys.executable} {script}"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "# Project\n"
        "Always ask before touching prod.\n"          # 정규식(ask before)도, 가짜 판정자도 잡는다
        "Commit messages follow conventional commits.\n"  # 정규식만 잡는다 -> 탈락
        "Please check with me first, then go.\n"      # 판정자만 잡는다 (표현이 달라 정규식이 놓침) - 아래 fake 규칙엔 없음
        "Generated files: hands off git for those.\n"  # 판정자만 잡는다 (정규식 표현 밖) -> 추가
        "A bogus line.\n",                              # 카탈로그 밖 값 -> 버림
        encoding="utf-8",
    )
    return root


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


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_glossary_names_every_catalog_cell_without_epistemic_words(lang: str) -> None:
    text = glossary(lang)
    for axis, values in DEFAULT_CATALOG.items():
        assert f"{axis} (" in text
        for value in values:
            assert f"  - {value}: " in text
    prompt = build_prompt([(1, "Ask before deleting anything.")], lang).lower()
    for token in EPISTEMIC_TOKENS:
        assert token.lower() not in prompt, (lang, token)
    assert '"n": 3' in prompt and "1. ask before deleting anything." in prompt


def test_parse_verdicts_keeps_only_catalog_cells_and_known_lines() -> None:
    raw = (
        "Sure! [{\"n\": 2, \"axis\": \"autonomy\", \"value\": \"ask_first\"},"
        " {\"n\": 2, \"axis\": \"autonomy\", \"value\": \"act_then_report\"},"
        " {\"n\": 9, \"axis\": \"autonomy\", \"value\": \"ask_first\"},"
        " {\"n\": 3, \"axis\": \"nope\", \"value\": \"ask_first\"},"
        " {\"n\": 3, \"axis\": \"test_discipline\", \"value\": \"test_first\"}, 7]"
    )
    verdicts = parse_verdicts(raw, [1, 2, 3])
    assert verdicts == {2: [("autonomy", "ask_first")], 3: [("test_discipline", "test_first")]}
    assert parse_verdicts("no json here", [1]) == {}
    assert parse_verdicts("[not json", [1]) == {}
    assert parse_verdicts('{"n": 1}', [1]) == {}


def test_candidates_match_mine_files_and_skip_owned_text(project: Path) -> None:
    (project / "CLAUDE.md").write_text(
        (project / "CLAUDE.md").read_text(encoding="utf-8")
        + "<!-- xout:begin sha256=abc -->\nAsk before anything.\n<!-- xout:end -->\n",
        encoding="utf-8",
    )
    found = candidates([project])
    assert [c.line_no for c in found] == [1, 2, 3, 4, 5, 6]
    assert all("xout:begin" not in c.line for c in found)
    assert all(c.abs_path.endswith("CLAUDE.md") for c in found)


def test_judge_batches_per_file_and_merge_counts_agreement(project: Path, tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(
            [
                {"n": 2, "axis": "autonomy", "value": "ask_first"},
                {"n": 5, "axis": "commit_style", "value": "no_auto_commit"},
            ]
        )

    report = judge(candidates([project]), runner, "en", ("fake",), batch=3)
    assert len(calls) == 2  # 6줄을 3줄씩
    assert {(o.line_no, o.axis, o.value) for o in report.observations} == {
        (2, "autonomy", "ask_first"),
        (5, "commit_style", "no_auto_commit"),
    }
    pattern = mine([project])
    merged, agreement, source = merge(pattern, list(report.observations))
    assert agreement == {"agreed": 1, "added": 1, "dropped": 1, "disagreed": 0}
    assert {o.line_no for o in merged} == {2, 5}
    assert source[(merged[0].abs_path, 2, "autonomy")] == "agreed"
    assert source[(merged[1].abs_path, 5, "commit_style")] == "agent"


def test_merge_prefers_agent_value_on_disagreement() -> None:
    a = Observation("autonomy", "ask_first", "CLAUDE.md", 1, "x", "/p/CLAUDE.md")
    b = Observation("autonomy", "act_then_report", "CLAUDE.md", 1, "x", "/p/CLAUDE.md")
    merged, agreement, _ = merge([a], [b])
    assert merged == [b]
    assert agreement["disagreed"] == 1 and agreement["agreed"] == 0


def test_mine_with_runner_reports_agreement_and_writes_receipt_only(
    capsys, project: Path, fake_judge: str, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    assert main(
        ["mine", str(project), "--no-user", "--base-dir", str(base), "--lang", "en",
         "--runner", fake_judge, "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    by_line = {(o["line"], o["axis"]): o for o in payload["observations"]}
    assert by_line[(2, "autonomy")]["source"] == "agreed"
    assert by_line[(5, "commit_style")]["source"] == "agent"
    assert (3, "commit_style") not in by_line  # 정규식만 잡은 줄은 탈락
    assert payload["agent"]["agreement"] == {"agreed": 1, "added": 1, "dropped": 1, "disagreed": 0}
    assert payload["agent"]["lines"] == 6 and payload["agent"]["files"] == 1
    receipt = Path(payload["agent"]["receipt_path"])
    assert receipt.is_file() and receipt.parent == base / "judgments"
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["kind"] == "xout_judge_receipt" and body["calls"][0]["raw"].startswith("Here you go")
    written = [p for p in base.rglob("*") if p.is_file()]
    assert written == [receipt], "판정은 영수증 외에 아무것도 쓰지 않는다"
    assert (project / "CLAUDE.md").read_text(encoding="utf-8").count("\n") == 6


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_mine_with_runner_text_output_in_every_language(
    capsys, project: Path, fake_judge: str, tmp_path: Path, lang: str
) -> None:
    assert main(
        ["mine", str(project), "--no-user", "--base-dir", str(tmp_path / "b"), "--lang", lang,
         "--runner", fake_judge]
    ) == 0
    out = capsys.readouterr().out
    assert "judgments" in out and "1" in out
    assert "{" not in out.split("\n")[0]


def test_mine_without_runner_is_unchanged(capsys, project: Path, tmp_path: Path) -> None:
    assert main(["mine", str(project), "--no-user", "--base-dir", str(tmp_path / "b"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "agent" not in payload
    assert all("source" not in o for o in payload["observations"])
    assert not (tmp_path / "b").exists()


def test_mine_with_missing_runner_exits_two(capsys, project: Path, tmp_path: Path) -> None:
    assert main(
        ["mine", str(project), "--no-user", "--base-dir", str(tmp_path / "b"),
         "--runner", "definitely-not-a-runner-xyz", "--json"]
    ) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_conflicts_with_runner_uses_agent_observations(
    capsys, project: Path, fake_judge: str, landed: Path
) -> None:
    # landed 프로필은 왼쪽 X 만 쳐서 autonomy 생존값이 ask_first 가 아니다 -> 2번 줄이 충돌.
    assert main(
        ["conflicts", str(project), "--no-user", "--base-dir", str(landed), "--lang", "en",
         "--runner", fake_judge, "--json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    lines = {(c["line"], c["axis"]) for c in payload["conflicts"]}
    assert (2, "autonomy") in lines or (5, "commit_style") in lines
    assert (3, "commit_style") not in lines
    assert payload["agent"]["agreement"]["dropped"] == 1
    assert Path(payload["agent"]["receipt_path"]).parent == landed / "judgments"


def test_candidate_dataclass_is_frozen() -> None:
    c = Candidate("a", "/a", 1, "x")
    with pytest.raises(AttributeError):
        c.line = "y"  # type: ignore[misc]
