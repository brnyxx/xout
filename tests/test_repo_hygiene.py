"""레포 위생 드리프트 가드.

손으로 복제되는 표면들이 어긋나면 CI가 실패해야 한다 - 동기화를 가장하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE_FIXTURES = ROOT / "fixtures"
PACKAGED_FIXTURES = ROOT / "xout" / "_data" / "fixtures"


def _fixture_files(base: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(base.rglob("*.json"))
    }


def test_fixture_packs_are_byte_identical() -> None:
    """fixtures/(소스)와 xout/_data/fixtures/(패키징)는 항상 같은 바이트여야 한다."""
    source = _fixture_files(SOURCE_FIXTURES)
    packaged = _fixture_files(PACKAGED_FIXTURES)
    assert source.keys() == packaged.keys(), (
        sorted(source.keys() ^ packaged.keys())
    )
    for name, body in source.items():
        assert body == packaged[name], f"fixture drift: {name}"


def test_meta_docs_exist() -> None:
    for name in ("CHANGELOG.md", "CONTRIBUTING.md", "LICENSE"):
        assert (ROOT / name).is_file(), name


def test_skill_frontmatter_contract() -> None:
    """skills 생태계 CLI 계약: skills/<name>/SKILL.md + name/description frontmatter."""
    skill = ROOT / "skills" / "xout" / "SKILL.md"
    head = skill.read_text(encoding="utf-8").split("---")[1]
    assert "name: xout" in head
    assert "description:" in head
