"""전수 감사(2026-09-02)에서 재현된 결함들의 회귀 테스트 - 각 항목은 먼저 실패했다."""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from xout.cli import main
from xout.state import ColdOpenSession
from xout.store import EventStore

ROOT = Path(__file__).resolve().parents[1]
_HANGUL = re.compile(r"[가-힣]")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(tmp_path)
    return fake_home


def _land(base: Path, lang: str = "en") -> None:
    session = ColdOpenSession(store=EventStore(base), land_dir=base, lang=lang)
    while True:
        snap = session.snapshot()
        if snap.session_complete or snap.pair is None:
            break
        session.strike("left", expected_pair_id=snap.pair.pair_id)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


# 1. XOUT.md 없이 enable하면 어떤 타깃도 건드리지 않는다
def test_enable_without_landed_rules_touches_nothing(home: Path, tmp_path: Path) -> None:
    base = tmp_path / "base"
    claude_md = home / ".claude" / "CLAUDE.md"
    before = claude_md.read_bytes()
    code, _, _ = _run(["enable", "--base-dir", str(base), "--grant", "--target", "claude", "codex", "--lang", "en"])
    assert code == 1
    assert claude_md.read_bytes() == before
    assert not (home / ".codex" / "AGENTS.md").exists()


# 2. 한 줄이 같은 축의 두 값에 걸리면 관측을 버린다 / "tests first"는 test_first
def test_mine_drops_ambiguous_lines_and_reads_tests_first(tmp_path: Path) -> None:
    from xout.mine import mine

    project = tmp_path / "p"
    project.mkdir()
    (project / "AGENTS.md").write_text(
        "Write tests only when asked.\n"
        "Retry a transient failure once. If it still fails, stop and report with the raw log.\n"
        "Always write tests first.\n",
        encoding="utf-8",
    )
    observations = mine([project])
    by_line = {}
    for o in observations:
        if o.axis in ("test_discipline", "error_behavior"):
            by_line.setdefault(o.line_no, set()).add((o.axis, o.value))
    assert by_line.get(1, set()) == {("test_discipline", "on_request")}
    assert ("error_behavior", "stop_and_report") not in by_line.get(2, set())
    assert ("error_behavior", "retry_then_report") in by_line.get(2, set())
    assert by_line.get(3, set()) == {("test_discipline", "test_first")}


# 3. export는 세션 언어를 따른다
def test_export_follows_lang(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _land(base, "en")
    code, out, _ = _run(["export", "--base-dir", str(base), "--lang", "en"])
    assert code == 0
    assert out.startswith("# xout Rules")
    assert not _HANGUL.search(out)


# 4. pair JSON의 rules[].axis_label도 세션 언어
def test_pair_rules_axis_labels_follow_lang(tmp_path: Path) -> None:
    base = tmp_path / "base"
    code, out, _ = _run(["pair", "--base-dir", str(base), "--lang", "en", "--no-user"])
    assert code == 0
    payload = json.loads(out)
    for rule in payload["rules"]:
        assert not _HANGUL.search(rule["axis_label"]), rule


# 5. status/enable/undo 사람용 출력은 stdout이고 언어를 따른다
def test_status_prints_to_stdout_in_session_lang(home: Path, tmp_path: Path, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    base = tmp_path / "base"
    _land(base, "en")
    code, out, err = _run(["status", "--base-dir", str(base), "--lang", "en"])
    assert code == 0
    assert out.strip(), "status must print its report to stdout"
    assert not _HANGUL.search(out + err), out + err
    code, out, _ = _run(["status", "--base-dir", str(base), "--lang", "en", "--json"])
    payload = json.loads(out)
    assert not _HANGUL.search(json.dumps(payload, ensure_ascii=False))
    code, out, err = _run(["enable", "--base-dir", str(base), "--grant", "--lang", "en"])
    assert code == 0 and not _HANGUL.search(out + err)
    code, out, err = _run(["undo", "--base-dir", str(base), "--lang", "en"])
    assert code == 0 and not _HANGUL.search(out + err)
    logged = [r.getMessage() for r in caplog.records if _HANGUL.search(r.getMessage())]
    assert logged == [], logged


def test_landing_log_line_has_no_hangul(tmp_path: Path, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    _land(tmp_path / "base", "en")
    assert not any(_HANGUL.search(r.getMessage()) for r in caplog.records), [
        r.getMessage() for r in caplog.records if _HANGUL.search(r.getMessage())
    ]


# 6. 완주한 뒤의 pair는 새 세션을 열지 않고 완료를 알린다
def test_pair_after_completion_reports_complete_unless_new(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _land(base, "en")
    store = EventStore(base)
    before = len(store.session_ids())
    code, out, _ = _run(["pair", "--base-dir", str(base), "--lang", "en", "--no-user"])
    assert code == 0
    payload = json.loads(out)
    assert payload["pair"] is None and payload["session_complete"] is True
    assert len(EventStore(base).session_ids()) == before
    code, out, _ = _run(["pair", "--base-dir", str(base), "--lang", "en", "--no-user", "--new"])
    assert code == 0 and json.loads(out)["pair"] is not None
    assert len(EventStore(base).session_ids()) == before + 1


# 7/9. 배포 메타는 4개 README와 테스트 하네스를 안다, 무거운 GIF는 sdist에 넣지 않는다
def test_distribution_manifests_know_every_readme() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    for name in ("README.md", "README.ko.md", "README.ja.md", "README.zh.md"):
        assert f"include {name}" in manifest, name
        assert name in package["files"], name
    assert "recursive-include tests *.py" in manifest
    assert "recursive-exclude .github/assets *.gif" in manifest
    assert "recursive-include .github/assets *.gif" not in manifest


# 8. 잠금 파일은 소유 디렉토리 밖에 남지 않는다
def test_no_lock_files_left_outside_owned_dir(home: Path, tmp_path: Path) -> None:
    base = home / ".claude" / "xout"
    _land(base, "en")
    assert _run(["enable", "--base-dir", str(base), "--grant", "--lang", "en"])[0] == 0
    assert _run(["undo", "--base-dir", str(base), "--lang", "en"])[0] == 0
    strays = [
        p for p in home.rglob("*")
        if p.is_file() and ".lock" in p.name and base not in p.parents
    ]
    assert strays == [], strays


# 13. version은 공통 플래그를 받는다
def test_version_accepts_lang(tmp_path: Path) -> None:
    code, out, _ = _run(["version", "--lang", "en", "--base-dir", str(tmp_path)])
    assert code == 0 and out.strip()


# 12. Claude 전용 문구 잔재
def test_plugin_description_is_tool_neutral() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "Claude Code behavior" not in plugin["description"]


# 10. CHANGELOG는 존재하는 자산만 가리킨다
def test_changelog_points_at_existing_assets() -> None:
    body = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "how-it-works*.svg" not in body


# 17. doctor --json은 stderr에 사람용 로그를 찍지 않는다
def test_doctor_json_keeps_stderr_quiet(tmp_path: Path) -> None:
    code, out, err = _run(["doctor", "--base-dir", str(tmp_path / "base"), "--json", "--lang", "en"])
    json.loads(out)
    assert "봉인" not in err
