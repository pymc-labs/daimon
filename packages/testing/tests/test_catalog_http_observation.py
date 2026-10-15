"""Real administrative apps and the owner-granted runner hook, without turns."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]

# A clean, literal child environment is part of this disposable app fixture.
# The separate names-only hostile test proves admission before app settings.
PROBE = r"""
import asyncio
import json
import os
import socket
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from pydantic_settings import DotEnvSettingsSource

from judge.catalog_runner import load_catalog, plan_scenario
from judge.catalog_steps import http_only_source
from judge.headless_executor import CellResult, execute_http_cell
from daimon.testing.http_observation import (
    HTTP_SOURCE_PINS, AdminHTTPRun, HTTPObserver, http_outcome, offline_http_apps,
)

def denied(*args, **kwargs):
    raise AssertionError("administrative probe attempted socket/DNS I/O")

socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
DotEnvSettingsSource._read_env_file = denied

def require(condition, reason):
    if not condition:
        raise AssertionError(reason)

def refuses(raw):
    try:
        CellResult.model_validate_json(json.dumps(raw))
    except ValueError:
        return
    raise AssertionError("forged administrative result survived JSON roundtrip")

async def run():
    root = Path("packages/testing/fixtures/http_sources").resolve()
    entries = load_catalog(root)
    require(len(entries) == 2, "canonical administrative sources changed")
    cells = []
    controls = 0
    with TemporaryDirectory(prefix="qa-http-control-") as folder:
        Path(folder, ".env").write_text("ANTHROPIC_WEBHOOK_SECRET=synthetic-do-not-read\n")
        os.chdir(folder)
        async with offline_http_apps(Path(folder, "data"), nonce="qa-http-control") as observer:
            for entry in entries:
                require(entry.sha256 == HTTP_SOURCE_PINS[entry.id], "source bytes changed")
                for backend in ("anthropic", "openai", "gemini"):
                    plan = plan_scenario(entry, root, backend, run_id="qa-http-control")
                    require(http_only_source(plan), "canonical source not admitted")
                    cell = await execute_http_cell(plan, observer)
                    raw = cell.model_dump(mode="json", exclude_computed_fields=True)
                    restored = CellResult.model_validate_json(json.dumps(raw))
                    require(restored.status == "FAIL", "real app regression earned PASS")
                    require(not restored.evidence.turns, "HTTP capture invented turn evidence")
                    require(restored.request_count == 2, "HTTP source was not fully executed")
                    require(len(restored.catalog_outcome.checks) == 2, "HTTP checks were dropped")
                    require(restored.admin_http is not None, "administrative envelope missing")
                    require(all(o.body and o.headers for o in restored.admin_http.observations),
                            "actual status/headers/body not captured")
                    cells.append(restored.model_dump(mode="json", exclude_computed_fields=True))
                    for fault in ("empty_report", "claimed_pass", "foreign_evidence",
                                  "wrong_request_count", "no_envelope", "turn_mode"):
                        forged = deepcopy(raw)
                        if fault == "empty_report":
                            forged["catalog_outcome"]["checks"] = []
                        elif fault == "claimed_pass":
                            for check in forged["catalog_outcome"]["checks"]:
                                check["status"] = "PASS"
                                check["code"] = "HTTP_EXPECTATIONS_MET"
                        elif fault == "foreign_evidence":
                            forged["evidence"]["scenario_id"] = "foreign"
                        elif fault == "wrong_request_count":
                            forged["request_count"] = 0
                        elif fault == "no_envelope":
                            forged["admin_http"] = None
                        else:
                            forged["mode"] = "offline_scripted"
                        refuses(forged)
                        controls += 1

                    # Missing bindings and missing responses stay PENDING,
                    # including after a JSON roundtrip of the complete source.
                    pending = await execute_http_cell(plan, HTTPObserver({}, nonce="missing"))
                    restored_pending = CellResult.model_validate_json(
                        pending.model_dump_json(exclude_computed_fields=True))
                    require(restored_pending.status == "PENDING" and
                            restored_pending.request_count == 0 and
                            len(restored_pending.catalog_outcome.checks) == 2,
                            "unbound HTTP source earned PASS")
                    admin = restored.admin_http.model_copy(update={"observations": ()})
                    empty = AdminHTTPRun.model_validate_json(admin.model_dump_json())
                    require(http_outcome(empty).status == "PENDING", "empty capture earned PASS")
                    controls += 2

                    for fault in ("foreign_pin", "extra_assertion", "human_step", "host_turn"):
                        document = deepcopy(entry.scenario)
                        pinned = entry
                        if fault == "foreign_pin":
                            pinned = entry.model_copy(update={"sha256": "0" * 64})
                        elif fault == "extra_assertion":
                            document["assert"].append({"kind": "http_check", "url": "https://foreign.invalid"})
                        elif fault == "human_step":
                            document["human"] = [{"do": "manual"}]
                        else:
                            document["steps"] = [{"do": "mention", "text": "hello"}]
                        if fault != "foreign_pin":
                            pinned = entry.model_copy(update={"scenario": document})
                        changed = plan_scenario(pinned, root, backend, run_id="qa-http-control")
                        require(not http_only_source(changed), "expanded source escaped admin scope")
                        try:
                            await execute_http_cell(changed, observer)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("unbound source executed through HTTP hook")
                        controls += 1
    Path(sys.argv[1]).write_text(json.dumps({"cells": cells, "negative_controls": controls}))

asyncio.run(run())
"""


@pytest.mark.parametrize("optimized", (False, True))
def test_real_apps_and_runner_envelopes_survive_roundtrip(tmp_path: Path, optimized: bool) -> None:
    output = tmp_path / "cells.json"
    command = [sys.executable, *(["-O"] if optimized else []), "-c", PROBE, str(output)]
    result = subprocess.run(
        command,
        cwd=REPOSITORY,
        env={"PYTHONPATH": str(REPOSITORY / "tests")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    raw = json.loads(output.read_text())
    if len(raw["cells"]) != 6 or raw["negative_controls"] != 72:
        raise AssertionError("administrative coverage did not retain all six cells/controls")
