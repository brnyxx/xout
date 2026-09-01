# Contributing to xout

Thanks for looking under the hood. xout is small on purpose - the rules below
keep it that way.

## Dev setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,release]'
.venv/bin/python -m pytest tests/ -q
```

The whole suite runs in seconds and must be green before any commit.
CI covers Python 3.10-3.14 on macOS, Linux, and Windows.

## Ground rules

- **Sealed artifacts are read-only.** Never edit `docs/prereg/`,
  `xout/_data/ground_truth/`, or `xout/_data/prereg/`. The session numbers
  (slot count, probe positions, validity floor) are owned by the sealed
  preregistration - runtime code reads them, never restates them.
- **Fixtures live in two synced locations.** `fixtures/` (source of truth)
  and `xout/_data/fixtures/` (packaged) must stay byte-identical. If you touch
  one, copy to the other.
- **The 8-axis catalog is frozen.** Improve measurement quality, don't add
  axes. Proposals that grow the catalog will be declined.
- **Zero runtime dependencies.** The engine is stdlib-only. Dev/test
  dependencies need a maintainer's OK before they land in `pyproject.toml`.
- **The ledger is append-only.** Never write code that rewrites or deletes
  events - corrections are new events (tombstones).
- **Language is a render-layer concern.** The event ledger stores axes,
  values, and fragment ids only. New user-facing strings go into the
  per-language tables (`ko` and `en`) - never hardcoded in logic.

## Commits and PRs

- One-line commit titles with a conventional prefix:
  `feat:` / `fix:` / `docs:` / `test:` / `chore:`.
- Every behavior change ships with a test in the same commit. Bug fixes start
  from a failing reproduction test.
- Versioning is deliberately slow: minor versions every 6-12 months, patches
  only for defects. Don't bump versions in a PR - maintainers cut releases,
  and `scripts/check_release_version.py` cross-checks every version string.

## Reporting issues

A good xout issue includes: your OS, Python version, the command you ran, and
the raw output. If a session behaves oddly, `xout doctor` and
`xout sessions <session-id>` output make diagnosis fast - both are read-only.
