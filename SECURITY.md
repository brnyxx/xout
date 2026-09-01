# Security Policy

## What counts as a vulnerability in xout

xout's security promises are narrow and explicit. Report anything that breaks
one of them:

1. A code path that writes outside `~/.claude/xout/` without an explicit
   consent record (the only sanctioned external write is one `@import` line
   in `~/.claude/CLAUDE.md`).
2. `xout undo` removing bytes it cannot prove it wrote (receipt bypass).
3. Any network call during a session (the engine must be fully local).
4. Ledger tampering that replay fails to detect, or landed-file edits that
   the content-hash check fails to flag.
5. The usual: arbitrary code execution via crafted fixture/session files.

## Reporting

Use [GitHub private vulnerability reporting](../../security/advisories/new).
Please include the command, raw output, and `xout doctor` output (read-only).
Do not open a public issue for an unpatched vulnerability.

## Supported versions

The latest minor release receives fixes. xout versions slowly (minors every
6-12 months), so "latest" is a small target to stay on.
