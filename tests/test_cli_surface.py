"""CLI 표면 계약 - 인자 없는 실행은 open, undo는 rollback과 동일 동작."""

from __future__ import annotations

from xout.cli import build_parser, cmd_rollback, normalize_argv


def test_bare_invocation_defaults_to_open() -> None:
    assert normalize_argv([]) == ["open"]


def test_explicit_command_passes_through() -> None:
    assert normalize_argv(["status"]) == ["status"]
    assert normalize_argv(["open", "--new"]) == ["open", "--new"]


def test_undo_is_rollback() -> None:
    parser = build_parser()
    undo = parser.parse_args(["undo"])
    rollback = parser.parse_args(["rollback"])
    assert undo.func is cmd_rollback
    assert rollback.func is cmd_rollback
