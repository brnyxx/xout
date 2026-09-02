#!/usr/bin/env python3
"""Build and validate xout's deterministic bilingual GitHub Pages site."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import struct
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = ROOT / "site"
LOCALES = ("en", "ko", "ja", "zh")
NPM_URL = "https://www.npmjs.com/package/@brnyxx/xout"
PYPI_URL = "https://pypi.org/project/xout/"
OG_LOCALE = {"en": "en_US", "ko": "ko_KR", "ja": "ja_JP", "zh": "zh_CN"}
PLACEHOLDER = re.compile(r"{{([a-z][a-z0-9_]*)}}")
BUILD_KEYS = frozenset(
    {
        "lang",
        "asset_prefix",
        "en_href",
        "ko_href",
        "en_current",
        "ko_current",
        "canonical_url",
        "en_url",
        "ko_url",
        "ja_url",
        "zh_url",
        "ja_href",
        "zh_href",
        "ja_current",
        "zh_current",
        "repository_url",
        "repository_readme_url",
        "repository_releases_url",
        "npm_url",
        "pypi_url",
        "og_locale",
        "og_locale_alt",
        "social_image_url",
        "version",
    }
)
ASSETS = (
    (Path("assets/site.css"), Path("assets/site.css")),
    (Path("../.github/assets/logo.svg"), Path("assets/logo.svg")),
    (Path("../.github/assets/hero.svg"), Path("assets/hero.svg")),
    (Path("../.github/assets/how-it-works.gif"), Path("assets/how-it-works.gif")),
    (Path("../.github/assets/how-it-works.ko.gif"), Path("assets/how-it-works.ko.gif")),
    (Path("../.github/assets/how-it-works.ja.gif"), Path("assets/how-it-works.ja.gif")),
    (Path("../.github/assets/how-it-works.zh.gif"), Path("assets/how-it-works.zh.gif")),
    (Path("../.github/assets/social-card.png"), Path("assets/social-card.png")),
    (Path("../.github/assets/social-card.ko.png"), Path("assets/social-card.ko.png")),
    (Path("../.github/assets/social-card.ja.png"), Path("assets/social-card.ja.png")),
    (Path("../.github/assets/social-card.zh.png"), Path("assets/social-card.zh.png")),
    (Path("../.github/assets/demo.gif"), Path("assets/demo.gif")),
    (Path("../.github/assets/demo.en.gif"), Path("assets/demo.en.gif")),
    (Path("../.github/assets/demo.ja.gif"), Path("assets/demo.ja.gif")),
    (Path("../.github/assets/demo.zh.gif"), Path("assets/demo.zh.gif")),
)
ARTIFACT_FILES = frozenset(
    {
        ".nojekyll",
        "index.html",
        "ko/index.html",
        "ja/index.html",
        "zh/index.html",
        "robots.txt",
        "sitemap.xml",
        "assets/site.css",
        "assets/logo.svg",
        "assets/hero.svg",
        "assets/how-it-works.gif",
        "assets/how-it-works.ko.gif",
        "assets/how-it-works.ja.gif",
        "assets/how-it-works.zh.gif",
        "assets/social-card.png",
        "assets/social-card.ko.png",
        "assets/social-card.ja.png",
        "assets/social-card.zh.png",
        "assets/demo.gif",
        "assets/demo.en.gif",
        "assets/demo.ja.gif",
        "assets/demo.zh.gif",
    }
)
PROHIBITED_TAGS = frozenset(
    {"base", "embed", "form", "iframe", "object", "script", "source", "style"}
)
PROHIBITED_FETCH_ATTRIBUTES = frozenset(
    {"action", "data", "formaction", "poster", "srcset", "style"}
)
ARTIFACT_DIRECTORIES = frozenset({"assets", "ko", "ja", "zh"})
FIXED_EPOCH = 315532800
PROJECT_BLOCK = re.compile(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)")
VERSION_LINE = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)


class SiteBuildError(ValueError):
    """The static site source or generated artifact violates its contract."""


class _RouteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.references: list[tuple[str, str, str, dict[str, str | None]]] = []
        self.tags: list[str] = []
        self.h1_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise SiteBuildError("INVALID_RENDERED_ROUTE:duplicate-attribute")
        self.tags.append(tag)
        if tag == "h1":
            self.h1_count += 1
        identifier = attributes.get("id")
        if identifier:
            self.ids.append(identifier)
        for name in ("href", "src"):
            value = attributes.get(name)
            if value:
                self.references.append((tag, name, value, attributes))
        for name in attributes:
            if name.lower().startswith("on"):
                raise SiteBuildError("INVALID_RENDERED_ROUTE:inline-handler")
            if name.lower() in PROHIBITED_FETCH_ATTRIBUTES:
                raise SiteBuildError("INVALID_RENDERED_ROUTE:fetch-attribute")
        if tag == "meta" and (attributes.get("http-equiv") or "").lower() == "refresh":
            raise SiteBuildError("INVALID_RENDERED_ROUTE:meta-refresh")


def _absolute_https(value: str, error: str, *, trailing_slash: bool) -> str:
    if (
        value != value.strip()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or "\\" in value
        or re.search(r"%(?:[^0-9A-Fa-f]|$)|%[0-9A-Fa-f](?:[^0-9A-Fa-f]|$)", value)
    ):
        raise SiteBuildError(error)
    parsed = urlparse(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise SiteBuildError(error) from exc
    if parsed.scheme != "https" or not parsed.netloc or hostname is None:
        raise SiteBuildError(error)
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SiteBuildError(error) from exc
    if ":" not in hostname:
        labels = hostname.rstrip(".").split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[A-Za-z0-9-]+", label) is None
            for label in labels
        ):
            raise SiteBuildError(error)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.path and not parsed.path.startswith("/"))
    ):
        raise SiteBuildError(error)
    normalized = value.rstrip("/")
    return normalized + "/" if trailing_slash else normalized


def project_version(root: Path = ROOT) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = PROJECT_BLOCK.search(text)
    if project is None:
        raise SiteBuildError("MISSING_PROJECT_VERSION")
    version = VERSION_LINE.search(project.group("body"))
    if version is None:
        raise SiteBuildError("MISSING_PROJECT_VERSION")
    return version.group("version")


def load_content(content_dir: Path) -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for locale in LOCALES:
        path = content_dir / f"{locale}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError) as exc:
            raise SiteBuildError(f"INVALID_LOCALE:{locale}") from exc
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and bool(value.strip())
            for key, value in payload.items()
        ):
            raise SiteBuildError(f"INVALID_LOCALE_VALUES:{locale}")
        if any(
            "{{" in value
            or "}}" in value
            or "<" in value
            or ">" in value
            or "http://" in value
            or "https://" in value
            for value in payload.values()
        ):
            raise SiteBuildError(f"INVALID_LOCALE_VALUES:{locale}")
        catalogs[locale] = payload
    if any(set(catalogs["en"]) != set(catalogs[locale]) for locale in LOCALES[1:]):
        raise SiteBuildError("LOCALE_KEY_MISMATCH")
    return catalogs


def template_keys(template: str) -> frozenset[str]:
    keys = frozenset(PLACEHOLDER.findall(template))
    remainder = PLACEHOLDER.sub("", template)
    if "{{" in remainder or "}}" in remainder:
        raise SiteBuildError("MALFORMED_PLACEHOLDER")
    return keys


def _build_values(
    *,
    locale: str,
    site_url: str,
    repository_url: str,
    version: str,
) -> dict[str, str]:
    urls = {loc: site_url + ("" if loc == "en" else f"{loc}/") for loc in LOCALES}

    def href(target: str) -> str:
        if target == locale:
            return "./"
        if target == "en":
            return "../"
        return f"{target}/" if locale == "en" else f"../{target}/"

    values = {
        "lang": locale,
        "asset_prefix": "" if locale == "en" else "../",
        "canonical_url": urls[locale],
        "repository_url": repository_url,
        "repository_readme_url": repository_url + "#readme",
        "repository_releases_url": repository_url + "/releases",
        "npm_url": NPM_URL,
        "pypi_url": PYPI_URL,
        "og_locale": OG_LOCALE[locale],
        "og_locale_alt": "ko_KR" if locale == "en" else "en_US",
        "social_image_url": site_url + "assets/" + ("social-card.png" if locale == "en" else f"social-card.{locale}.png"),
        "version": version,
    }
    for loc in LOCALES:
        values[f"{loc}_url"] = urls[loc]
        values[f"{loc}_href"] = href(loc)
        values[f"{loc}_current"] = "page" if loc == locale else "false"
    return values


def render(
    template: str,
    catalog: Mapping[str, str],
    *,
    locale: str,
    site_url: str,
    repository_url: str,
    version: str,
) -> str:
    placeholders = template_keys(template)
    locale_keys = placeholders - BUILD_KEYS
    missing = locale_keys - set(catalog)
    extra = set(catalog) - locale_keys
    if missing:
        raise SiteBuildError(f"MISSING_LOCALE_KEY:{','.join(sorted(missing))}")
    if extra:
        raise SiteBuildError(f"UNUSED_LOCALE_KEY:{','.join(sorted(extra))}")
    build_values = _build_values(
        locale=locale,
        site_url=site_url,
        repository_url=repository_url,
        version=version,
    )

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in build_values:
            return html.escape(build_values[key], quote=True)
        if key in catalog:
            return html.escape(catalog[key], quote=True)
        raise SiteBuildError(f"UNKNOWN_PLACEHOLDER:{key}")

    output = PLACEHOLDER.sub(replace, template)
    if "{{" in output or "}}" in output:
        raise SiteBuildError("MALFORMED_PLACEHOLDER")
    return output.rstrip() + "\n"


def _resolve_local(route: Path, value: str, root: Path) -> Path:
    without_fragment = value.split("#", 1)[0]
    if not without_fragment:
        return route
    if without_fragment.startswith("/"):
        raise SiteBuildError("INVALID_RENDERED_ROUTE:root-relative-url")
    candidate = (route.parent / without_fragment).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise SiteBuildError("INVALID_RENDERED_ROUTE:path-escape")
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate


def _validate_route(
    path: Path,
    root: Path,
    repository_url: str,
    canonical_urls: frozenset[str],
) -> None:
    body = path.read_text(encoding="utf-8")
    if "{{" in body or "}}" in body:
        raise SiteBuildError("INVALID_RENDERED_ROUTE:placeholder")
    parser = _RouteParser()
    parser.feed(body)
    if parser.h1_count != 1 or any(tag in PROHIBITED_TAGS for tag in parser.tags):
        raise SiteBuildError("INVALID_RENDERED_ROUTE:semantic-contract")
    if len(parser.ids) != len(set(parser.ids)):
        raise SiteBuildError("INVALID_RENDERED_ROUTE:duplicate-id")
    ids = set(parser.ids)
    allowed_external = {
        repository_url,
        repository_url + "#readme",
        repository_url + "/releases",
        NPM_URL,
        PYPI_URL,
        "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement",
    }
    for tag, attribute, value, attributes in parser.references:
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            if tag == "a" and attribute == "href" and value in allowed_external:
                continue
            rel = frozenset((attributes.get("rel") or "").lower().split())
            if (
                tag == "link"
                and attribute == "href"
                and value in canonical_urls
                and rel in {frozenset({"canonical"}), frozenset({"alternate"})}
            ):
                continue
            raise SiteBuildError("INVALID_RENDERED_ROUTE:remote-subresource")
        target = _resolve_local(path, value, root)
        fragment = value.split("#", 1)[1] if "#" in value else ""
        if value.startswith("#"):
            if fragment not in ids:
                raise SiteBuildError("INVALID_RENDERED_ROUTE:missing-fragment")
        elif not target.is_file():
            raise SiteBuildError("INVALID_RENDERED_ROUTE:missing-local-target")


def _validate_assets(root: Path) -> None:
    for card in ("social-card.png", "social-card.ko.png", "social-card.ja.png", "social-card.zh.png"):
        png = (root / "assets" / card).read_bytes()
        if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
            raise SiteBuildError(f"INVALID_ARTIFACT_TREE:{card}")
        width, height = struct.unpack(">II", png[16:24])
        if (width, height) != (1200, 630):
            raise SiteBuildError(f"INVALID_ARTIFACT_TREE:{card}-size")
    for gif_name in ("assets/demo.gif", "assets/demo.en.gif", "assets/demo.ja.gif", "assets/demo.zh.gif"):
        demo = (root / gif_name).read_bytes()
        if len(demo) < 10 or demo[:6] not in {b"GIF87a", b"GIF89a"}:
            raise SiteBuildError(f"INVALID_ARTIFACT_TREE:{gif_name}")
        if struct.unpack("<HH", demo[6:10]) != (960, 608):
            raise SiteBuildError("INVALID_ARTIFACT_TREE:demo-size")
    for gif_name in ("assets/how-it-works.gif", "assets/how-it-works.ko.gif", "assets/how-it-works.ja.gif", "assets/how-it-works.zh.gif"):
        motion = (root / gif_name).read_bytes()
        if len(motion) < 10 or motion[:6] not in {b"GIF87a", b"GIF89a"}:
            raise SiteBuildError(f"INVALID_ARTIFACT_TREE:{gif_name}")
        if struct.unpack("<HH", motion[6:10]) != (960, 540):
            raise SiteBuildError("INVALID_ARTIFACT_TREE:how-it-works-size")
    for name in ("logo.svg", "hero.svg"):
        svg = (root / "assets" / name).read_text(encoding="utf-8").lower()
        if (
            "<script" in svg
            or "<image" in svg
            or re.search(r"(?:xlink:)?href\s*=", svg)
            or re.search(r"url\s*\(", svg)
        ):
            raise SiteBuildError(f"INVALID_ARTIFACT_TREE:{name}")
    css = (root / "assets/site.css").read_text(encoding="utf-8").lower()
    if "url(" in css or "@import" in css or "javascript:" in css:
        raise SiteBuildError("INVALID_ARTIFACT_TREE:site.css")


def validate_tree(root: Path, repository_url: str, site_url: str) -> None:
    files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if files != ARTIFACT_FILES or any(path.is_symlink() for path in root.rglob("*")):
        raise SiteBuildError("INVALID_ARTIFACT_TREE:allowlist")
    directories = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and not path.is_symlink()
    }
    if directories != ARTIFACT_DIRECTORIES:
        raise SiteBuildError("INVALID_ARTIFACT_TREE:directory-allowlist")
    canonical_urls = frozenset(
        site_url + ("" if locale == "en" else f"{locale}/") for locale in LOCALES
    )
    for route in (root / "index.html", root / "ko/index.html"):
        _validate_route(route, root, repository_url, canonical_urls)
    _validate_assets(root)


def _source_file(source: Path, relative: Path) -> Path:
    bundle = source.parent.absolute()
    candidate = Path(os.path.abspath(source / relative))
    if candidate == bundle or bundle not in candidate.parents:
        raise SiteBuildError(f"SOURCE_PATH_ESCAPE:{relative.as_posix()}")
    current = bundle
    if current.is_symlink():
        raise SiteBuildError(f"SOURCE_SYMLINK:{relative.as_posix()}")
    for part in candidate.relative_to(bundle).parts:
        current /= part
        if current.is_symlink():
            raise SiteBuildError(f"SOURCE_SYMLINK:{relative.as_posix()}")
    if not candidate.is_file():
        raise SiteBuildError(f"MISSING_SOURCE_FILE:{relative.as_posix()}")
    return candidate


def _write_tree(
    staging: Path,
    *,
    source: Path,
    site_url: str,
    repository_url: str,
    version: str,
) -> None:
    template_path = _source_file(source, Path("template.html"))
    for locale in LOCALES:
        _source_file(source, Path(f"content/{locale}.json"))
    template = template_path.read_text(encoding="utf-8")
    catalogs = load_content(source / "content")
    for locale in LOCALES[1:]:
        (staging / locale).mkdir(parents=True)
    (staging / "assets").mkdir()
    for locale in LOCALES:
        route = staging / ("index.html" if locale == "en" else f"{locale}/index.html")
        route.write_text(
            render(
                template,
                catalogs[locale],
                locale=locale,
                site_url=site_url,
                repository_url=repository_url,
                version=version,
            ),
            encoding="utf-8",
            newline="\n",
        )
    for relative_source, destination in ASSETS:
        try:
            source_asset = _source_file(source, relative_source)
        except SiteBuildError as exc:
            if str(exc).startswith("MISSING_SOURCE_FILE:"):
                raise SiteBuildError(f"MISSING_ASSET:{destination.name}") from exc
            raise
        shutil.copyfile(source_asset, staging / destination)
    (staging / ".nojekyll").write_bytes(b"")
    (staging / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {site_url}sitemap.xml\n",
        encoding="utf-8",
        newline="\n",
    )
    (staging / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(
            f"  <url><loc>{html.escape(site_url + ('' if loc == 'en' else loc + '/'))}</loc></url>\n"
            for loc in LOCALES
        )
        + "</urlset>\n",
        encoding="utf-8",
        newline="\n",
    )
    validate_tree(staging, repository_url, site_url)
    for path in sorted(staging.rglob("*"), reverse=True):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
        os.utime(path, (FIXED_EPOCH, FIXED_EPOCH))
    os.chmod(staging, 0o755)
    os.utime(staging, (FIXED_EPOCH, FIXED_EPOCH))


def _validate_paths(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise SiteBuildError("INVALID_SOURCE_DIRECTORY")
    if destination.is_symlink():
        raise SiteBuildError("OUTPUT_SYMLINK_UNSUPPORTED")
    source_path = source.resolve()
    destination_path = destination.resolve(strict=False)
    if (
        source_path == destination_path
        or source_path in destination_path.parents
        or destination_path in source_path.parents
    ):
        raise SiteBuildError("SOURCE_OUTPUT_OVERLAP")
    if destination_path.exists() and not destination_path.is_dir():
        raise SiteBuildError("OUTPUT_NOT_DIRECTORY")


def build(
    output: Path,
    *,
    site_url: str,
    repository_url: str,
    source: Path = SITE_ROOT,
) -> None:
    normalized_site = _absolute_https(site_url, "INVALID_SITE_URL", trailing_slash=True)
    normalized_repository = _absolute_https(
        repository_url, "INVALID_REPOSITORY_URL", trailing_slash=False
    )
    source_path = source.expanduser().absolute()
    destination = output.expanduser().absolute()
    _validate_paths(source_path, destination)
    _source_file(source_path, Path("../pyproject.toml"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.build-", dir=destination.parent)
    )
    promotion_lock = destination.with_name(f".{destination.name}.promotion-lock")
    lock_owned = False
    backup: Path | None = None
    try:
        _write_tree(
            staging,
            source=source_path,
            site_url=normalized_site,
            repository_url=normalized_repository,
            version=project_version(source_path.parent),
        )
        try:
            promotion_lock.mkdir(mode=0o700)
            lock_owned = True
        except FileExistsError as exc:
            raise SiteBuildError("OUTPUT_PROMOTION_BUSY") from exc
        _validate_paths(source_path, destination)
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.previous-",
                    dir=destination.parent,
                )
            )
            backup.rmdir()
            destination.replace(backup)
        try:
            staging.replace(destination)
        except BaseException:
            if backup is not None and backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if lock_owned:
            promotion_lock.rmdir()


def tree_digest(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    """Return deterministic file metadata for tests and release diagnostics."""
    records: list[tuple[str, int, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            mode = stat.st_mode & 0o777
            if os.name == "nt":
                mode = 0o644 if os.access(path, os.W_OK) else 0o444
            records.append(
                (
                    path.relative_to(root).as_posix(),
                    mode,
                    int(stat.st_mtime),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return tuple(records)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--repository-url", required=True)
    args = parser.parse_args(argv)
    build(
        args.output,
        site_url=args.site_url,
        repository_url=args.repository_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
