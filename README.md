<div align="center">

<h1>xout</h1>

<img src=".github/assets/logo.svg" alt="xout logo: a behavior card crossed out with a bold crimson X, with one kept rule line below" width="96">

**X out the AI behavior you never want again.**

<img src=".github/assets/hero.svg" alt="Fix the bug becomes an A/B behavior test: Should I start is X'd out, Fixed and tests pass survives, and Act first report after becomes the rule" width="920">

[![PyPI](https://img.shields.io/pypi/v/xout)](https://pypi.org/project/xout/) [![CI](https://github.com/brnyxx/xout/actions/workflows/ci.yml/badge.svg)](https://github.com/brnyxx/xout/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[한국어](README.ko.md) · [Live explanation](https://brnyxx.github.io/xout/)

</div>

Your coding agent shows two concrete ways it could behave. You X out the wrong one. Two minutes and 15 X's later, the surviving choices are compiled into 8 local rules that Claude Code loads from `CLAUDE.md`.

```bash
uvx xout
```

That's it. The whole session runs right in your terminal. X things out for about 2 minutes. Your agent gets 8 rules.

<img src=".github/assets/demo.gif" alt="A real xout terminal session progressing through fifteen cross-outs, compiling eight conditional rules, and applying them with one keystroke" width="860">

**No cloud. No telemetry. No LLM calls. One-line rollback.**

**v1.0.0 · Python 3.10–3.14 · MIT · zero third-party runtime packages**

<details>
<summary><strong>Other install paths</strong> (pip, venv)</summary>

```bash
pip install xout
xout
```

Or fully isolated:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install xout
.venv/bin/xout
```

Upgrading from Popper 1.x? Just run `xout` once: your data in `~/.claude/popper/` moves to `~/.claude/xout/`, and the owned import line is updated only when xout can prove it wrote it.

</details>

## How it works

1. **You X out** the behavior you hate, across three real scenes: a routine bugfix, a new feature, and a risky production migration. Each pair shows two concrete agent behaviors - ask first vs act first, standard library vs install-a-package, rehearse the migration vs trust a re-read.
2. **xout compiles** the survivors into 8 executable rules, written atomically under `~/.claude/xout/` with evidence and provenance. And when your X's diverge between routine and irreversible work, the rule compiles **with that condition attached**:

   > 짧은 계획을 먼저 적고 곧바로 이어서 실행한다. **단, 삭제, push, 배포, 마이그레이션처럼 되돌리기 어려운 작업에서는 실행 전에 반드시 승인을 받는다.**

   That condition is not a template. It exists because you X'd differently in the migration scene. No interview-based tool can produce it.
3. **You apply with one keystroke.** The completion screen asks "apply now?" - saying yes adds exactly one owned `@import` line to `~/.claude/CLAUDE.md`. `xout undo` removes only that line.

Repeat the request that used to annoy you in a fresh Claude Code session and watch the rule hold. When it ever feels stale, `xout` again.

## Every rule can prove itself

`xout why` traces any rule back to the exact X's that created it:

```text
$ xout why autonomy
[자율성]
규칙: 짧은 계획을 먼저 적고 곧바로 이어서 실행한다. 단, ... 승인을 받는다.
상태: 판별시험 통과 / 출처: 당신의 X
근거:
  - 일상 작업 장면(scn-bugfix)에서 ask_first에 X (세션 a3f2c9d1)
  - 되돌리기 어려운 작업 장면(scn-risky)에서 act_then_report에 X (세션 a3f2c9d1)
```

A rule you can't trace is a rule you can't trust. Every xout rule carries its receipts.

> **Korean-first v1:** the session UI and generated rule text are currently Korean. An English runtime pack is planned; this README describes the product honestly either way.

## What you get

After the fifteenth X, three files land under `~/.claude/xout/`:

| File | What it is |
|---|---|
| `XOUT.md` | 8 executable rule lines for Claude Code |
| `manifest.json` | Rule value, confidence label, source, and content hashes |
| `settings.xout.json` | A reviewable settings proposal |

Rules you confirmed by X'ing are labeled **confirmed**; defaults xout guessed without asking you are honestly labeled **guessed** and queued for a quick re-pick. Nothing is ever activated without your explicit yes.

*(Popper 1.x landed the same files as `POPPER.md` and `settings.popper.json` under `~/.claude/popper/`; xout migrates them automatically on first run.)*

## The map

Eight axes, measured across three scenes. Five axes are measured in **both** contexts, so they can fork on the routine/irreversible boundary - with evidence.

| Axis | Routine scenes | Irreversible scene | Can fork |
|---|---|---|---|
| Autonomy | bugfix | migration | yes |
| Error behavior | bugfix | migration | yes |
| Verification before done | feature | migration | yes |
| Dependency policy | feature | migration | yes |
| Commit policy | feature | migration | yes |
| Scope adherence | bugfix + feature | - | cross-checked |
| Test discipline | bugfix + feature | - | cross-checked |
| Comments and docs | bugfix | - | style axis |

## Commands

| Command | What it does | Writes to | Consent |
|---|---|---|---|
| `xout` | Start (or automatically resume) a session | own dir only | - |
| `xout why [axis]` | Trace a rule back to the X's that created it | nothing | - |
| `xout status` | Show your 8 rules and whether they are active | nothing | - |
| `xout undo` | Remove the one import line xout owns - full rollback | one owned line | - |
| `xout enable --grant` | Activate: add one owned `@import` line | one owned line | explicit |
| `xout pair` / `xout strike` | Headless JSON session for agents and scripts | own dir only | - |

## Why you can trust it

- **Local only.** No LLM calls, no telemetry, no cookies, no network during a session.
- **Crash-safe.** Append-only ledger with atomic writes: interrupt anywhere, resume anywhere, land exactly once.
- **Reversible.** Activation is one owned import line; `xout undo` removes only what xout can prove it wrote.
- **Honest.** A kept behavior is "not crossed out yet," never "proven right." Guessed defaults are labeled as guesses.

<details>
<summary><strong>The engineering behind those claims</strong></summary>

Every strike is an append-only JSONL event with fsync; landing is atomic with content hashes; sessions replay deterministically; duplicate sessions are rejected; manual edits are detected before landing. Pair scheduling judges discriminative power per context, so routine strikes never starve the risky scene; a session is voided unless at least five axes carry real strike evidence. The 15 strikes narrow a 6,561-agent hypothesis space (3 values across 8 axes) down to one - and the survivor is only \"not falsified yet,\" never \"proven right.\" The sealed preregistration lives in [`docs/prereg/prereg_sealed.json`](docs/prereg/prereg_sealed.json); the frozen axis catalog lives in [`docs/axis_locality_table.md`](docs/axis_locality_table.md). The eight-axis catalog is deliberately frozen: xout is a local behavior compiler, not a prompt manager, cloud profile, or agent orchestrator.

</details>

## Claude Code plugin

xout also runs inside Claude Code as a conversation: `/xout:xout` shows each behavior pair in chat, you pick the one to X, and the agent records only your explicit choice. `/xout:xout status`, `/xout:xout undo` work the same way.

<details>
<summary><strong>Checksum-verified plugin install</strong></summary>

Download `xout-plugin-1.0.0.zip`, `SHA256SUMS`, and `verify_checksums.py` from the [v1.0.0 release](../../releases/tag/v1.0.0), keep all three in one directory, then:

```bash
python3 verify_checksums.py SHA256SUMS \
  --only xout-plugin-1.0.0.zip verify_checksums.py
DEST="$HOME/.local/share/xout-plugin-1.0.0"
test ! -e "$DEST" || { echo "destination already exists: $DEST" >&2; exit 1; }
python3 -m zipfile -e xout-plugin-1.0.0.zip "$DEST"
claude plugin marketplace add "$DEST"
claude plugin install xout@xout-marketplace
```

Then in a fresh Claude Code session: `/xout:xout doctor`, `/xout:xout`.

</details>

## Remove

```bash
xout undo        # deactivate: removes the one owned import line
```

Your rules and event history stay in `~/.claude/xout/` (yours to keep or delete). Uninstalling the package never touches them.

## Development

```bash
python3 -m pip install -e '.[test,release]'
python3 -m pytest tests/ -q
```

CI covers Python 3.10-3.14 on macOS, Linux, and Windows. Releases ship wheel, sdist, plugin ZIP, `SHA256SUMS`, and artifact provenance.

MIT © 2026 Brian Kim.
