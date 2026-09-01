# xout maintenance contract

This file is the canonical contract for working on xout - for humans and
agents alike. It restates nothing the code can say; it states what the code
cannot enforce by itself.

## Invariants (never break these)

1. **Sealed artifacts are read-only.** `docs/prereg/`, `xout/_data/prereg/`,
   `xout/_data/ground_truth/` are hash-sealed evidence. Fix scoring around
   them, never edit them.
2. **The ledger is append-only.** No code path may rewrite or delete events.
   Corrections are new events (tombstones). Everything else - counters, rules,
   session verdicts - is a pure fold of the stream and must stay derivable.
3. **Session numbers are owned by the sealed preregistration.** Slot count,
   probe positions, and the validity floor are read from the sealed document.
   A guard test fails the build if runtime code restates them as literals.
4. **The 8-axis catalog is frozen.** 3 values per axis, 6,561 combinations.
   Quality goes up; the catalog does not grow.
5. **Zero runtime dependencies, zero LLM calls in a session.** The engine is
   stdlib-only. The measurement instrument must not depend on the thing it
   measures. The one deliberate exception lives outside sessions: `xout probe`
   is opt-in, calls an external runner the user names, and writes only a
   receipt under the owned directory.
6. **Consent gates every write outside the owned directory.** The only file
   xout touches outside `~/.claude/xout/` is one `@import` line in
   `~/.claude/CLAUDE.md`, receipt-proofed for rollback.

## Duplicated surfaces (sync or the build fails)

- `fixtures/` (source of truth) == `xout/_data/fixtures/` (packaged),
  byte-identical - guarded by `tests/test_repo_hygiene.py`.
- Version strings: `pyproject.toml` == `package.json` == plugin.json ==
  marketplace.json == README release links - guarded by
  `scripts/check_release_version.py`.
- Language tables come in sets: every `ko` entry has an entry for each
  language in `SUPPORTED_LANGS` (`en`, `ja`, `zh`) - rule texts, clauses,
  axis labels, TUI/why strings - guarded by `tests/test_lang_en.py`. Each
  language also owns a scene pack, a demo GIF, a how-it-works diagram, and a
  README edition - guarded by `tests/test_repo_hygiene.py`. The ledger itself
  is language-neutral.

## Working rules

- Full suite green before every commit: `python3 -m pytest tests/ -q`.
- One-line conventional commit titles (`feat:` / `fix:` / `docs:` / `test:` /
  `chore:`). Bug fixes start from a failing reproduction test.
- Versions are cut by maintainers only, slowly: minors every 6-12 months,
  patches for defects. Never bump a version inside a feature change.
- User-facing copy never uses epistemic vocabulary in `XOUT.md` output or
  probe prompts (falsification, hypothesis, grade live in the manifest, not
  the rules) - guarded by the EPISTEMIC_TOKENS tests.
- `XOUT.md` has a fixed skeleton per language (`XOUT_DOC`): preamble, routine
  section, hard-to-reverse section with the condition defined once. Rule
  sentences come from the rule tables; the skeleton never restates them.
- Release rail: tag `vX.Y.Z` -> CI validates the version contract -> builds
  wheel/sdist/plugin ZIP with SHA256SUMS and provenance.
