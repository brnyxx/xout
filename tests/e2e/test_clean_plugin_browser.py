"""Install the plugin in an isolated Claude home, then exercise its cache."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

from .test_browser import _finish_with_keyboard, _start_server, _strike_until


def test_clean_plugin_browser(page, tmp_path: Path) -> None:
    claude = shutil.which("claude")
    if claude is None:
        if os.environ.get("RUN_CLEAN_PLUGIN_E2E") == "1":
            pytest.fail("claude CLI is required when RUN_CLEAN_PLUGIN_E2E=1")
        pytest.skip("claude CLI is not installed")

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    env.pop("CLAUDE_CONFIG_DIR", None)
    repo = Path(
        os.environ.get("XOUT_PLUGIN_ROOT", Path(__file__).resolve().parents[2])
    ).resolve()
    marketplace = "popper-marketplace"
    plugin_id = "popper@popper-marketplace"
    subprocess.run(
        [claude, "plugin", "marketplace", "add", str(repo), "--scope", "user"],
        env=env,
        check=True,
        cwd=repo,
    )
    subprocess.run(
        [claude, "plugin", "install", plugin_id, "--scope", "user", "--yes"],
        env=env,
        check=True,
        cwd=repo,
    )
    listed = subprocess.run(
        [claude, "plugin", "list", "--json"],
        env=env,
        check=True,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    installed = json.loads(listed.stdout)
    assert plugin_id in json.dumps(installed)
    plugin_version = json.loads(
        (repo / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]
    cache = home / ".claude" / "plugins" / "cache"
    candidates = [
        p
        for p in cache.rglob("*")
        if p.is_dir()
        and p.name == plugin_version
        and marketplace in p.parts
        and "popper" in p.parts
    ]
    assert candidates, f"plugin cache version not found under {cache}"

    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text('{"name":"fixture"}\n', encoding="utf-8")
    (project / "index.ts").write_text("export const ready = true;\n", encoding="utf-8")
    launcher = candidates[0] / "scripts" / "popper_plugin.py"
    assert launcher.is_file()
    runtime_venv = tmp_path / "runtime-venv"
    venv.EnvBuilder(with_pip=False).create(runtime_venv)
    runtime_python = runtime_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    runtime_env = dict(env)
    runtime_env.pop("PYTHONPATH", None)
    runtime_env.pop("PYTHONHOME", None)
    probe = subprocess.run(
        [
            str(runtime_python),
            "-I",
            "-c",
            "import importlib.util; raise SystemExit(importlib.util.find_spec('popper') is not None)",
        ],
        cwd=tmp_path,
        env=runtime_env,
        check=False,
    )
    assert probe.returncode == 0
    landed = tmp_path / "landed"
    doctor = subprocess.run(
        [
            str(runtime_python),
            str(launcher),
            "doctor",
            "--base-dir",
            str(landed),
            "--json",
        ],
        cwd=project,
        env=runtime_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(doctor.stdout)["healthy"] is True

    first, url = _start_server(
        landed,
        cwd=project,
        env=runtime_env,
        command=[str(runtime_python), str(launcher)],
        extra_args=["--repo", str(project)],
    )
    second: subprocess.Popen[str] | None = None
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.locator('[data-strike-target="left"]').wait_for(state="visible")
        assert "Node.js" in page.locator("#left-text").inner_text()
        _strike_until(page, 4)
        first.terminate()
        first.wait(timeout=5)

        second, resumed_url = _start_server(
            landed,
            cwd=project,
            env=runtime_env,
            command=[str(runtime_python), str(launcher)],
            extra_args=["--repo", str(project)],
            operation="resume",
        )
        page.goto(resumed_url, wait_until="domcontentloaded")
        assert (
            page.get_by_role("progressbar", name="세션 슬롯 진행").get_attribute(
                "aria-valuenow"
            )
            == "4"
        )
        assert "Node.js" in page.locator("#left-text").inner_text()
        _finish_with_keyboard(page)
        assert page.locator("#stage-complete").evaluate(
            "element => document.activeElement === element"
        )
        assert "산출물이 착지했다" in page.locator("#landing").inner_text()
        assert page.locator("#next-proof").is_visible()
        assert "/popper:popper enable" in page.locator("#next-proof").inner_text()
        assert any(
            rule in page.locator("#next-rule").inner_text()
            for rule in page.locator("#rules .rule-text").all_inner_texts()
        )
        assert second.wait(timeout=5) == 0

        claude_md = home / ".claude" / "CLAUDE.md"
        without_grant = subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "enable",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=False,
        )
        assert without_grant.returncode == 1
        assert not claude_md.exists()

        subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "enable",
                "--grant",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=True,
        )
        enabled = subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "status",
                "--base-dir",
                str(landed),
                "--json",
            ],
            cwd=project,
            env=runtime_env,
            check=True,
            capture_output=True,
            text=True,
        )
        enabled_status = json.loads(enabled.stdout)
        assert enabled_status["activation"]["status"] == "active"
        expected_import = enabled_status["activation"]["expected_import"]
        expected_bytes = (expected_import + "\n").encode("utf-8")
        assert claude_md.read_bytes() == expected_bytes
        assert (landed / "activation.json").is_file()
        subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "enable",
                "--grant",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=True,
        )
        assert claude_md.read_bytes() == expected_bytes

        subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "rollback",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=True,
        )
        rolled_back = subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "status",
                "--base-dir",
                str(landed),
                "--json",
            ],
            cwd=project,
            env=runtime_env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(rolled_back.stdout)["activation"]["status"] == "inactive"
        assert claude_md.read_bytes() == b""
        assert not (landed / "activation.json").exists()

        claude_md.write_bytes(expected_bytes)
        subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "enable",
                "--grant",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=True,
        )
        subprocess.run(
            [
                str(runtime_python),
                str(launcher),
                "rollback",
                "--base-dir",
                str(landed),
            ],
            cwd=project,
            env=runtime_env,
            check=True,
        )
        assert claude_md.read_bytes() == expected_bytes
        assert not (landed / "activation.json").exists()
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=5)
        if second is not None:
            if second.poll() is None:
                second.kill()
            second.wait(timeout=5)
