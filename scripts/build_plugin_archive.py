#!/usr/bin/env python3
"""Build a deterministic Popper Claude Code plugin archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import zipfile

ROOT_FILES = (
    "LICENSE",
    "README.md",
    "README.ko.md",
    "pyproject.toml",
    "scripts/popper_plugin.py",
)
DIRECTORIES = (".claude-plugin", "skills", "xout")
EXCLUDED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    "test",
    "dist",
    "build",
    ".git",
}
EPOCH = (1980, 1, 1, 0, 0, 0)


def archive_members(root: Path) -> list[Path]:
    members: list[Path] = []
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(path)
        members.append(path)
    for directory in DIRECTORIES:
        base = root / directory
        if not base.is_dir():
            raise FileNotFoundError(base)
        members.extend(
            path
            for path in base.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts)
            and path.name not in {".coverage", ".DS_Store"}
            and path.suffix not in {".pyc", ".pyo"}
        )
    return sorted(members, key=lambda path: path.relative_to(root).as_posix())


def build_archive(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in archive_members(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())


def declared_version(root: Path) -> str:
    manifest = json.loads(
        (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("plugin.json version is missing")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or Path("dist") / f"popper-plugin-{declared_version(root)}.zip"
    build_archive(root, output)
    print(output)


if __name__ == "__main__":
    main()
