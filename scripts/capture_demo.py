#!/usr/bin/env python3
"""Capture a real fixed-session Popper flow as a compact GIF.

Requires the `e2e` extra, installed Playwright Chromium, and ffmpeg. This is a
release-maintainer asset tool; it is never imported by the Popper runtime.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from xout.store import EventStore
from xout.web.server import build_server
from xout.web.state import ColdOpenSession


def capture(output: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to build the demo GIF")
    with tempfile.TemporaryDirectory(prefix="popper-demo-") as temporary:
        root = Path(temporary)
        data = root / "data"
        frames = root / "frames"
        frames.mkdir()
        session = ColdOpenSession(
            session_id="popper-public-demo",
            store=EventStore(data),
            land_dir=data,
        )
        server = build_server(session=session, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sequence = 0

        def frame(page, repeats: int = 1) -> None:
            nonlocal sequence
            source = frames / f"frame-{sequence:03d}.png"
            page.screenshot(path=str(source), full_page=False)
            sequence += 1
            for _ in range(repeats - 1):
                target = frames / f"frame-{sequence:03d}.png"
                shutil.copyfile(source, target)
                sequence += 1

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1200, "height": 760})
                page.goto(server.url, wait_until="domcontentloaded")
                left = page.locator('[data-strike-target="left"]').first
                left.wait_for(state="visible")
                frame(page, repeats=4)
                for slot in range(1, 16):
                    if slot in {1, 5, 9, 13}:
                        left.evaluate("element => element.classList.add('striking')")
                        frame(page)
                        left.evaluate("element => element.classList.remove('striking')")
                    left.click()
                    if slot < 15:
                        page.locator('[role="progressbar"]').wait_for(state="visible")
                        page.wait_for_function(
                            "expected => Number(document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')) === expected",
                            arg=slot,
                        )
                    else:
                        page.locator("#stage-complete").wait_for(state="visible")
                    frame(page, repeats=2 if slot in {5, 10} else 1)
                frame(page, repeats=6)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                "3",
                "-i",
                str(frames / "frame-%03d.png"),
                "-vf",
                "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3",
                "-loop",
                "0",
                str(output),
            ],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path(".github/assets/demo.gif"),
    )
    args = parser.parse_args()
    capture(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
