"""GA 배포 메타데이터와 결정론적 플러그인 아카이브 계약."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = ROOT / ".claude-plugin" / "marketplace.json"


def _script_module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_marketplace_and_package_versions_match() -> None:
    version_contract = _script_module("check_release_version")
    plugin = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))
    entry = marketplace["plugins"][0]

    assert marketplace["name"] == "xout-marketplace"
    assert entry["name"] == plugin["name"] == "xout"
    versions = version_contract.declared_versions(ROOT)
    assert len(set(versions.values())) == 1
    assert entry["version"] == plugin["version"] == versions["package"]
    assert entry["source"] == "./"
    assert version_contract.validate(f"v{plugin['version']}", ROOT) == plugin["version"]
    with pytest.raises(version_contract.VersionContractError, match="TAG_MISMATCH"):
        version_contract.validate("v0.0.0", ROOT)


def test_plugin_archive_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    module = _script_module("build_plugin_archive")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    module.build_archive(ROOT, first)
    module.build_archive(ROOT, second)

    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert ".claude-plugin/plugin.json" in names
        assert "skills/xout/SKILL.md" in names
        assert "scripts/xout_plugin.py" in names
        assert "xout/_data/prereg/prereg_sealed.txt" in names
        assert "xout/_data/ground_truth/ground_truth.txt" in names
        assert "LICENSE" in names
        assert "README.md" in names
        assert "README.ko.md" in names
        assert not any(name.startswith(("tests/", ".git/", "build/")) for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_public_distribution_has_license_and_automation() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Brian Kim" in license_text
    assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()
    assert (ROOT / ".github" / "workflows" / "release.yml").is_file()
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for public_surface in (
        "README.ko.md",
        ".claude-plugin",
        ".github/assets",
        ".github/workflows",
        "scripts",
        "site",
        "skills",
    ):
        assert public_surface in manifest


def test_readmes_are_bilingual_brand_first_and_lifecycle_complete() -> None:
    version = _script_module("check_release_version").validate(root=ROOT)
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    korean = (ROOT / "README.ko.md").read_text(encoding="utf-8")
    assert ".github/assets/demo.en.gif" in english
    assert ".github/assets/demo.gif" in korean
    for readme in (english, korean):
        assert readme.count("<h1>xout</h1>") == 1
        assert f"v{version}" in readme
        assert ".github/assets/logo.svg" in readme
        assert re.search(r"\.github/assets/hero(\.ko)?\.svg", readme)
        assert "uvx xout" in readme
        assert "xout undo" in readme
        assert "xout status" in readme
        assert "/xout:xout" in readme
        assert "verify_checksums.py" in readme
        assert f"xout-plugin-{version}.zip" in readme
        assert f"../../releases/tag/v{version}" in readme
        assert f"xout-plugin-{version}" in readme
        assert "zero runtime dependencies" not in readme
        assert "런타임 의존성 0개" not in readme
        assert "<repository-url>" not in readme
        assert "example.test" not in readme
    assert "[한국어](README.ko.md)" in english
    assert "[English](README.md)" in korean
    assert "X out the AI behavior you never want again." in english
    assert "다시 보고 싶지 않은 AI 행동에 X를 치세요." in korean
    assert "--lang en" in english
    assert "labeled **guessed**" in english
    assert "**추정**으로 표시" in korean
    assert "preference convergence" not in english.lower()
    assert "선호 수렴" not in korean
    assert english.index("## How it works") < english.index("## Commands")
    assert korean.index("## 동작 방식") < korean.index("## 명령어")
    for source, readme in (
        (ROOT / "README.md", english),
        (ROOT / "README.ko.md", korean),
    ):
        references = re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
        references += re.findall(r'<img[^>]+src="([^"]+)"', readme)
        for reference in references:
            if reference.startswith(("http://", "https://", "#", "../../")):
                continue
            path = reference.split("#", 1)[0].split("?", 1)[0]  # 쿼리는 이미지 프록시 캐시 우회용
            assert (source.parent / path).exists(), f"broken README link: {reference}"


def test_bilingual_site_catalogs_are_structurally_distribution_safe() -> None:
    catalogs = [
        json.loads(
            (ROOT / "site" / "content" / f"{locale}.json").read_text(encoding="utf-8")
        )
        for locale in ("en", "ko")
    ]
    assert set(catalogs[0]) == set(catalogs[1])
    for catalog in catalogs:
        assert all(
            isinstance(value, str) and value.strip() for value in catalog.values()
        )
        assert all(
            "{{" not in value
            and "}}" not in value
            and "http://" not in value
            and "https://" not in value
            and "your-org" not in value
            for value in catalog.values()
        )


def test_release_workflows_pin_supply_chain_and_gate_browser_e2e() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    for workflow in (ci, release):
        uses = re.findall(r"uses:\s*[^@\s]+@([^\s#]+)", workflow)
        assert uses
        assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
        assert "playwright" not in workflow
    assert "@anthropic-ai/claude-code@2.1.235" in release
    assert "needs: quality" in release
    assert "python -m zipfile -e /tmp/xout-plugin.zip /tmp/xout-plugin" in release
    assert "claude plugin validate /tmp/xout-plugin" in release
    assert "pip install --no-index dist/*.whl" in release
    assert "/tmp/xout-wheel/bin/xout doctor" in release
    assert "scripts/check_release_version.py" in release
    assert "scripts/normalize_sdist.py" in release
    assert "scripts/build_checksums.py" in release
    assert "scripts/verify_checksums.py" in release


def test_cross_platform_checksum_verifier_fails_closed(tmp_path: Path) -> None:
    builder = _script_module("build_checksums")
    verifier = _script_module("verify_checksums")
    first = tmp_path / "first.whl"
    second = tmp_path / "plugin.zip"
    first.write_bytes(b"wheel")
    second.write_bytes(b"plugin")
    sums = tmp_path / "SHA256SUMS"
    lines = builder.build(sums, [second, first])
    assert tuple(line.split("  ", 1)[1] for line in lines) == (first.name, second.name)
    before = sums.read_bytes()
    assert builder.build(sums, [first, second]) == lines
    assert sums.read_bytes() == before
    assert verifier.verify(sums) == (first.name, second.name)
    assert verifier.verify(sums, only=[second.name]) == (second.name,)
    with pytest.raises(verifier.ChecksumError, match="MISSING_CHECKSUM_ENTRY"):
        verifier.verify(sums, only=["missing.zip"])
    second.write_bytes(b"tampered")
    with pytest.raises(verifier.ChecksumError, match="CHECKSUM_MISMATCH"):
        verifier.verify(sums)


def test_standalone_verifier_checks_partial_release_download(tmp_path: Path) -> None:
    builder = _script_module("build_checksums")
    version = _script_module("check_release_version").validate(root=ROOT)
    plugin = tmp_path / f"xout-plugin-{version}.zip"
    plugin.write_bytes(b"plugin")
    verifier = tmp_path / "verify_checksums.py"
    shutil.copyfile(ROOT / "scripts" / "verify_checksums.py", verifier)
    unneeded_wheel = tmp_path / f"popper-{version}.whl"
    unneeded_wheel.write_bytes(b"wheel")
    sums = tmp_path / "SHA256SUMS"
    builder.build(sums, [plugin, verifier, unneeded_wheel])
    unneeded_wheel.unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(verifier),
            str(sums),
            "--only",
            plugin.name,
            verifier.name,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert f"{plugin.name}: OK" in result.stdout
    assert f"{verifier.name}: OK" in result.stdout


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("", "CHECKSUM_FILE_EMPTY"),
        ("not-a-checksum\n", "MALFORMED_LINE"),
        (f"{'0' * 64}  ../escape\n", "UNSAFE_PATH"),
        (f"{'0' * 64}  duplicate.zip\n{'1' * 64}  duplicate.zip\n", "DUPLICATE_PATH"),
        (f"{'0' * 64}  absent.zip\n", "MISSING_FILE"),
    ],
)
def test_checksum_verifier_rejects_malformed_manifests(
    tmp_path: Path, body: str, error: str
) -> None:
    verifier = _script_module("verify_checksums")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(body, encoding="utf-8")
    with pytest.raises(verifier.ChecksumError, match=error):
        verifier.verify(sums)


def test_sdist_normalizer_is_deterministic_and_rejects_unsafe_members(
    tmp_path: Path,
) -> None:
    normalizer = _script_module("normalize_sdist")

    def build(path: Path, *, mtime: int, reverse: bool = False) -> None:
        directory = tarfile.TarInfo("popper-1.2.0")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o775
        directory.mtime = mtime
        payload = b"metadata"
        member = tarfile.TarInfo("popper-1.2.0/PKG-INFO")
        member.size = len(payload)
        member.mode = 0o600
        member.mtime = mtime
        entries = [(directory, None), (member, io.BytesIO(payload))]
        with tarfile.open(path, "w:gz") as archive:
            for info, stream in reversed(entries) if reverse else entries:
                archive.addfile(info, stream)

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    build(first, mtime=1)
    build(second, mtime=2, reverse=True)
    normalizer.normalize(first, epoch=normalizer.DEFAULT_EPOCH)
    normalizer.normalize(second, epoch=normalizer.DEFAULT_EPOCH)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            member.name for member in members
        )
        assert all(member.mtime == normalizer.DEFAULT_EPOCH for member in members)
        assert all(member.uid == member.gid == 0 for member in members)

    unsafe = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(normalizer.SdistNormalizationError, match="UNSAFE_MEMBER"):
        normalizer.normalize(unsafe)


def test_skill_distinguishes_servers_from_sync_diagnostics() -> None:
    skill = (ROOT / "skills" / "xout" / "SKILL.md").read_text(encoding="utf-8")
    assert "| (없음) 또는 `chat` | 포그라운드 반복 |" in skill
    assert "| `doctor` | 포그라운드 출력 |" in skill
    assert '"${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" pair' in skill
    assert '"${CLAUDE_PLUGIN_ROOT}/scripts/xout_plugin.py" enable --grant' in skill
    assert "사용자 대신 X를 치지 않는다" in skill
    assert "python3 -m xout" not in skill
    assert "--no-browser" not in skill
