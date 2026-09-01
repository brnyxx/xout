"""Browser-level coverage of the product cold-open flow."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

URL_RE = re.compile(r"(https?://127\.0\.0\.1:\d+/)")


def _start_server(
    base_dir: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
    extra_args: list[str] | None = None,
    operation: str = "open",
) -> tuple[subprocess.Popen[str], str]:
    prefix = command or [sys.executable, "-m", "xout"]
    process = subprocess.Popen(
        [
            *prefix,
            operation,
            "--no-browser",
            "--base-dir",
            str(base_dir),
            *(extra_args or []),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
    )
    assert process.stderr is not None
    deadline = time.monotonic() + 15
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if line:
            lines.append(line)
            match = URL_RE.search(line)
            if match:
                return process, match.group(1)
        elif process.poll() is not None:
            break
    process.kill()
    process.wait(timeout=5)
    raise AssertionError("server URL was not logged: " + "".join(lines))


def _strike_until(page, target_slot: int) -> None:
    target = page.locator('[data-strike-target="left"]').first
    if not target.evaluate("element => document.activeElement === element"):
        page.keyboard.press("Tab")
    assert target.evaluate("element => document.activeElement === element")
    current = int(page.locator('[role="progressbar"]').get_attribute("aria-valuenow"))
    for _ in range(current, target_slot):
        expected = (
            int(page.locator('[role="progressbar"]').get_attribute("aria-valuenow")) + 1
        )
        target.press("Enter")
        page.wait_for_function(
            """expected => Number(document.querySelector('[role=progressbar]')
            .getAttribute('aria-valuenow')) >= expected""",
            arg=expected,
        )


def _finish_with_keyboard(page) -> None:
    _strike_until(page, 15)
    page.locator("#stage-complete").wait_for(state="visible")


def test_product_browser_cold_open(page, tmp_path: Path) -> None:
    process, url = _start_server(tmp_path)
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.locator('[data-strike-target="left"]').wait_for(state="visible")
        assert (
            page.get_by_role(
                "button", name=re.compile(r"왼쪽 긋기.+README", re.S)
            ).count()
            == 1
        )
        assert (
            page.get_by_role(
                "button", name=re.compile(r"오른쪽 긋기.+README", re.S)
            ).count()
            == 1
        )
        assert page.get_by_role("progressbar", name="세션 슬롯 진행").count() == 1
        _finish_with_keyboard(page)
        assert page.locator("#stage-complete").evaluate(
            "element => document.activeElement === element"
        )
        assert page.locator("#stage-complete").get_attribute("role") == "status"
        assert "산출물이 착지했다" in page.locator("#landing").inner_text()
        assert page.locator("#next-proof").is_visible()
        assert "새 Claude Code 세션" in page.locator("#next-proof").inner_text()
        assert any(
            rule in page.locator("#next-rule").inner_text()
            for rule in page.locator("#rules .rule-text").all_inner_texts()
        )
        assert list(tmp_path.glob("manifest*.json"))
        status = page.locator("#strike-status")
        assert status.get_attribute("role") == "status"
        assert status.get_attribute("aria-live") == "polite"
        assert status.text_content()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_product_browser_resumes_after_process_restart(page, tmp_path: Path) -> None:
    base = tmp_path / "resume-data"
    first, first_url = _start_server(base)
    second: subprocess.Popen[str] | None = None
    try:
        page.goto(first_url, wait_until="domcontentloaded")
        page.locator('[data-strike-target="left"]').wait_for(state="visible")
        _strike_until(page, 4)
        first.terminate()
        first.wait(timeout=5)
        page.keyboard.press("Enter")
        page.locator("#connection-error").wait_for(state="visible")
        assert (
            "저장된 슬롯부터 이어진다" in page.locator("#connection-error").inner_text()
        )
        assert page.locator("body").get_attribute("data-connection") == "offline"

        second, second_url = _start_server(base)
        page.goto(second_url, wait_until="domcontentloaded")
        assert (
            page.get_by_role("progressbar", name="세션 슬롯 진행").get_attribute(
                "aria-valuenow"
            )
            == "4"
        )
        _finish_with_keyboard(page)
        assert "산출물이 착지했다" in page.locator("#landing").inner_text()
        assert second.wait(timeout=5) == 0
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=5)
        if second is not None:
            if second.poll() is None:
                second.kill()
            second.wait(timeout=5)
