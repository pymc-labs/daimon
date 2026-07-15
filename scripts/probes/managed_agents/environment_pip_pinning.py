"""Probe: can MA environments preinstall pinned pip packages, with readable source?

Questions answered (spike 038):
  EXP1 — pin semantics: does `config.packages.pip: ["six==1.16.0"]` install exactly
         1.16.0 (latest is 1.17.x), with source readable in site-packages?
  EXP2 — invalid pin: where does `six==999.999.999` fail — env create, session
         create, or a session.error at runtime?
  EXP3 — heavy stack: does `pip: ["pymc-marketing"]` provision, what does the
         session-start + first-turn latency look like, is the source importable
         and readable?
  EXP4 — fallback in a bare env: how long do in-session `pip install pymc-marketing`
         and `git clone --depth 1` take via the bash tool?

Usage:
    set -a && source .env && set +a
    uv run python scripts/probes/managed_agents/environment_pip_pinning.py

Requires ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"
TAG = uuid.uuid4().hex[:8]
RESULTS_PATH = Path(__file__).parent / "environment_pip_pinning_results.json"

TURN_TIMEOUT_S = 1200
POLL_INTERVAL_S = 3


def api_headers(api_key: str, json_ct: bool = True) -> dict[str, str]:
    h = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


@dataclass
class ExperimentResult:
    name: str
    env_id: str | None = None
    env_create_s: float | None = None
    env_create_error: str | None = None
    session_id: str | None = None
    session_create_s: float | None = None
    session_create_error: str | None = None
    turn_total_s: float | None = None
    session_errors: list[dict] = field(default_factory=list)
    tool_output: str | None = None
    agent_text: str | None = None
    error: str | None = None


async def create_env(
    http: httpx.AsyncClient, api_key: str, name: str, pip: list[str]
) -> tuple[str | None, float, str | None]:
    body = {
        "name": name,
        "config": {
            "type": "cloud",
            "networking": {"type": "unrestricted"},
            "packages": {
                "type": "packages",
                "apt": [],
                "cargo": [],
                "gem": [],
                "go": [],
                "npm": [],
                "pip": pip,
            },
        },
    }
    t0 = time.monotonic()
    r = await http.post(f"{API_BASE}/environments", headers=api_headers(api_key), json=body)
    dt = time.monotonic() - t0
    if r.status_code >= 400:
        return None, dt, f"HTTP {r.status_code}: {r.text[:500]}"
    return r.json()["id"], dt, None


async def create_agent(http: httpx.AsyncClient, api_key: str) -> tuple[str, int]:
    body = {
        "name": f"probe-envpip-{TAG}",
        "model": "claude-haiku-4-5",
        "system": (
            "You are a probe runner. When the user gives you a bash command, run it "
            "EXACTLY as given with the bash tool, then reply with ONLY the raw output. "
            "Never substitute commands, never summarize, never add commentary."
        ),
        "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
    }
    r = await http.post(f"{API_BASE}/agents", headers=api_headers(api_key), json=body)
    r.raise_for_status()
    j = r.json()
    return j["id"], j["version"]


async def run_command_session(
    http: httpx.AsyncClient,
    api_key: str,
    result: ExperimentResult,
    agent_id: str,
    agent_version: int,
    env_id: str,
    command: str,
) -> None:
    """Create a session in env_id, send one bash command, collect output + timings."""
    t0 = time.monotonic()
    r = await http.post(
        f"{API_BASE}/sessions",
        headers=api_headers(api_key),
        json={
            "agent": {"type": "agent", "id": agent_id, "version": agent_version},
            "environment_id": env_id,
            "metadata": {"daimon_probe": f"spike-038-envpip-{TAG}"},
        },
    )
    result.session_create_s = round(time.monotonic() - t0, 2)
    if r.status_code >= 400:
        result.session_create_error = f"HTTP {r.status_code}: {r.text[:500]}"
        return
    session_id = r.json()["id"]
    result.session_id = session_id

    t_msg = time.monotonic()
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=api_headers(api_key),
        json={
            "events": [
                {
                    "type": "user.message",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Run this exact bash command and paste its full raw output:\n{command}",
                        }
                    ],
                }
            ]
        },
    )
    r.raise_for_status()

    deadline = time.monotonic() + TURN_TIMEOUT_S
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        events: list[dict] = []
        page: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 1000, "order": "asc"}
            if page is not None:
                params["page"] = page
            er = await http.get(
                f"{API_BASE}/sessions/{session_id}/events",
                headers=api_headers(api_key, json_ct=False),
                params=params,
            )
            er.raise_for_status()
            ebody = er.json()
            events.extend(ebody.get("data", []))
            page = ebody.get("next_page_token") or ebody.get("next_page")
            if not page:
                break

        result.session_errors = [e for e in events if e.get("type") == "session.error"]
        user_idx = max(
            (i for i, e in enumerate(events) if e.get("type") == "user.message"), default=-1
        )
        tail = events[user_idx + 1 :]
        if any(e.get("type") == "session.status_idle" for e in tail):
            result.turn_total_s = round(time.monotonic() - t_msg, 2)
            tool_outputs = [
                "".join(b.get("text", "") for b in e.get("content", []) if b.get("type") == "text")
                for e in tail
                if e.get("type") == "agent.tool_result"
            ]
            result.tool_output = "\n---\n".join(tool_outputs)[:4000]
            texts = [
                "".join(b.get("text", "") for b in e.get("content", []) if b.get("type") == "text")
                for e in tail
                if e.get("type") == "agent.message"
            ]
            result.agent_text = "\n".join(t for t in texts if t.strip())[:4000]
            return
        if time.monotonic() > deadline:
            result.error = f"turn timeout after {TURN_TIMEOUT_S}s"
            return


async def cleanup(
    api_key: str, agent_id: str | None, env_ids: list[str], session_ids: list[str]
) -> None:
    print("\n== Cleanup ==")
    async with httpx.AsyncClient(timeout=60) as http:
        for sid in session_ids:
            try:
                r = await http.post(
                    f"{API_BASE}/sessions/{sid}/archive",
                    headers=api_headers(api_key, json_ct=False),
                )
                print(f"  archive session {sid} -> {r.status_code}")
            except Exception as err:  # noqa: BLE001 — best-effort cleanup
                print(f"  archive session {sid} failed: {err}")
        for eid in env_ids:
            try:
                r = await http.delete(
                    f"{API_BASE}/environments/{eid}",
                    headers=api_headers(api_key, json_ct=False),
                )
                print(f"  delete env {eid} -> {r.status_code}")
            except Exception as err:  # noqa: BLE001 — best-effort cleanup
                print(f"  delete env {eid} failed: {err}")
        if agent_id:
            try:
                r = await http.post(
                    f"{API_BASE}/agents/{agent_id}/archive",
                    headers=api_headers(api_key, json_ct=False),
                )
                print(f"  archive agent {agent_id} -> {r.status_code}")
            except Exception as err:  # noqa: BLE001 — best-effort cleanup
                print(f"  archive agent failed: {err}")


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DAIMON_ANTHROPIC__API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY / DAIMON_ANTHROPIC__API_KEY not set")

    results: list[ExperimentResult] = []
    agent_id: str | None = None
    env_ids: list[str] = []
    session_ids: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as http:
            agent_id, agent_version = await create_agent(http, api_key)
            print(f"agent: {agent_id} v{agent_version}")

            # EXP1 — pinned light package: pin honored? source readable?
            print("\n== EXP1: pinned light package (six==1.16.0; latest is 1.17.x) ==")
            r1 = ExperimentResult(name="exp1_pinned_light")
            r1.env_id, r1.env_create_s, r1.env_create_error = await create_env(
                http, api_key, f"probe-envpip-light-{TAG}", ["six==1.16.0"]
            )
            print(f"  env create: {r1.env_create_s}s error={r1.env_create_error}")
            if r1.env_id:
                env_ids.append(r1.env_id)
                await run_command_session(
                    http,
                    api_key,
                    r1,
                    agent_id,
                    agent_version,
                    r1.env_id,
                    "python3 -c \"import six; print('SIX_VERSION', six.__version__); "
                    "print('SIX_FILE', six.__file__)\" "
                    "&& head -3 \"$(python3 -c 'import six; print(six.__file__)')\"",
                )
                if r1.session_id:
                    session_ids.append(r1.session_id)
                print(f"  session create: {r1.session_create_s}s  turn: {r1.turn_total_s}s")
                print(f"  output: {(r1.tool_output or r1.agent_text or '')[:400]}")
            results.append(r1)

            # EXP2 — invalid pin: where does the failure surface?
            print("\n== EXP2: invalid pin (six==999.999.999) ==")
            r2 = ExperimentResult(name="exp2_invalid_pin")
            r2.env_id, r2.env_create_s, r2.env_create_error = await create_env(
                http, api_key, f"probe-envpip-bad-{TAG}", ["six==999.999.999"]
            )
            print(f"  env create: {r2.env_create_s}s error={r2.env_create_error}")
            if r2.env_id:
                env_ids.append(r2.env_id)
                await run_command_session(
                    http,
                    api_key,
                    r2,
                    agent_id,
                    agent_version,
                    r2.env_id,
                    "python3 -c \"import six; print('SIX_VERSION', six.__version__)\" "
                    "|| echo SIX_NOT_INSTALLED",
                )
                if r2.session_id:
                    session_ids.append(r2.session_id)
                print(f"  session create: {r2.session_create_s}s error={r2.session_create_error}")
                print(f"  turn: {r2.turn_total_s}s  session_errors: {len(r2.session_errors)}")
                print(f"  output: {(r2.tool_output or r2.agent_text or '')[:400]}")
            results.append(r2)

            # EXP3 — heavy stack: pymc-marketing preinstalled, latency + source
            print("\n== EXP3: heavy stack (pymc-marketing, unpinned) ==")
            r3 = ExperimentResult(name="exp3_heavy_stack")
            r3.env_id, r3.env_create_s, r3.env_create_error = await create_env(
                http, api_key, f"probe-envpip-heavy-{TAG}", ["pymc-marketing"]
            )
            print(f"  env create: {r3.env_create_s}s error={r3.env_create_error}")
            if r3.env_id:
                env_ids.append(r3.env_id)
                await run_command_session(
                    http,
                    api_key,
                    r3,
                    agent_id,
                    agent_version,
                    r3.env_id,
                    "python3 -c \"import pymc_marketing, pymc, arviz; "
                    "print('PMM_VERSION', pymc_marketing.__version__); "
                    "print('PYMC_VERSION', pymc.__version__); "
                    "print('PMM_FILE', pymc_marketing.__file__)\" "
                    "&& head -5 \"$(python3 -c 'import pymc_marketing.mmm.components.saturation as s; "
                    "print(s.__file__)')\"",
                )
                if r3.session_id:
                    session_ids.append(r3.session_id)
                print(f"  session create: {r3.session_create_s}s  turn: {r3.turn_total_s}s")
                print(f"  output: {(r3.tool_output or r3.agent_text or '')[:400]}")
            results.append(r3)

            # EXP4 — fallback: bare env, in-session pip install + shallow clone timing
            print("\n== EXP4: bare env fallback (in-session pip install + git clone) ==")
            r4 = ExperimentResult(name="exp4_bare_fallback")
            r4.env_id, r4.env_create_s, r4.env_create_error = await create_env(
                http, api_key, f"probe-envpip-bare-{TAG}", []
            )
            print(f"  env create: {r4.env_create_s}s error={r4.env_create_error}")
            if r4.env_id:
                env_ids.append(r4.env_id)
                await run_command_session(
                    http,
                    api_key,
                    r4,
                    agent_id,
                    agent_version,
                    r4.env_id,
                    "( time pip install -q example-package ) 2>&1 | tail -4 "
                    "&& python3 -c \"import example_package; print('PKG_VERSION', example_package.__version__)\" "
                    "&& ( time git clone -q --depth 1 https://github.com/example-org/example-repo /tmp/pm ) 2>&1 | tail -4 "
                    "&& ls /tmp/pm/tests | head -3",
                )
                if r4.session_id:
                    session_ids.append(r4.session_id)
                print(f"  session create: {r4.session_create_s}s  turn: {r4.turn_total_s}s")
                print(f"  output: {(r4.tool_output or r4.agent_text or '')[:600]}")
            results.append(r4)

    finally:
        RESULTS_PATH.write_text(
            json.dumps([r.__dict__ for r in results], indent=2, default=str)
        )
        print(f"\nforensics -> {RESULTS_PATH}")
        await cleanup(api_key, agent_id, env_ids, session_ids)

    print("\n== Verdict inputs ==")
    for r in results:
        print(
            f"  {r.name}: env_err={bool(r.env_create_error)} "
            f"sess_err={bool(r.session_create_error)} turn={r.turn_total_s}s "
            f"errors_in_session={len(r.session_errors)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
