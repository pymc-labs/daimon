"""Actual child-pytest fixture graph probe; no foreign connection is opened."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def run_fixture_preflight_probe(
    *, tmp_path: Path, target: str, local_contributor: bool
) -> tuple[int, str, bool]:
    marker = tmp_path / "connection-attempt"
    plugin = tmp_path / "n7_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy.ext.asyncio import AsyncEngine\n"
        "def pytest_configure(config):\n"
        "    def trap(*args, **kwargs):\n"
        f"        Path({str(marker)!r}).write_text('attempted')\n"
        "        raise RuntimeError('connection trap reached before fixture preflight')\n"
        "    AsyncEngine.connect = trap\n"
        "    AsyncEngine.begin = trap\n"
    )
    child_env = dict(os.environ)
    child_env.pop("GITHUB_ACTIONS", None)
    child_env.pop("CI", None)
    child_env["DAIMON_DATABASE__TEST_URL"] = (
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test"
        if local_contributor
        else "postgresql+asyncpg://unused:unused@127.0.0.1:1/foreign_test"
    )
    child_env["PYTHONPATH"] = str(tmp_path) + os.pathsep + child_env.get("PYTHONPATH", "")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-n0",
        "-p",
        "n7_connection_trap",
        target,
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 120)
    except BaseException:
        if process.returncode is None:
            process.terminate()
        await process.communicate()
        raise
    return process.returncode or 0, output.decode(), marker.exists()
