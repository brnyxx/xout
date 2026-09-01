"""Bilingual Pages, brand asset, SEO, accessibility, and workflow contracts."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import struct
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://pages.example.test/popper/"
REPOSITORY_URL = "https://github.com/example/popper"
EXPECTED_ARTIFACTS = {
    ".nojekyll",
    "index.html",
    "ko/index.html",
    "robots.txt",
    "sitemap.xml",
    "assets/site.css",
    "assets/logo.svg",
    "assets/hero.svg",
    "assets/social-card.png",
    "assets/demo.gif",
    "assets/demo.en.gif",
}


def _script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.structure: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag in {"header", "nav", "main", "section", "footer", "h1", "h2", "h3"}:
            self.structure.append((tag, values.get("id", ""), values.get("class", "")))


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def _gif_timeline(payload: bytes) -> tuple[int, int, int, int]:
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    width, height = struct.unpack("<HH", payload[6:10])
    packed = payload[10]
    position = 13
    if packed & 0x80:
        position += 3 * (2 ** ((packed & 0x07) + 1))
    frames = 0
    duration_centiseconds = 0
    pending_delay = 0
    while position < len(payload):
        marker = payload[position]
        position += 1
        if marker == 0x3B:
            break
        if marker == 0x21:
            label = payload[position]
            position += 1
            if label == 0xF9:
                block_size = payload[position]
                position += 1
                block = payload[position : position + block_size]
                position += block_size
                pending_delay = int.from_bytes(block[1:3], "little")
                assert payload[position] == 0
                position += 1
                continue
            if label in {0x01, 0xFF}:
                header_size = payload[position]
                position += 1 + header_size
            while True:
                block_size = payload[position]
                position += 1
                if block_size == 0:
                    break
                position += block_size
            continue
        assert marker == 0x2C
        descriptor = payload[position : position + 9]
        position += 9
        if descriptor[8] & 0x80:
            position += 3 * (2 ** ((descriptor[8] & 0x07) + 1))
        position += 1
        while True:
            block_size = payload[position]
            position += 1
            if block_size == 0:
                break
            position += block_size
        frames += 1
        duration_centiseconds += pending_delay
        pending_delay = 0
    assert position == len(payload)
    return width, height, frames, duration_centiseconds


def _copy_site_source(destination: Path) -> Path:
    shutil.copytree(ROOT / "site", destination / "site")
    shutil.copytree(ROOT / ".github" / "assets", destination / ".github" / "assets")
    shutil.copyfile(ROOT / "pyproject.toml", destination / "pyproject.toml")
    return destination / "site"


def test_site_source_has_exact_locale_and_placeholder_parity() -> None:
    builder = _script("build_site")
    template = (ROOT / "site" / "template.html").read_text(encoding="utf-8")
    catalogs = builder.load_content(ROOT / "site" / "content")
    locale_keys = builder.template_keys(template) - builder.BUILD_KEYS

    assert set(catalogs["en"]) == set(catalogs["ko"]) == set(locale_keys)
    assert template.lower().count("<h1") == 1
    assert "<script" not in template.lower()
    assert "<form" not in template.lower()
    assert "<del>" in template
    assert "6,561" not in template  # the visible figure is localized but parity-checked
    for key in ("open_command", "enable_command", "status_command"):
        assert catalogs["en"][key] == catalogs["ko"][key]


def test_site_build_is_deterministic_bilingual_and_bounded(tmp_path: Path) -> None:
    builder = _script("build_site")
    first = tmp_path / "first"
    second = tmp_path / "second"
    builder.build(first, site_url=SITE_URL, repository_url=REPOSITORY_URL)
    builder.build(second, site_url=SITE_URL, repository_url=REPOSITORY_URL)

    assert builder.tree_digest(first) == builder.tree_digest(second)
    assert {
        path.relative_to(first).as_posix()
        for path in first.rglob("*")
        if path.is_file()
    } == EXPECTED_ARTIFACTS
    assert all(
        record[1:3] == (0o644, builder.FIXED_EPOCH)
        for record in builder.tree_digest(first)
    )
    assert (
        (first / "robots.txt")
        .read_text(encoding="utf-8")
        .endswith(f"Sitemap: {SITE_URL}sitemap.xml\n")
    )
    sitemap = (first / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.index(f"<loc>{SITE_URL}</loc>") < sitemap.index(
        f"<loc>{SITE_URL}ko/</loc>"
    )


def test_rendered_routes_have_matching_structure_links_and_seo(tmp_path: Path) -> None:
    builder = _script("build_site")
    output = tmp_path / "site"
    builder.build(output, site_url=SITE_URL, repository_url=REPOSITORY_URL)
    english = (output / "index.html").read_text(encoding="utf-8")
    korean = (output / "ko/index.html").read_text(encoding="utf-8")

    en_outline = Outline()
    ko_outline = Outline()
    en_outline.feed(english)
    ko_outline.feed(korean)
    assert en_outline.structure == ko_outline.structure
    assert [item[1] for item in en_outline.structure if item[0] == "section"] == [
        "hero",
        "outcome",
        "mechanism",
        "start",
        "boundaries",
        "share",
    ]
    assert f'<link rel="canonical" href="{SITE_URL}">' in english
    assert f'<link rel="canonical" href="{SITE_URL}ko/">' in korean
    assert (
        f'<meta property="og:image" content="{SITE_URL}assets/social-card.png">'
        in english
    )
    assert '<meta property="og:image:width" content="1200">' in english
    assert '<meta property="og:image:height" content="630">' in english
    assert "Content-Security-Policy\" content=\"default-src 'none'" in english
    assert '<link rel="stylesheet" href="../assets/site.css">' in korean
    assert '<link rel="icon" href="../assets/logo.svg"' in korean
    assert 'aria-current="page"' in english and 'aria-current="page"' in korean
    assert REPOSITORY_URL + "/releases" in english
    assert "{{" not in english + korean
    assert "6,561 (3⁸; 8 axes, 3 values each)" in english
    assert "6,561 (3⁸; 8축, 축당 3값)" in korean
    assert "4,374" in english + korean
    assert "/xout:xout enable" in english and "/xout:xout enable" in korean
    assert (
        "verify_checksums.py SHA256SUMS --only "
        f"xout-plugin-{builder.project_version()}.zip" in english
    )
    assert "pip install xout" in english
    assert 'class="storyboard" aria-hidden="true"' in english
    assert "--lang en" in english
    assert "strike-backed axes" in english
    assert (
        "자율성, 범위 준수, 테스트 규율, 주석과 문서화, 에러 시 행동, 커밋 정책, 완료 전 검증, 의존성 정책" in korean
    )


def test_brand_assets_are_local_deterministic_and_social_ready(tmp_path: Path) -> None:
    social = _script("build_social_card")
    generated = tmp_path / "social-card.png"
    original_argv = list(social.sys.argv)
    try:
        social.sys.argv = ["build_social_card.py", str(generated)]
        social.main()
    finally:
        social.sys.argv = original_argv
    checked_in = ROOT / ".github" / "assets" / "social-card.png"
    assert generated.read_bytes() == checked_in.read_bytes()
    payload = checked_in.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", payload[16:24]) == (1200, 630)
    for gif_name in ("demo.gif", "demo.en.gif"):
        demo = (ROOT / ".github" / "assets" / gif_name).read_bytes()
        assert _gif_timeline(demo) == (960, 608, 49, 2311)
    for name, dimensions in (
        ("logo.svg", 'viewBox="0 0 256 256"'),
        ("hero.svg", 'viewBox="0 0 1200 420"'),
    ):
        body = (ROOT / ".github" / "assets" / name).read_text(encoding="utf-8")
        assert dimensions in body
        assert "<script" not in body.lower()
        assert not re.search(r"(?:xlink:)?href\s*=", body, re.I)
        assert "check" not in body.lower()
        assert body.count("#D92332") == 1
    hero = (ROOT / ".github" / "assets" / "hero.svg").read_text(encoding="utf-8")
    for message in (
        "FIX THE BUG.",
        "SHOULD I START?",
        "FIXED. TESTS PASS.",
        "ACT FIRST.",
        "REPORT AFTER.",
        "X OUT THE AI BEHAVIOR YOU NEVER WANT AGAIN",
    ):
        assert message in hero


def test_site_css_meets_responsive_theme_focus_and_contrast_contract() -> None:
    css = (ROOT / "site" / "assets" / "site.css").read_text(encoding="utf-8")
    active = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    compact = re.sub(r"\s+", " ", active)
    root = re.search(r":root\s*\{([^}]+)\}", active)
    assert root is not None
    variables = dict(re.findall(r"--([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})", root.group(1)))
    assert _contrast(variables["ink"], variables["paper"]) >= 4.5
    assert _contrast(variables["muted"], variables["paper"]) >= 4.5
    assert _contrast(variables["accent"], variables["paper"]) >= 4.5
    dark_source = active.split("@media (prefers-color-scheme: dark)", 1)[1]
    dark_root = re.search(r":root\s*\{([^}]+)\}", dark_source)
    assert dark_root is not None
    dark_variables = dict(
        re.findall(r"--([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})", dark_root.group(1))
    )
    assert _contrast(dark_variables["ink"], dark_variables["paper"]) >= 4.5
    assert _contrast(dark_variables["muted"], dark_variables["paper"]) >= 4.5
    focus = re.search(r"a:focus-visible\s*\{([^}]+)\}", active)
    assert focus is not None and "outline: 3px solid var(--survivor)" in focus.group(1)
    assert "color-scheme: light" in root.group(1)
    assert "@media (prefers-color-scheme: dark)" in active
    assert "@media (prefers-reduced-motion: reduce)" in active
    assert "animation: story-next 4.8s both" in compact
    assert "infinite" not in active
    assert ".story-falsified .strike-line" in active
    assert ".story-rule, .story-next { opacity: 1; transform: none; }" in compact
    assert "min-height: 44px" in active
    assert "@media (max-width: 760px)" in active
    assert "overflow-x: auto" in active
    assert "url(" not in active and "@import" not in active
    assert "grid-template-columns: 1fr" in compact


def test_builder_rejects_invalid_urls_catalog_drift_and_missing_assets(
    tmp_path: Path,
) -> None:
    builder = _script("build_site")
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(builder.SiteBuildError, match="INVALID_SITE_URL"):
        builder.build(
            output, site_url="http://example.test/", repository_url=REPOSITORY_URL
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"

    source_root = tmp_path / "source"
    source = _copy_site_source(source_root)
    korean_path = source / "content" / "ko.json"
    korean = json.loads(korean_path.read_text(encoding="utf-8"))
    korean.pop("hero_title")
    korean_path.write_text(json.dumps(korean, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(builder.SiteBuildError, match="LOCALE_KEY_MISMATCH"):
        builder.build(
            tmp_path / "drift",
            site_url=SITE_URL,
            repository_url=REPOSITORY_URL,
            source=source,
        )

    source = _copy_site_source(tmp_path / "missing")
    (source.parent / ".github" / "assets" / "hero.svg").unlink()
    with pytest.raises(builder.SiteBuildError, match="MISSING_ASSET:hero.svg"):
        builder.build(
            tmp_path / "missing-output",
            site_url=SITE_URL,
            repository_url=REPOSITORY_URL,
            source=source,
        )


@pytest.mark.parametrize(
    ("site_url", "repository_url", "error"),
    [
        ("https://example.test/a b", REPOSITORY_URL, "INVALID_SITE_URL"),
        ("https://example.test/%ZZ", REPOSITORY_URL, "INVALID_SITE_URL"),
        ("https://example.test/\x00", REPOSITORY_URL, "INVALID_SITE_URL"),
        ("https://example.test:99999/", REPOSITORY_URL, "INVALID_SITE_URL"),
        (SITE_URL, "https://user@example.test/repo", "INVALID_REPOSITORY_URL"),
        (SITE_URL, "https://-bad.example/repo", "INVALID_REPOSITORY_URL"),
    ],
)
def test_builder_rejects_malformed_absolute_urls(
    tmp_path: Path, site_url: str, repository_url: str, error: str
) -> None:
    builder = _script("build_site")
    with pytest.raises(builder.SiteBuildError, match=error):
        builder.build(
            tmp_path / "output",
            site_url=site_url,
            repository_url=repository_url,
        )


@pytest.mark.parametrize(
    ("injection", "error"),
    [
        (
            '<link rel="stylesheet" href="https://attacker.example/style.css">',
            "remote-subresource",
        ),
        (
            '<meta http-equiv="refresh" content="0;https://attacker.example">',
            "meta-refresh",
        ),
        ('<base href="https://attacker.example/">', "semantic-contract"),
        (
            '<img src="assets/logo.svg" srcset="https://attacker.example/logo.png 2x">',
            "fetch-attribute",
        ),
        (
            '<p style="background:url(https://attacker.example/pixel)">x</p>',
            "fetch-attribute",
        ),
    ],
)
def test_rendered_validator_rejects_remote_fetch_surfaces(
    tmp_path: Path, injection: str, error: str
) -> None:
    builder = _script("build_site")
    source = _copy_site_source(tmp_path / "source")
    template = source / "template.html"
    body = template.read_text(encoding="utf-8")
    template.write_text(
        body.replace("</head>", injection + "\n</head>"), encoding="utf-8"
    )
    with pytest.raises(builder.SiteBuildError, match=error):
        builder.build(
            tmp_path / "output",
            site_url=SITE_URL,
            repository_url=REPOSITORY_URL,
            source=source,
        )


def test_builder_rejects_source_symlinks_and_extra_output_directories(
    tmp_path: Path,
) -> None:
    builder = _script("build_site")
    source = _copy_site_source(tmp_path / "source")
    external = tmp_path / "external.css"
    external.write_text(":root { color: black; }\n", encoding="utf-8")
    css = source / "assets" / "site.css"
    css.unlink()
    try:
        css.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(builder.SiteBuildError, match="SOURCE_SYMLINK"):
        builder.build(
            tmp_path / "symlink-output",
            site_url=SITE_URL,
            repository_url=REPOSITORY_URL,
            source=source,
        )

    output = tmp_path / "valid"
    builder.build(output, site_url=SITE_URL, repository_url=REPOSITORY_URL)
    (output / "unexpected").mkdir()
    with pytest.raises(builder.SiteBuildError, match="directory-allowlist"):
        builder.validate_tree(output, REPOSITORY_URL, SITE_URL)


def test_promotion_preserves_unowned_siblings_and_refuses_active_lock(
    tmp_path: Path,
) -> None:
    builder = _script("build_site")
    output = tmp_path / "pages"
    builder.build(output, site_url=SITE_URL, repository_url=REPOSITORY_URL)
    unowned = tmp_path / ".pages.previous"
    unowned.mkdir()
    sentinel = unowned / "important"
    sentinel.write_text("keep", encoding="utf-8")

    replacement_url = "https://pages.example.test/popper-next/"
    builder.build(
        output,
        site_url=replacement_url,
        repository_url=REPOSITORY_URL,
    )
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert replacement_url in (output / "index.html").read_text(encoding="utf-8")

    before = builder.tree_digest(output)
    lock = tmp_path / ".pages.promotion-lock"
    lock.mkdir()
    with pytest.raises(builder.SiteBuildError, match="OUTPUT_PROMOTION_BUSY"):
        builder.build(output, site_url=SITE_URL, repository_url=REPOSITORY_URL)
    assert builder.tree_digest(output) == before
    assert lock.is_dir()


def test_pages_workflow_is_sha_pinned_least_privilege_and_deploy_gated() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
        encoding="utf-8"
    )
    document = yaml.load(workflow, Loader=yaml.BaseLoader)
    assert set(document["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"] == {
        "group": "pages",
        "cancel-in-progress": "false",
    }
    assert set(document["jobs"]) == {"verify", "deploy"}
    verify = document["jobs"]["verify"]
    deploy = document["jobs"]["deploy"]
    assert verify["permissions"] == {"contents": "read"}
    assert deploy["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert deploy["needs"] == "verify"
    assert (
        deploy["if"]
        == "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'"
    )
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }

    def actions(job: dict) -> dict[str, str]:
        result: dict[str, str] = {}
        for step in job["steps"]:
            use = step.get("uses")
            if use is None:
                continue
            name, revision = use.rsplit("@", 1)
            assert re.fullmatch(r"[0-9a-f]{40}", revision)
            result[name] = revision
        return result

    assert set(actions(verify)) == {"actions/checkout", "actions/setup-python"}
    assert set(actions(deploy)) == {
        "actions/checkout",
        "actions/setup-python",
        "actions/configure-pages",
        "actions/upload-pages-artifact",
        "actions/deploy-pages",
    }
    configure = next(step for step in deploy["steps"] if step.get("id") == "pages")
    assert configure["uses"].startswith("actions/configure-pages@")
    build_step = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Build deterministic bilingual site"
    )
    assert "python scripts/build_site.py" in build_step["run"]
    assert "${{ steps.pages.outputs.base_url }}" in build_step["run"]
    upload = next(
        step
        for step in deploy["steps"]
        if (step.get("uses") or "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["with"]["path"] == "${{ runner.temp }}/xout-pages"
    verify_runs = "\n".join(step["run"] for step in verify["steps"] if "run" in step)
    assert 'python -m pip install ".[test]"' in verify_runs
    assert "python -m pytest tests/test_site_contract.py -q" in verify_runs
    assert "playwright" not in verify_runs
