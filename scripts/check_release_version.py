#!/usr/bin/env python3
"""Fail unless package, plugin, marketplace, and release tag versions agree."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_BLOCK = re.compile(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)")
VERSION_LINE = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)


class VersionContractError(ValueError):
    """Release version sources disagree or cannot be read."""


def package_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    text = pyproject.read_text(encoding="utf-8")
    project = PROJECT_BLOCK.search(text)
    if project is None:
        raise VersionContractError("MISSING_PROJECT_TABLE")
    match = VERSION_LINE.search(project.group("body"))
    if match is None:
        raise VersionContractError("MISSING_PACKAGE_VERSION")
    return match.group("version")


def declared_versions(root: Path = ROOT) -> dict[str, str]:
    plugin = json.loads(
        (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    marketplace = json.loads(
        (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    if not isinstance(plugin, Mapping) or not isinstance(marketplace, Mapping):
        raise VersionContractError("INVALID_MANIFEST_ROOT")
    plugins = marketplace.get("plugins")
    if (
        not isinstance(plugins, list)
        or len(plugins) != 1
        or not isinstance(plugins[0], Mapping)
    ):
        raise VersionContractError("INVALID_MARKETPLACE_PLUGIN_SET")
    metadata = marketplace.get("metadata")
    if not isinstance(metadata, Mapping):
        raise VersionContractError("INVALID_MARKETPLACE_METADATA")
    npm = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if not isinstance(npm, Mapping):
        raise VersionContractError("INVALID_NPM_MANIFEST")
    versions = {
        "package": package_version(root / "pyproject.toml"),
        "plugin": str(plugin.get("version", "")),
        "marketplace": str(plugins[0].get("version", "")),
        "marketplace_metadata": str(metadata.get("version", "")),
        "npm": str(npm.get("version", "")),
    }
    if not all(versions.values()):
        raise VersionContractError("EMPTY_DECLARED_VERSION")
    return versions


def validate(tag: str | None = None, root: Path = ROOT) -> str:
    versions = declared_versions(root)
    unique = set(versions.values())
    if len(unique) != 1:
        detail = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise VersionContractError(f"VERSION_MISMATCH:{detail}")
    version = next(iter(unique))
    if tag is not None and tag != f"v{version}":
        raise VersionContractError(f"TAG_MISMATCH:{tag}:v{version}")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--print-version", action="store_true")
    args = parser.parse_args()
    version = validate(args.tag)
    print(version if args.print_version else f"release version {version} aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
