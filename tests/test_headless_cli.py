"""에이전트용 헤드리스 CLI(pair/strike)와 TUI 루프 계약.

pair는 다음 페어를 JSON으로 내놓고, strike는 그 pair_id로만 긋기를 기록하며,
프로세스를 오가도 같은 이벤트 원장 위에서 이어진다. TUI는 완주 시 착지까지
도달하고 적용 여부를 물은 뒤 동의 없이는 사용자 파일을 건드리지 않는다.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from xout.cli import main
from xout.compiler import XOUT_MD


def _pair_json(capsys, tmp_path: Path) -> dict:
    assert main(["pair", "--base-dir", str(tmp_path)]) == 0
    return json.loads(capsys.readouterr().out)


def test_pair_then_strike_roundtrip(capsys, tmp_path: Path) -> None:
    first = _pair_json(capsys, tmp_path)
    assert first["pair"] is not None
    pair_id = first["pair"]["pair_id"]
    assert first["slots_used"] == 0

    assert (
        main(
            [
                "strike",
                "left",
                "--pair-id",
                pair_id,
                "--base-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    after = json.loads(capsys.readouterr().out)
    assert after["slots_used"] == 1
    assert after["session_id"] == first["session_id"]

    resumed = _pair_json(capsys, tmp_path)
    assert resumed["session_id"] == first["session_id"]
    assert resumed["slots_used"] == 1


def test_strike_rejects_wrong_pair_id(capsys, tmp_path: Path) -> None:
    first = _pair_json(capsys, tmp_path)
    assert first["pair"] is not None
    assert (
        main(
            [
                "strike",
                "right",
                "--pair-id",
                "pair-does-not-exist",
                "--base-dir",
                str(tmp_path),
            ]
        )
        == 1
    )


def test_tui_completes_session_and_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def scripted_input(prompt: str = "") -> str:
        if prompt.startswith("X>"):
            return "1"
        return ""  # 적용 프롬프트 - 동의하지 않음

    monkeypatch.setattr(builtins, "input", scripted_input)
    assert main(["open", "--base-dir", str(tmp_path)]) == 0
    assert (tmp_path / XOUT_MD).is_file()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest
