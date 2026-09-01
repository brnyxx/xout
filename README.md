<div align="center">

<h1>xout</h1>

<img src=".github/assets/logo.svg" alt="xout logo: a behavior card crossed out with a bold crimson X, with one kept rule line below" width="96">

**X out the AI behavior you never want again.**

<img src=".github/assets/hero.svg" alt="Fix the bug becomes an A/B behavior test: Should I start is X'd out, Fixed and tests pass survives, and Act first report after becomes the rule" width="920">

[![PyPI](https://img.shields.io/pypi/v/xout)](https://pypi.org/project/xout/) [![CI](https://github.com/brnyxx/xout/actions/workflows/ci.yml/badge.svg)](https://github.com/brnyxx/xout/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[Quickstart](#how-it-works) · [Every rule proves itself](#every-rule-can-prove-itself) · [The map](#the-map) · [Commands](#commands) · [Why trust it](#why-you-can-trust-it)

<sub>Read in: English · [한국어](README.ko.md) · [Live explanation](https://brnyxx.github.io/xout/)</sub>

</div>

Your coding agent shows two concrete ways it could behave. You X out the wrong one. Two minutes and 15 X's later, the surviving choices are compiled into 8 local rules that Claude Code loads from `CLAUDE.md`.

```bash
uvx xout --lang en
```

That's it. The whole session runs right in your terminal. X things out for about 2 minutes. Your agent gets 8 rules.

<img src=".github/assets/demo.en.gif" alt="A real xout terminal session progressing through fifteen cross-outs, compiling eight conditional rules, and applying them with one keystroke" width="860">

<sub>That recording is a real session, not a mockup: every pair and rule on screen was produced by the actual engine (`scripts/record_tui_demo.py` drives a live `ColdOpenSession`).</sub>

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

   > Write a short plan first, then proceed immediately. **However, for hard-to-reverse work like deletes, pushes, deploys, and migrations, always get approval before executing.**

   That condition is not a template. It exists because you X'd differently in the migration scene. No interview-based tool can produce it.
3. **You apply with one keystroke.** The completion screen asks "apply now?" - saying yes adds exactly one owned `@import` line to `~/.claude/CLAUDE.md`. `xout undo` removes only that line.

Repeat the request that used to annoy you in a fresh Claude Code session and watch the rule hold. When it ever feels stale, `xout` again.

## Every rule can prove itself

`xout why` traces any rule back to the exact X's that created it:

```text
$ xout why autonomy --lang en
[Autonomy]
rule: Write a short plan first, then proceed immediately. However, ... get approval before executing.
state: discriminated / source: your X
evidence:
  - X'd ask_first in the routine-work scene (scn-bugfix) (session a3f2c9d1)
  - X'd act_then_report in the hard-to-reverse-work scene (scn-risky) (session a3f2c9d1)
```

A rule you can't trace is a rule you can't trust. Every xout rule carries its receipts.

> Sessions run in English with `--lang en` (pairs, rules, and screen text); the default without the flag is Korean. The event ledger is language-neutral either way.

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

## How it compares

|  | Hand-written CLAUDE.md / interview tools | xout |
|---|---|---|
| How preferences are captured | You state them - and normative pressure skews stated answers | Revealed by choice: you cross out one of two concrete behaviors |
| Where rules come from | Written from memory, no provenance | Compiled from your strikes - `xout why` traces every rule to its evidence |
| Routine vs risky work | One rule fits all situations | Rules fork on the routine/irreversible boundary - only when your X's actually diverged |
| Rollback | Hand-edit the file and hope | One receipt-proofed line; `xout undo` removes exactly it |
| **When rules go stale** | They silently drift until the agent annoys you again | Stale rules get re-struck in a 2-minute session; unstable ones are flagged for recheck |

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

  <details><summary><b>[PROOF]</b></summary>

  - **Setup** — the test suite kills sessions mid-strike, replays the ledger from disk, and opens concurrent sessions against the same store.
  - **Result** — replay reconstructs the identical state every time, duplicate sessions are rejected, and landing happens exactly once; 406 tests run on every commit across Python 3.10-3.14 and three OSes.
  - **So** — "interrupt anywhere" is a tested property, not a promise.

  </details>

- **Reversible.** Activation is one owned import line; `xout undo` removes only what xout can prove it wrote.

  <details><summary><b>[PROOF]</b></summary>

  - **Setup** — before touching `~/.claude/CLAUDE.md`, xout records a receipt: the file's prefix hash and the exact byte where its one line was inserted.
  - **Result** — `xout undo` re-verifies that receipt before removing anything; if the file changed around the line, it refuses rather than guessing.
  - **So** — rollback is proof-gated. xout cannot delete a line it cannot prove it wrote.

  </details>

- **Honest.** A kept behavior is "not crossed out yet," never "proven right." Guessed defaults are labeled as guesses.

  <details><summary><b>[PROOF]</b></summary>

  - **Setup** — we dogfood xout on itself. While building the English pack, `xout why` turned out to print `rule: None` - it read the wrong manifest key.
  - **Result** — the bug is documented in the [changelog](CHANGELOG.md), and the fix landed with a regression test in the same commit.
  - **So** — a tool whose whole pitch is "every rule carries receipts" keeps receipts on its own defects too.

  </details>

<details>
<summary><strong>The engineering behind those claims</strong></summary>

Every strike is an append-only JSONL event with fsync; landing is atomic with content hashes; sessions replay deterministically; duplicate sessions are rejected; manual edits are detected before landing. Pair scheduling judges discriminative power per context, so routine strikes never starve the risky scene; a session is voided unless at least five axes carry real strike evidence. The 15 strikes narrow a 6,561-agent hypothesis space (3 values across 8 axes) down to one - and the survivor is only "not falsified yet," never "proven right." The sealed preregistration lives in [`docs/prereg/prereg_sealed.json`](docs/prereg/prereg_sealed.json); the frozen axis catalog lives in [`docs/axis_locality_table.md`](docs/axis_locality_table.md). The eight-axis catalog is deliberately frozen: xout is a local behavior compiler, not a prompt manager, cloud profile, or agent orchestrator.

</details>

## Claude Code plugin & Agent Skills

xout also runs inside Claude Code as a conversation: `/xout:xout` shows each behavior pair in chat, you pick the one to X, and the agent records only your explicit choice. `/xout:xout status`, `/xout:xout undo` work the same way.

Or install the same skill through the open [Agent Skills](https://github.com/vercel-labs/skills) ecosystem - one command, any supported agent:

```bash
npx skills@latest add brnyxx/xout
```

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

## Credits

Built on the [Agent Skills](https://github.com/vercel-labs/skills) ecosystem (MIT) - its `SKILL.md` architecture and one-command install, in the skill-authoring lineage of [mattpocock/skills](https://github.com/mattpocock/skills) (MIT). Not a fork - the falsification engine underneath (append-only event ledger, pure-fold compiler, sealed preregistration) is xout's own.

MIT © 2026 Brian Kim.
