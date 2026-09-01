"""테스트 전역 가드 - CLI 경유 테스트가 실제 사용자 홈을 이관하지 못하게 막는다."""

from __future__ import annotations

import pytest

import xout.cli
from xout.migrate import MigrationResult


@pytest.fixture(autouse=True)
def _no_real_home_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        xout.cli, "migrate_legacy_home", lambda *a, **k: MigrationResult()
    )
