"""Real app profile consumers, observed responses and refusal under optimization."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = r"""
import asyncio
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pydantic_settings import DotEnvSettingsSource
from daimon.testing import http_observation as http
from daimon.testing import product_profiles as profiles
from judge.catalog_runner import load_catalog, plan_scenario
from judge.headless_executor import execute_http_cell, CellResult


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def denied(*args, **kwargs):
    raise AssertionError("profile fixture attempted unbound allocation or socket I/O")

socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
DotEnvSettingsSource._read_env_file = denied

async def run():
    entries = load_catalog(Path("packages/testing/fixtures/http_sources").resolve())
    baseline = []
    selected = []
    original_environment = os.environ
    with TemporaryDirectory(prefix="qa-profile-controls-") as folder:
        for name in ("implicit-none", "explicit-none", "opt-in"):
            profile = profiles.OPT_IN_PRODUCT_PROFILE if name == "opt-in" else profiles.DEFAULT_PRODUCT_PROFILE
            with profiles.use_product_profile(profile):
                kwargs = {"profile": profile} if name == "opt-in" else ({"profile": None} if name == "explicit-none" else {})
                async with http.offline_http_apps(Path(folder, name), nonce="same", **kwargs) as observer:
                    require(os.environ is original_environment, "environment mapping leaked beyond construction")
                    flags = profiles.applied_product_flags()
                    require(flags == frozenset(profile.flags) if name == "opt-in" else not flags,
                            "actual factories did not apply exactly the selected switches")
                    observations = []
                    for entry in entries:
                        for backend in ("anthropic", "openai", "gemini"):
                            plan = plan_scenario(entry, Path("packages/testing/fixtures/http_sources").resolve(), backend, run_id="profile-control")
                            cell = await execute_http_cell(plan, observer)
                            restored = CellResult.model_validate_json(cell.model_dump_json(exclude_computed_fields=True))
                            expected = "FAIL"  # no profile switch remains; non-browser link errors stay JSON
                            require(restored.status == expected, f"response oracle ignored the real profile setting: {name} {entry.id} {backend} {restored.status} != {expected}")
                            require(restored.request_count == 2 and not restored.evidence.turns, "HTTP source lost scope or observations")
                            observations.append(tuple((o.status, o.headers, o.body, o.requested_url) for o in restored.admin_http.observations))
                    if name == "opt-in":
                        selected = observations
                    elif baseline:
                        require(baseline == observations, "explicit None changed response status, headers or bytes")
                    else:
                        baseline = observations
            require(profiles.current_product_profile() == profiles.DEFAULT_PRODUCT_PROFILE and not profiles.applied_product_flags(), "profile scope did not restore")
        require(selected == baseline, "a profile switch changed the HTTP hosts' responses")  # no host switch remains
        with patch.object(http, "build_async_anthropic", denied):
            try:
                async with http.offline_http_apps(Path(folder, "mismatch"), nonce="same", profile=profiles.OPT_IN_PRODUCT_PROFILE):
                    raise AssertionError("inactive profile reached apps")
            except ValueError as error:
                require("active product profile" in str(error), "wrong profile refusal")
            require(not Path(folder, "mismatch").exists(), "profile refusal created a store")
        with profiles.use_product_profile(profiles.OPT_IN_PRODUCT_PROFILE):
            with patch.dict(os.environ, {"ANTHROPIC_BASE_URL":"https://hostile.invalid"}), patch.object(http.httpx, "AsyncClient", denied):
                try:
                    async with http.offline_http_apps(Path(folder, "hostile"), nonce="same", profile=profiles.OPT_IN_PRODUCT_PROFILE):
                        raise AssertionError("profile bypassed ambient SDK refusal")
                except ValueError as error:
                    require("refuses ambient" in str(error), "hostile environment reached app construction")
                require(not Path(folder, "hostile").exists(), "ambient refusal created a store")
    print("PASS: None byte parity, applied settings, 18 real cells, host responses unchanged by profile, profile/ambient admission, cleanup")

asyncio.run(run())
"""


@pytest.mark.parametrize("optimized", (False, True))
def test_real_product_factories_execute_profiles(tmp_path: Path, optimized: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, *(["-O"] if optimized else []), "-c", PROBE],
        cwd=root,
        env={"PYTHONPATH": str(root / "tests")},
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    if "PASS: None byte parity" not in result.stdout:
        raise AssertionError("real product controls did not complete")
    (tmp_path / "controls.json").write_text(json.dumps({"stdout": result.stdout}))
