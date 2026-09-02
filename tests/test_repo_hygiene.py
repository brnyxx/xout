"""레포 위생 드리프트 가드.

손으로 복제되는 표면들이 어긋나면 CI가 실패해야 한다 - 동기화를 가장하지 않는다.
"""

from __future__ import annotations

import struct
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


def test_every_language_owns_its_surfaces() -> None:
    """언어 하나를 추가하면 팩·데모 GIF·다이어그램·README가 같이 와야 한다."""
    from xout.fixtures import SUPPORTED_LANGS

    assets = ROOT / ".github" / "assets"
    for lang in SUPPORTED_LANGS:
        gif = assets / ("demo.gif" if lang == "ko" else f"demo.{lang}.gif")
        payload = gif.read_bytes()
        assert payload[:6] in {b"GIF87a", b"GIF89a"}, gif.name
        assert struct.unpack("<HH", payload[6:10]) == (960, 608), gif.name
        diagram = assets / ("how-it-works.gif" if lang == "en" else f"how-it-works.{lang}.gif")
        motion = diagram.read_bytes()
        assert motion[:6] in {b"GIF87a", b"GIF89a"}, diagram.name
        assert struct.unpack("<HH", motion[6:10]) == (960, 540), diagram.name
        readme = ROOT / ("README.md" if lang == "en" else f"README.{lang}.md")
        body = readme.read_text(encoding="utf-8")
        assert diagram.name in body, readme.name
        assert gif.name in body, readme.name
        for other in SUPPORTED_LANGS:
            link = "README.md" if other == "en" else f"README.{other}.md"
            assert other == lang or link in body, (readme.name, link)


def test_readmes_list_every_registered_target() -> None:
    """지원 도구 표(README 4종)와 targets.REGISTRY가 같이 움직여야 한다."""
    from xout.targets import REGISTRY

    for name in ("README.md", "README.ko.md", "README.ja.md", "README.zh.md"):
        body = (ROOT / name).read_text(encoding="utf-8")
        for target in REGISTRY.values():
            assert f"| `{target.target_id}`" in body, (name, target.target_id)
            assert target.doc_url in body, (name, target.doc_url)
            if target.mode == "block":
                assert target.relative_path.split("/")[-1] in body, (name, target.relative_path)
