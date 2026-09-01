"""로컬 채굴(xout mine) 계약.

읽기전용 스캔이 규칙 파일의 줄을 8축으로 귀속시키고, 모든 관측이 file:line
영수증을 동반하며, 아무 파일도 쓰지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from xout.cli import main
from xout.counter import DEFAULT_CATALOG
from xout.mine import MINED_PATTERNS, mine, summarize


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.md").write_text(
        "\n".join(
            [
                "# rules",
                "Ask for approval before modifying code.",
                "Never commit unless explicitly asked.",
                "Avoid comments that restate the code.",
                "Run the tests before submitting.",
                "Do not add new dependencies without asking.",
            ]
        ),
        encoding="utf-8",
    )
    sub = root / "packages" / "web"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        "테스트를 먼저 작성하고 실패를 확인한다.\n요청받은 범위 밖의 관련 없는 파일은 수정하지 않는다.\n",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "CLAUDE.md").write_text(
        "Ask for approval before anything.", encoding="utf-8"
    )
    return root


def test_mine_attributes_lines_with_receipts(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    observations = mine([root])
    assert observations, "관측이 하나도 없다"
    keyed = {(obs.axis, obs.value) for obs in observations}
    assert ("autonomy", "ask_first") in keyed
    assert ("commit_style", "no_auto_commit") in keyed
    assert ("comment_doc", "minimal") in keyed
    assert ("verification", "always_run") in keyed
    assert ("dependency_policy", "ask_first") in keyed
    assert ("test_discipline", "test_first") in keyed
    assert ("scope_adherence", "strict") in keyed
    for obs in observations:
        assert obs.line_no >= 1
        assert obs.path
        assert "node_modules" not in obs.path


def test_mine_is_read_only_and_deterministic(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = sorted(str(p) for p in root.rglob("*"))
    first = mine([root])
    second = mine([root])
    assert [o.to_dict() for o in first] == [o.to_dict() for o in second]
    assert sorted(str(p) for p in root.rglob("*")) == before


def test_summarize_covers_all_axes(tmp_path: Path) -> None:
    counts = summarize(mine([_tree(tmp_path)]))
    assert set(counts) == set(DEFAULT_CATALOG)
    for axis, values in DEFAULT_CATALOG.items():
        assert set(counts[axis]) == set(values)


def test_patterns_only_target_catalog_cells() -> None:
    for axis, value in MINED_PATTERNS:
        assert value in DEFAULT_CATALOG[axis], (axis, value)


def test_cli_mine_json(capsys, tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert (
        main(
            [
                "mine",
                str(root),
                "--json",
                "--base-dir",
                str(tmp_path / "base"),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["observations"]
    assert set(payload["summary"]) == set(DEFAULT_CATALOG)


def test_cli_mine_empty_root(capsys, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        main(["mine", str(empty), "--base-dir", str(tmp_path / "base")]) == 0
    )
    out = capsys.readouterr().out
    assert "관측" in out or "observation" in out


# ---------------------------------------------------------------------------
# 프로젝트 규칙 파일과의 충돌 - 두 맥락 어느 쪽 생존값도 아닌 관측만
# ---------------------------------------------------------------------------


def test_find_conflicts_respects_both_context_survivors() -> None:
    from xout.mine import Observation, find_conflicts

    observations = [
        Observation("autonomy", "ask_first", "CLAUDE.md", 3, "Always ask for approval before editing."),
        Observation("autonomy", "act_then_report", "CLAUDE.md", 4, "Act first."),
        Observation("commit_style", "narrative", "AGENTS.md", 9, "narrative commit"),
        Observation("scope_adherence", "strict", "AGENTS.md", 1, "stay in scope"),
    ]
    rules = {
        "autonomy": ("act_then_report", "ask_first"),  # 조건부: 두 값 모두 생존
        "commit_style": ("conventional", None),
    }
    conflicts = find_conflicts(observations, rules)
    assert [(c.axis, c.observed_value, c.rule_value, c.line_no) for c in conflicts] == [
        ("commit_style", "narrative", "conventional", 9)
    ]
    assert conflicts[0].to_dict()["path"] == "AGENTS.md"


_AUTONOMY_LINES = {
    "ask_first": "Always ask for approval before editing.",
    "propose_then_act": "Write the plan first, then act.",
    "act_then_report": "Act first, then report.",
}


def test_conflicts_command_reports_file_line_against_landed_rules(
    capsys, tmp_path: Path
) -> None:
    import json

    from xout.cli import main
    from xout.compiler import MANIFEST_JSON
    from xout.state import ColdOpenSession
    from xout.store import EventStore

    base = tmp_path / "base"
    session = ColdOpenSession(store=EventStore(base), land_dir=base, lang="en")
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)
    manifest = json.loads((base / MANIFEST_JSON).read_text(encoding="utf-8"))
    autonomy = next(r for r in manifest["rules"] if r["axis"] == "autonomy")
    kept = {autonomy["value"], autonomy.get("irreversible_value")}
    disagreeing = next(v for v in _AUTONOMY_LINES if v not in kept)
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# rules\n" + _AUTONOMY_LINES[disagreeing] + "\n", encoding="utf-8"
    )

    assert main(["conflicts", str(project), "--base-dir", str(base), "--json", "--no-user"]) == 0
    payload = json.loads(capsys.readouterr().out)
    hit = [c for c in payload["conflicts"] if c["axis"] == "autonomy"]
    assert hit and hit[0]["observed_value"] == disagreeing
    assert hit[0]["line"] == 2 and hit[0]["path"].endswith("CLAUDE.md")

    (project / "CLAUDE.md").write_text(
        "# rules\n" + _AUTONOMY_LINES[autonomy["value"]] + "\n", encoding="utf-8"
    )
    assert main(["conflicts", str(project), "--base-dir", str(base), "--lang", "en", "--no-user"]) == 0
    out = capsys.readouterr().out
    assert "disagree" not in out or "No project rule file disagrees" in out


def test_conflicts_command_without_rules_exits_one(capsys, tmp_path: Path) -> None:
    from xout.cli import main

    assert main(["conflicts", str(tmp_path), "--base-dir", str(tmp_path / "nothing"), "--lang", "en"]) == 1
    assert "run xout first" in capsys.readouterr().out


def test_mine_skips_xout_owned_blocks_and_import_line(tmp_path: Path) -> None:
    """xout이 직접 쓴 블록/import 줄을 다시 채굴해 자기 규칙을 중복·모순으로 보고하면 안 된다."""
    from xout.targets import render_block

    project = tmp_path / "p"
    project.mkdir()
    own = render_block("# xout Rules\n\n- Always ask for approval before editing.\n- Act first, then report. (the user rejected: prefer_existing)\n")
    (project / "AGENTS.md").write_text(
        "Write the plan first, then act.\n\n" + own + "\n\nnarrative commit\n@~/.claude/xout/XOUT.md\n",
        encoding="utf-8",
    )
    observations = mine([project])
    assert [(o.value, o.line_no) for o in observations] == [("propose_then_act", 1), ("narrative", 11)]
