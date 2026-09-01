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
