# Changelog

All notable changes to xout are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and xout follows [Semantic Versioning](https://semver.org/) with a deliberately
slow cadence: minor versions ship every 6-12 months, patches only for defects.

## [Unreleased]

### Added
- Japanese and Simplified Chinese runtime packs: `--lang ja` and `--lang zh`
  run the whole session (pairs, rules, screen text) in that language. The
  ledger stays language-neutral; `xout why --lang ja|zh` renders the same
  receipts.
- A how-it-works diagram per language (`.github/assets/how-it-works*.svg`)
  in the READMEs and on the site, plus an under-the-hood ledger diagram in
  the READMEs.
- `README.ja.md` and `README.zh.md`.

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

[1.0.1]: https://github.com/brnyxx/xout/releases/tag/v1.0.1
[1.0.0]: https://github.com/brnyxx/xout/releases/tag/v1.0.0
