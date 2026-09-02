"""규칙 감사 계약 - 러너는 가짜, 판정은 결정적, 규칙 파일과 원장은 무접촉."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from xout.audit import (
    VERDICT_DEFAULT,
    VERDICT_EFFECTIVE,
    VERDICT_IGNORED,
    VERDICT_UNCLEAR,
    Scene,
    build_ask_prompt,
    build_clash_prompt,
    build_generate_prompt,
    parse_clashes,
    parse_scenes,
    select,
)
from xout.cli import main
from xout.compiler import EPISTEMIC_TOKENS
from xout.fixtures import SUPPORTED_LANGS
from xout.judge import Candidate, candidates

# 세 가지 호출을 한 스크립트로 흉내 낸다. 장면 생성은 "MEASURE" 표시가 있는 줄만,
# A/B 질문은 보기 문장의 FOLLOW/VIOLATE 표시와 "고정 지시" 유무로, 충돌 조회는
# "clash" 표시가 있는 줄 짝으로 답한다. 어느 언어로 물어도 답이 같다.
FAKE_RUNNER = '''import json, re, sys
from pathlib import Path

prompt = sys.argv[-1]
BEHAVIOR = {
    "default": ("follow", "follow"),
    "effective": ("violate", "follow"),
    "ignored": ("follow", "violate"),
    "clash": ("follow", "follow"),
    "flaky": ("violate", "follow"),
}


def numbered():
    return [(int(m.group(1)), m.group(2)) for m in re.finditer(r"^(\\d+)\\. (.*)$", prompt, re.M)]


if '"task"' in prompt:
    out = []
    for n, text in numbered():
        if "BOGUS" in text:
            out.append({"n": n, "task": "t", "follow": "f"})
            out.append({"n": 9999, "task": "t", "follow": "f", "violate": "v"})
            continue
        if "MEASURE" not in text:
            continue
        kind = text.split("MEASURE", 1)[1].strip().split(":")[0].strip()
        out.append({
            "n": n,
            "task": "The user asks for something on line %d." % n,
            "follow": "FOLLOW %s %d: does what the line asks for." % (kind, n),
            "violate": "VIOLATE %s %d: does the other thing instead." % (kind, n),
        })
    print("here you go:\\n" + json.dumps(out))
elif '"why"' in prompt:
    clash = [n for n, text in numbered() if "clash" in text]
    pairs = [{"a": 4321, "b": clash[0], "why": "line nobody sent"}] if clash else []
    if len(clash) > 1:
        pairs.append({"a": clash[0], "b": clash[1], "why": "one waits, one pushes"})
    print(json.dumps(pairs))
else:
    options = dict(re.findall(r"^([AB])\\) (.*)$", prompt, re.M))
    found = re.search(r"FOLLOW (\\S+) (\\d+):", prompt)
    kind, n = found.group(1), int(found.group(2))
    ruled = "MEASURE" in prompt
    if kind == "unclear":
        print("???")
    else:
        want = BEHAVIOR.get(kind, ("follow", "follow"))[1 if ruled else 0]
        if kind == "flaky" and ruled:
            store = Path(sys.argv[0] + ".count")
            seen = json.loads(store.read_text()) if store.exists() else {}
            seen[str(n)] = seen.get(str(n), 0) + 1
            store.write_text(json.dumps(seen))
            want = "violate" if seen[str(n)] == 1 else "follow"
        print([k for k, v in options.items() if v.startswith(want.upper())][0])
'''

RULES = """# Project rules

| Column | Value |
|---|---|

Be nice.

```bash
MEASURE default: this line lives inside a code block and must not be sent.
```

- MEASURE default: keep the changelog tidy in every change you make.
- MEASURE effective: run the whole test suite before you call it done.
- MEASURE ignored: never touch the production database from a local shell.
- MEASURE unclear: write commit messages in the imperative mood please.
- BOGUS: the generator answers this one badly on purpose, so it drops out.
- MEASURE clash: always ask the person before you push anything anywhere.
- MEASURE clash: push to the remote as soon as the tests are green here.
"""

SENT_LINES = (12, 13, 14, 15, 16, 17, 18)


@pytest.fixture
def fake_runner(tmp_path: Path) -> str:
    script = tmp_path / "fake_runner.py"
    script.write_text(FAKE_RUNNER, encoding="utf-8")
    return f"{sys.executable} {script}"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "CLAUDE.md").write_text(RULES, encoding="utf-8")
    return root


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "base"


def _run(base: Path, project: Path, runner: str, *extra: str) -> list[str]:
    return [
        "audit", str(project), "--base-dir", str(base), "--no-user",
        "--runner", runner, *extra,
    ]


def _scene(line: str = "Ask before you push.", first: str = "follow") -> Scene:
    return Scene(
        candidate=Candidate("CLAUDE.md", "/abs/CLAUDE.md", 3, line),
        task="The user asks you to ship the branch.",
        follow="You ask the person first, then push.",
        violate="You push straight away and mention it after.",
        first=first,
    )


def test_selection_drops_headings_fences_tables_and_short_lines(project: Path) -> None:
    selection = select(candidates([project]))
    assert [c.line_no for c in selection.items] == list(SENT_LINES)
    assert selection.scanned == 14 and selection.skipped == 7
    assert selection.over_limit == 0 and selection.files == 1
    assert all("code block" not in c.line for c in selection.items)


def test_limit_caps_what_is_sent_and_reports_the_rest(project: Path) -> None:
    selection = select(candidates([project]), limit=3)
    assert [c.line_no for c in selection.items] == [12, 13, 14]
    assert selection.over_limit == 4 and selection.skipped == 7


def test_generation_parsing_is_strict() -> None:
    raw = (
        'sure:\n[{"n": 1, "task": "t", "follow": "f", "violate": "v"},'
        ' {"n": 2, "task": "t", "follow": "f"},'          # 칸이 빈다
        ' {"n": 99, "task": "t", "follow": "f", "violate": "v"},'   # 모르는 번호
        ' {"n": 3, "task": " ", "follow": "f", "violate": "v"},'    # 빈 문자열
        ' "nope",'                                                  # dict가 아니다
        ' {"n": 1, "task": "x", "follow": "x", "violate": "x"}]'    # 중복 - 앞이 이긴다
    )
    assert parse_scenes(raw, (1, 2, 3)) == {1: ("t", "f", "v")}
    assert parse_scenes("no json here", (1,)) == {}
    assert parse_scenes('[{"n": 1, "task": "t", ', (1,)) == {}


def test_clash_parsing_is_strict() -> None:
    raw = '[{"a": 1, "b": 2, "why": "opposite"}, {"a": 1, "b": 1, "why": "self"},' \
          ' {"a": 9, "b": 1, "why": "unknown"}, {"a": 2, "b": 1, "why": "same pair"}]'
    assert parse_clashes(raw, (1, 2, 3)) == [(1, 2, "opposite")]
    assert parse_clashes("{}", (1, 2)) == []


def test_letters_are_mixed_by_a_hash_of_path_and_line(project: Path) -> None:
    from xout.audit import _first

    seen = {_first(c.path, c.line_no) for c in candidates([project])}
    assert seen == {"follow", "violate"}, "A/B 순서가 섞여야 자리 편향을 흩는다"
    scene = _scene(first="violate")
    assert scene.a_text == scene.violate and scene.b_text == scene.follow
    assert scene.kind_of("A") == "violate" and scene.kind_of("B") == "follow"
    assert scene.kind_of(None) is None


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_prompts_carry_no_epistemic_vocabulary(lang: str) -> None:
    lines = [(3, "Ask before you push."), (7, "Run the tests before you finish.")]
    scene = _scene()
    prompts = [
        build_generate_prompt(lines, lang),
        build_clash_prompt(lines, lang),
        build_ask_prompt(scene, lang, None),
        build_ask_prompt(scene, lang, scene.candidate.line),
    ]
    for prompt in prompts:
        for token in EPISTEMIC_TOKENS:
            assert token.lower() not in prompt.lower(), (lang, token)
    assert "3. Ask before you push." in prompts[0]
    assert scene.candidate.line in prompts[3] and scene.candidate.line not in prompts[2]
    assert scene.follow in prompts[2] and scene.violate in prompts[2]


def test_audit_sorts_every_line_into_a_verdict(capsys, base: Path, project: Path, fake_runner: str) -> None:
    assert main(_run(base, project, fake_runner, "--lang", "en", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "xout_audit_receipt"
    verdicts = {line["line_no"]: line["verdict"] for line in payload["lines"]}
    assert verdicts == {
        12: VERDICT_DEFAULT,
        13: VERDICT_EFFECTIVE,
        14: VERDICT_IGNORED,
        15: VERDICT_UNCLEAR,
        17: VERDICT_DEFAULT,
        18: VERDICT_DEFAULT,
    }
    assert [c["line_no"] for c in payload["not_instructions"]] == [16]
    summary = payload["summary"]
    assert summary["sent"] == 7 and summary["scenes"] == 6
    assert summary["default"] == 3 and summary["effective"] == 1
    assert summary["ignored"] == 1 and summary["unclear"] == 1
    assert summary["not_an_instruction"] == 1 and summary["skipped"] == 7
    clash = payload["contradictions"]
    assert len(clash) == 1 and (clash[0]["a"]["line_no"], clash[0]["b"]["line_no"]) == (17, 18)
    assert clash[0]["why"] == "one waits, one pushes"


def test_receipt_keeps_every_raw_answer_and_nothing_else_is_written(
    capsys, base: Path, project: Path, fake_runner: str
) -> None:
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert main(_run(base, project, fake_runner, "--lang", "en", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    receipt = Path(payload["receipt_path"])
    assert receipt.is_file() and receipt.parent == base / "audits"
    assert [p for p in base.rglob("*") if p.is_file()] == [receipt]
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
    kept = json.loads(receipt.read_text(encoding="utf-8"))
    assert kept["summary"] == payload["summary"]
    assert [c["kind"] for c in kept["calls"]] == ["generate", "clash"]
    assert all(call["raw"] for call in kept["calls"])
    for line in kept["lines"]:
        assert line["bare"]["raws"] and line["ruled"]["raws"]
        assert line["trials"] == 1


def test_repeat_decides_by_majority(capsys, tmp_path: Path, base: Path, fake_runner: str) -> None:
    root = tmp_path / "flaky"
    root.mkdir()
    (root / "AGENTS.md").write_text(
        "- MEASURE flaky: run the migration only after a fresh backup exists.\n",
        encoding="utf-8",
    )
    assert main(_run(base, root, fake_runner, "--lang", "en", "--repeat", "3", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    line = payload["lines"][0]
    assert line["ruled"]["kinds"] == ["violate", "follow", "follow"]
    assert line["verdict"] == VERDICT_EFFECTIVE, "다수결이 첫 시행을 이긴다"
    assert line["trials"] == 3 and payload["repeat"] == 3


def test_a_single_trial_would_have_called_the_same_line_ignored(
    capsys, tmp_path: Path, base: Path, fake_runner: str
) -> None:
    root = tmp_path / "flaky"
    root.mkdir()
    (root / "AGENTS.md").write_text(
        "- MEASURE flaky: run the migration only after a fresh backup exists.\n",
        encoding="utf-8",
    )
    assert main(_run(base, root, fake_runner, "--lang", "en", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lines"][0]["verdict"] == VERDICT_IGNORED


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_text_output_groups_every_verdict_in_each_language(
    capsys, base: Path, project: Path, fake_runner: str, lang: str
) -> None:
    assert main(_run(base, project, fake_runner, "--lang", lang)) == 0
    out = capsys.readouterr().out
    assert out.count("CLAUDE.md:") == 7  # 판정 6줄 + 충돌 1줄
    for line_no in (12, 13, 14, 15, 17, 18):
        assert f"CLAUDE.md:{line_no}" in out
    assert "CLAUDE.md:16" not in out, "지시가 아닌 줄은 목록에 없다"
    assert "one waits, one pushes" in out
    assert str(base / "audits") in out


def test_dry_run_calls_nothing(capsys, base: Path, project: Path) -> None:
    assert main(_run(base, project, "/definitely/not/a/runner", "--lang", "en", "--dry-run", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [line["line_no"] for line in payload["lines"]] == list(SENT_LINES)
    assert payload["skipped"] == 7 and payload["over_limit"] == 0
    assert "MEASURE default: keep the changelog" in payload["prompt_sample"]
    assert not base.exists(), "dry-run은 영수증도 남기지 않는다"


def test_dry_run_text_shows_a_sample_prompt(capsys, base: Path, project: Path) -> None:
    assert main(_run(base, project, "/definitely/not/a/runner", "--lang", "en", "--dry-run", "--limit", "3")) == 0
    out = capsys.readouterr().out
    assert "3 line(s) from 1 rule file(s) would be sent" in out
    assert "held back by --limit 3" in out
    assert "12. - MEASURE default" in out


def test_missing_runner_exits_two(capsys, base: Path, project: Path) -> None:
    assert main(_run(base, project, "/definitely/not/a/runner", "--lang", "en")) == 2
    assert "Cannot start the runner" in capsys.readouterr().out
    assert not base.exists()


def test_a_failing_runner_exits_two(capsys, tmp_path: Path, base: Path, project: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    assert main(_run(base, project, f"{sys.executable} {script}", "--lang", "en", "--json")) == 2
    assert "error" in json.loads(capsys.readouterr().out)
