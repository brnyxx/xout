<div align="center">

<h1>xout</h1>

<img src=".github/assets/logo.svg" alt="xout logo: a behavior card crossed out with a bold crimson X, with one kept rule line below" width="96">

**X out the AI behavior you never want again.**

<img src=".github/assets/hero.svg" alt="Fix the bug becomes an A/B behavior test: Should I start is X'd out, Fixed and tests pass survives, and Act first report after becomes the rule" width="920">

[한국어](README.ko.md) · [Live explanation](https://brnyxx.github.io/xout/)

</div>

Your coding agent shows two concrete ways it could behave. You X out the wrong one. Two minutes and 15 X's later, the surviving choices are compiled into 8 local rules that Claude Code loads from `CLAUDE.md`.

```bash
uvx xout
```

That's it. Your browser opens. X things out for about 2 minutes. Your agent gets 8 rules.

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

1. **You X out** the behavior you hate. Each pair compares two real agent behaviors: ask first vs act first, strict scope vs proactive cleanup, test first vs test after, and so on.
2. **xout compiles** the surviving choices into 8 executable rule lines, written atomically under `~/.claude/xout/` with evidence and provenance.
3. **You apply with one click.** The completion screen asks "apply now?" - saying yes adds exactly one owned `@import` line to `~/.claude/CLAUDE.md`. `xout undo` removes only that line.

Repeat the request that used to annoy you in a fresh Claude Code session and watch the rule hold. When it ever feels stale, `xout` again.

<details>
<summary><strong>Watch the real 15-X browser session</strong> (1.6 MB GIF)</summary>
<br>
<img src=".github/assets/demo.gif" alt="The real browser UI progressing from zero to fifteen cross-outs, compiling local rules, and completing the session" width="860">
</details>

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

## Commands

| Command | What it does |
|---|---|
| `xout` | Start (or automatically resume) a session; self-checks before opening |
| `xout undo` | Remove the one import line xout owns - full rollback |
| `xout status` | Show your 8 rules and whether they are active |
| `xout dev ...` | Power tools: export, validate, re-pick, backup, session inspection |

## Why you can trust it

- **Local only.** No LLM calls, no telemetry, no cookies, no network during a session.
- **Crash-safe.** Append-only ledger with atomic writes: interrupt anywhere, resume anywhere, land exactly once.
- **Reversible.** Activation is one owned import line; `xout undo` removes only what xout can prove it wrote.
- **Honest.** A kept behavior is "not crossed out yet," never "proven right." Guessed defaults are labeled as guesses.

<details>
<summary><strong>The engineering behind those claims</strong></summary>

Append-only JSONL events, fsync, process locks, sealed fixture/session digests, deterministic replay, atomic replacement, loopback Host/Origin checks, duplicate-session rejection, and manual-edit detection before landing. The sealed preregistration lives in [`docs/prereg/prereg_sealed.json`](docs/prereg/prereg_sealed.json); the frozen axis catalog lives in [`docs/axis_locality_table.md`](docs/axis_locality_table.md). The eight-axis catalog is deliberately frozen: xout is a local behavior compiler, not a prompt manager, cloud profile, or agent orchestrator.

</details>

## Claude Code plugin

xout also runs inside Claude Code: `/xout:xout open`, `/xout:xout status`, `/xout:xout undo`.

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

Then in a fresh Claude Code session: `/xout:xout doctor`, `/xout:xout open`.

</details>

## Remove

```bash
xout undo        # deactivate: removes the one owned import line
```

Your rules and event history stay in `~/.claude/xout/` (yours to keep or delete). Uninstalling the package never touches them.

## Development

```bash
python3 -m pip install -e '.[test,e2e,release]'
python3 -m pytest tests/ -q
```

CI covers Python 3.10-3.14, macOS/Linux/Windows, and Chromium/Firefox/WebKit. Releases ship wheel, sdist, plugin ZIP, `SHA256SUMS`, and artifact provenance.

MIT © 2026 Brian Kim.
