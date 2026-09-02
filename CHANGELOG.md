# Changelog

All notable changes to xout are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and xout follows [Semantic Versioning](https://semver.org/) with a deliberately
slow cadence: minor versions ship every 6-12 months, patches only for defects.

## [Unreleased]

### Added
- `xout mine --runner "…"` and `xout conflicts --runner "…"`: an opt-in
  second layer that hands the lines of your rule files to your own agent
  (`claude -p`, `codex exec`, any runner from the probe table) and asks it to
  name the axis and value each line states. Its verdicts are merged with the
  pattern layer and the two are compared (agreed / agent only / pattern only /
  different value); the receipt under `~/.claude/xout/judgments/` keeps every
  raw answer. Without the flag nothing changes.
- `xout probe --repeat N`: every question is asked N times, the majority
  decides, and the receipt keeps each trial. The summary adds the by-trial
  count and how many rules held every single time.
- `xout probe --context-file FILE`: a real project document goes in front of
  the rules in both passes, so the rules have to hold while buried under other
  instructions.
- `xout probe --via-target ID`: the rules stay out of the prompt. For the bare
  pass the block (or the `@import` line) is taken out of that tool's own rules
  file, for the ruled pass it is put back, and the file is restored at the end
  (savepoint first). This measures whether the tool actually reads the file
  xout wrote into.
- `xout probe` strips terminal colour codes before reading the answer
  (kiro-cli prints them).

## [1.1.0] - 2026-09-02

### Added
- Japanese and Simplified Chinese runtime packs: `--lang ja` and `--lang zh`
  run the whole session (pairs, rules, screen text) in that language. The
  ledger stays language-neutral; `xout why --lang ja|zh` renders the same
  receipts.
- A how-it-works film per language (`.github/assets/how-it-works*.gif`,
  rendered with Remotion from `video/`) in the READMEs and on the site,
  plus an under-the-hood ledger diagram in the READMEs.
- `README.ja.md` and `README.zh.md`, a Japanese and a Chinese site route
  (`/ja/`, `/zh/`, with hreflang links and a four-language switcher), and a
  hero image and social card per language (`hero.<lang>.svg`,
  `social-card.<lang>.png`). `tests/test_repo_hygiene.py` fails when a
  language is missing any of its surfaces.
- `xout probe` - opt-in, outside any session: sends each measured scene as
  an A/B question to an external runner (default `claude -p`) twice, bare
  and with the landed `XOUT.md` in front, and writes a receipt under
  `~/.claude/xout/probes/` saying per axis whether the rule held and whether
  it moved the choice. The ledger is never touched.
- `xout conflicts [paths]` - lines in a project's CLAUDE.md/AGENTS.md/
  .cursorrules that ask for a different value than your rules, with file:line.
  The session completion screen shows them before asking to apply.
- Your own prompts are part of the picture now: `mine`, `conflicts`, and
  `reconcile` read `~/.claude/CLAUDE.md` and `~/.claude/rules/` by default
  (`--no-user` to skip), and every pair in the session shows the lines your
  files already say about that axis (`xout pair` JSON carries them as `mined`).
- `xout reconcile [paths]` - lines your existing rule files repeat from
  `XOUT.md` (duplicates) and lines that say the opposite (conflicts). Writes a
  proposed patch under `~/.claude/xout/reconcile/`; `--apply --grant` removes
  the duplicate lines only, always behind a savepoint. Conflicts are never
  edited.
- Universal activation: `xout enable --grant --target codex|opencode|gemini|copilot|pi|omp|kiro|agents|all`
  adds one owned, marker-bounded block to that tool's documented rules file
  (`~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md`, `~/.gemini/GEMINI.md`,
  `~/.copilot/copilot-instructions.md`, `~/.pi/agent/AGENTS.md`,
  `~/.omp/agent/AGENTS.md`, `~/.kiro/steering/xout.md`, `./AGENTS.md`), behind a
  receipt and a savepoint; `xout undo` removes exactly that block. Claude Code
  keeps the one `@import` line. `xout targets` lists every target with its live
  state. Paths come from each tool's own documentation; gajae-code's path
  (`~/.gjc/agent/AGENTS.md`) comes from its installed package source, which
  the README says explicitly.
- `xout probe` closes stdin for the runner (codex exec otherwise waits on it)
  and the README records real cross-tool probes: codex, opencode and
  gajae-code each held 8/8 rules on the same landed profile.
- `xout savepoint [create|list|restore <id>] - byte-exact snapshots of the
  rule files outside xout's directory, stored under `~/.claude/xout/savepoints/`.
  Restore rewrites only files that existed at snapshot time.

### Changed
- `XOUT.md` is now written for the agent that reads it: a one-paragraph
  preamble (whose preferences these are, project rules win on a direct
  conflict), a "routine work" section, and a "hard-to-reverse work" section
  that defines the condition once instead of repeating it per rule, with an
  emphasized tie-breaker ("when unsure, treat it as hard to reverse"). Each
  rule carries the alternatives the user actually X'd out. Rule sentences
  themselves are unchanged; `manifest.json` gains `irreversible_value`.
- The `test_after` rule says in so many words that bug fixes are fixed first and
  get their regression test in the same change - the probe showed the agent
  defaulting to test-first on bugfixes. `xout probe` now pairs every rule with
  its true opposite (`OPPOSITE` in `xout/probe.py`) instead of the first
  eliminated value, after a weak pair (prefer_existing vs ask_first) produced a
  meaningless miss.
- Demo GIFs re-recorded with the new rule text and the other-tools hint.
- Every language's copy rewritten in its own voice (README, site, scene packs,
  rule sentences, CLI messages, film). The site's transcript labels are
  "Crossed out / Kept" (ko: "X 친 쪽 / 남긴 쪽") instead of epistemic terms.
- Audit fixes: `enable` refuses to write any target until XOUT.md exists;
  `pair` reports `session_complete` instead of silently opening a new session
  (`--new` to start one); `export`, `pair` rule labels, `status`, `enable`,
  `undo` and landing logs follow `--lang`; `status`/`enable`/`undo` print to
  stdout; `mine` drops lines that match two values of one axis and reads
  "tests first"; lock files live under `~/.claude/xout/.locks/` instead of
  next to `CLAUDE.md`; sdist ships the test harness and every README and no
  GIFs.
- Japanese and Chinese conditional rules end with `。` instead of an ASCII
  period, and `xout why` grade labels are localized for both.

## [1.0.1] - 2026-09-01

### Added
- `xout mine [paths]` - read-only mining of local rule files (`CLAUDE.md`,
  `AGENTS.md`, `.cursorrules`, `.cursor/rules/`, copilot instructions,
  `GEMINI.md`) into axis observations, every one carrying a file:line receipt.
- `docs/mined-prior.md` - the receipts behind the catalog's mined prior: a
  survey of 100+ high-star (10k-240k+) prompt/agent projects, with verbatim
  quotes, verified star counts, and per-axis frequency verdicts.

### Changed
- The mined prior for two axes now matches what the field actually writes
  (see `docs/mined-prior.md`): `commit_style` defaults to `no_auto_commit`
  (previously `conventional`) and `test_discipline` defaults to `test_after`
  (previously `test_first`). Six of eight priors were confirmed as-is. Value
  sets, measurement, and your own strikes are unaffected - only the honest
  guess for axes you have not yet been asked about changed.

## [1.0.0] - 2026-09-01

First release under the xout name. xout is the successor to Popper 1.x -
same falsification engine, new name, new measurement design.

### Added
- Three-scene measurement: a routine bugfix, a routine feature, and an
  irreversible production migration. Five of the eight axes are measured in
  both contexts.
- Conditional rule compilation: when your strikes diverge between routine and
  irreversible work, the rule ships with that condition attached - derived
  from strike evidence, never from a template.
- `xout why [axis]` - trace any rule back to the exact strikes that created it.
- Terminal session (TUI) as the only interactive surface; the browser UI was
  removed entirely.
- Headless agent commands: `xout pair` (next pair as JSON) and
  `xout strike <target> --pair-id` for conversational agent sessions.
- English runtime pack: `--lang en` runs the whole session in English
  (pairs, rules, screen text). The event ledger is language-neutral.
- Agent Skills ecosystem support: `npx skills@latest add brnyxx/xout`
  installs the `/xout` skill to any supported agent.
- Automatic migration from Popper 1.x home (`~/.claude/popper/` to
  `~/.claude/xout/`), including receipt-proof rewrite of the owned import line.

### Changed
- Axis catalog v2: `verification` and `dependency_policy` replace
  `response_language` and `verbosity` (still 8 axes x 3 values = 6,561).
  Events recorded against retired axes replay leniently.
- Session validity gate v2: a session is valid when at least five axes carry
  real strike evidence (complete or partial discrimination).
- Pair scheduling judges discriminative power per context, so routine strikes
  never starve the irreversible scene.

### Fixed
- `xout why` printed `rule: None` - it read the wrong manifest key.

[1.1.0]: https://github.com/brnyxx/xout/releases/tag/v1.1.0
[1.0.1]: https://github.com/brnyxx/xout/releases/tag/v1.0.1
[1.0.0]: https://github.com/brnyxx/xout/releases/tag/v1.0.0
