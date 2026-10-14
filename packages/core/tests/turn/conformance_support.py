"""Provider-independent facts and database preflight controls for host conformance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from daimon.core.scope import ResolvedConfig
from daimon.core.turn.admission import Admission
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef

TENANT = uuid.UUID(int=4404)

ACCOUNT = uuid.UUID(int=4405)

PROFILE = "openai.persistent_workspace"

REVISION = ConfigRevision.create(
    ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
    1,
    resolve_default(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna")),
)


def admission() -> Admission:
    return Admission(
        account_id=ACCOUNT,
        agent=ma_agent(id="host-metadata-not-a-provider-agent"),
        environment=ma_environment(),
        config=ResolvedConfig(agent_name="test", environment_name="test"),
        backend_revision=REVISION,
        backend_revision_origin="channel",
    )


async def record_usage(event: object) -> None:
    raise AssertionError("dispatch must not bill")


def run_host_fixture_preflight_probe(tmp_path: Path, *, target: str, case: str) -> None:
    """Run the actual host fixture graph with allocation/connection traps."""
    plugin = tmp_path / "n4_preflight_probe.py"
    receipt = tmp_path / "receipt.json"
    plugin.write_text(
        """
import json
import os
from pathlib import Path
import pytest
import daimon.testing.db as shared
from sqlalchemy import event
from sqlalchemy.engine import Engine

counts = {"builds": 0, "connections": 0, "host_setup": 0}
patch = pytest.MonkeyPatch()

def pytest_sessionstart(session):
    original = shared.build_test_engine
    def allocate(*args, **kwargs):
        counts["builds"] += 1
        return original(*args, **kwargs)
    def forbidden_connection(*args, **kwargs):
        counts["connections"] += 1
        raise RuntimeError("N4_ACCEPTED_CONFIGURATION_CONNECTION_TRAP")
    patch.setattr(shared, "build_test_engine", allocate)
    event.listen(Engine, "do_connect", forbidden_connection)
    if os.environ["N4_PREFLIGHT_CASE"] == "ci_foreign_schema":
        patch.setattr(shared, "_new_schema_name",
                      lambda kind: f"test_w{os.getpid() + 1}_1234abcd")

def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == "host":
        counts["host_setup"] += 1

def pytest_sessionfinish(session, exitstatus):
    counts["exitstatus"] = int(exitstatus)
    Path(os.environ["N4_PREFLIGHT_RECEIPT"]).write_text(json.dumps(counts))
"""
    )
    urls = {
        "foreign": "postgresql+asyncpg://fixture:fixture@localhost/operator_test",
        "remote": "postgresql+asyncpg://fixture:fixture@remote.invalid/daimon_test_nc_N4",
        "query": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_N4?host=remote.invalid",
        "contributor": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
        "lane": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_N4_probe",
        "ci": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
        "ci_foreign_schema": "postgresql+asyncpg://fixture:fixture@localhost/daimon_test",
    }
    # Do not read/copy operator credential values into the child process.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
    env.update(
        DAIMON_DATABASE__TEST_URL=urls[case],
        PYTHONPATH=str(tmp_path),
        N4_PREFLIGHT_CASE=case,
        N4_PREFLIGHT_RECEIPT=str(receipt),
    )
    if case.startswith("ci"):
        env.update(CI="true", GITHUB_ACTIONS="true")
    child = subprocess.run(
        [
            sys.executable,
            *(["-O"] if sys.flags.optimize else []),
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-n",
            "0",
            "-p",
            "n4_preflight_probe",
            target,
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    observed = json.loads(receipt.read_text())
    accepted = case in {"lane", "ci"}
    expected = {"builds": int(accepted), "connections": int(accepted), "host_setup": 0}
    if {name: observed[name] for name in expected} != expected:
        raise RuntimeError(f"host fixture reached work before admission: {observed}")
    if case == "contributor":
        if child.returncode != 0 or "skipped" not in child.stdout:
            raise RuntimeError("plain contributor fixture did not skip before allocation")
    elif child.returncode != 1:
        raise RuntimeError(f"fixture refusal/trap was not executed: {child.stdout}{child.stderr}")
    elif accepted and "N4_ACCEPTED_CONFIGURATION_CONNECTION_TRAP" not in child.stdout:
        raise RuntimeError("accepted lane/registered CI did not reach the deliberate trap")
    elif not accepted and (
        "Test hooks require owned local lane engines or registered isolated CI schemas"
        not in child.stdout
    ):
        raise RuntimeError(
            f"shared preflight refusal was not executed: {child.stdout}{child.stderr}"
        )
