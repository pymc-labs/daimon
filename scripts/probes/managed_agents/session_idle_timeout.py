"""Probe: does Managed Agents reap idle sessions, and if so after how long?

Approach chosen: end_turn + subsequent user.message
-----------------------------------------------------
Driving a session to `requires_action` (tool confirmation) requires an agent
with a custom tool marked `confirmation_required=true`. The API surface for
that is not yet stable/documented. Instead we use the simpler and equally
valid approach:

  1. Send a user.message; stream until `end_turn` (session goes idle).
  2. Record wall time. Close the stream.
  3. At each checkpoint (30s, 2m, 5m, 10m), probe three things:
       a. GET /sessions/{id}          — is the session still accessible?
       b. GET /sessions/{id}/events   — does history still return 200?
       c. POST a new user.message + open SSE stream — does the agent respond?
  4. Print per-checkpoint results and a final summary line.

This tests "return control to adapter, resume later" which is exactly the
Discord approval-gap scenario — between the adapter returning and the user
clicking approve the MA session is idle with no SSE stream open.

Usage:
    # Key can come from .env or ANTHROPIC_API_KEY in environment.
    uv run python scripts/probes/managed_agents/session_idle_timeout.py

    # Or point at the daimon-cma .env:
    env ANTHROPIC_API_KEY=sk-... uv run python ...

Requires ANTHROPIC_API_KEY with Managed Agents access.
Creates one minimal agent + env + session and cleans them up at the end.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv
from httpx_sse import aconnect_sse

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"

# Checkpoint schedule in seconds.
CHECKPOINTS = [30, 120, 300, 600]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def headers(api_key: str, *, json_ct: bool = True) -> dict[str, str]:
    h: dict[str, str] = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


def sse_headers(api_key: str) -> dict[str, str]:
    return {k: v for k, v in headers(api_key).items() if k != "content-type"}


@dataclass
class CheckpointResult:
    label: str
    elapsed_s: float
    session_get_status: int | None = None
    events_get_status: int | None = None
    post_stream_ok: bool = False
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class ProbeReport:
    checkpoints: list[CheckpointResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


async def create_agent_and_env(http: httpx.AsyncClient, api_key: str) -> tuple[str, int, str]:
    suffix = uuid.uuid4().hex[:8]
    r = await http.post(
        f"{API_BASE}/agents",
        headers=headers(api_key),
        json={
            "name": f"probe-idle-{suffix}",
            "model": {"id": "claude-haiku-4-5", "speed": "standard"},
            "system": "You are a probe agent. Reply concisely in 1-2 sentences.",
            "skills": [],
            "tools": [],
            "mcp_servers": [],
        },
    )
    r.raise_for_status()
    a = r.json()
    agent_id: str = a["id"]
    agent_version: int = a["version"]

    r = await http.post(
        f"{API_BASE}/environments",
        headers=headers(api_key),
        json={
            "name": f"probe-idle-env-{suffix}",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    r.raise_for_status()
    env_id: str = r.json()["id"]
    return agent_id, agent_version, env_id


async def create_session(
    http: httpx.AsyncClient, api_key: str, agent_id: str, agent_version: int, env_id: str
) -> str:
    r = await http.post(
        f"{API_BASE}/sessions",
        headers=headers(api_key),
        json={
            "agent": {"type": "agent", "id": agent_id, "version": agent_version},
            "environment_id": env_id,
            "resources": [],
            "vault_ids": [],
        },
    )
    r.raise_for_status()
    session_id: str = r.json()["id"]
    return session_id


async def send_message(http: httpx.AsyncClient, api_key: str, session_id: str, text: str) -> None:
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=headers(api_key),
        json={"events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]},
    )
    r.raise_for_status()


async def delete_session(http: httpx.AsyncClient, api_key: str, session_id: str) -> int:
    r = await http.delete(
        f"{API_BASE}/sessions/{session_id}",
        headers=headers(api_key, json_ct=False),
    )
    return r.status_code


async def cleanup(http: httpx.AsyncClient, api_key: str, agent_id: str, env_id: str) -> None:
    print("\n== Cleanup ==")
    try:
        r = await http.post(
            f"{API_BASE}/agents/{agent_id}/archive",
            headers=headers(api_key, json_ct=False),
        )
        print(f"  archive agent {agent_id} -> {r.status_code}")
    except Exception as e:
        print(f"  archive agent failed: {e}")
    try:
        r = await http.delete(
            f"{API_BASE}/environments/{env_id}",
            headers=headers(api_key, json_ct=False),
        )
        print(f"  delete env {env_id} -> {r.status_code}")
    except Exception as e:
        print(f"  delete env failed: {e}")


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------


async def stream_until_idle(
    http: httpx.AsyncClient, api_key: str, session_id: str, prompt: str
) -> str | None:
    """Open SSE stream, send prompt, drain until session.status_idle. Returns last stop_reason."""
    stop_reason: str | None = None
    async with aconnect_sse(
        http,
        "GET",
        f"{API_BASE}/sessions/{session_id}/events/stream",
        headers=sse_headers(api_key),
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    ) as es:
        await send_message(http, api_key, session_id, prompt)
        async for sse_evt in es.aiter_sse():
            try:
                raw = json.loads(sse_evt.data)
            except Exception:
                continue
            evt_type = raw.get("type", "")
            if evt_type == "agent.turn_end":
                stop_reason = raw.get("stop_reason") or raw.get("content", {})
                # stop_reason may be nested inside content; try both shapes
                if isinstance(stop_reason, dict):
                    stop_reason = stop_reason.get("stop_reason")
            if evt_type == "session.status_idle":
                break
    return stop_reason


async def probe_stream_resume(
    http: httpx.AsyncClient, api_key: str, session_id: str
) -> tuple[bool, str | None]:
    """Post a new user.message and open an SSE stream. Returns (success, stop_reason)."""
    try:
        stop_reason = await stream_until_idle(
            http,
            api_key,
            session_id,
            "Say 'still alive' and nothing else.",
        )
        return True, stop_reason
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Per-checkpoint probe
# ---------------------------------------------------------------------------


async def run_checkpoint(
    http: httpx.AsyncClient,
    api_key: str,
    session_id: str,
    label: str,
    elapsed_s: float,
) -> CheckpointResult:
    result = CheckpointResult(label=label, elapsed_s=elapsed_s)

    # a. GET /sessions/{id}
    try:
        r = await http.get(
            f"{API_BASE}/sessions/{session_id}",
            headers=headers(api_key, json_ct=False),
        )
        result.session_get_status = r.status_code
    except Exception as e:
        result.session_get_status = None
        result.error = f"session GET failed: {e}"
        return result

    # b. GET /sessions/{id}/events
    try:
        r = await http.get(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=headers(api_key, json_ct=False),
            params={"limit": 10, "order": "desc"},
        )
        result.events_get_status = r.status_code
    except Exception as e:
        result.events_get_status = None
        result.error = f"events GET failed: {e}"

    # c. Post new message + stream
    ok, sr = await probe_stream_resume(http, api_key, session_id)
    result.post_stream_ok = ok
    result.stop_reason = sr

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Load key: try project .env first, then fallback to sibling daimon-cma .env.
    project_root = Path(__file__).resolve().parents[3]
    load_dotenv(project_root / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sibling_env = project_root.parent / "daimon-cma" / ".env"
        if sibling_env.exists():
            load_dotenv(sibling_env)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set. Set it in .env or export it before running.")

    report = ProbeReport()

    async with httpx.AsyncClient(timeout=90) as http:
        # --- Setup ---
        print("== Setup: creating agent + env + session ==")
        agent_id, agent_version, env_id = await create_agent_and_env(http, api_key)
        session_id = await create_session(http, api_key, agent_id, agent_version, env_id)
        print(f"  agent={agent_id} v={agent_version}")
        print(f"  env={env_id}")
        print(f"  session={session_id}")

        try:
            # --- Initial turn: drive session to end_turn (idle) ---
            print("\n== Initial turn: driving session to end_turn ==")
            t_idle_start = time.monotonic()
            t_wall_start = time.time()
            stop_reason = await stream_until_idle(
                http, api_key, session_id, "Reply with exactly: ready."
            )
            print(f"  Session reached idle. stop_reason={stop_reason!r}")
            print(f"  Wall time of idle: {time.strftime('%H:%M:%S', time.localtime(t_wall_start))}")
            print(f"  Stream closed. Starting checkpoint schedule: {CHECKPOINTS}s intervals.")

            # --- Checkpoint loop ---
            prev_elapsed = 0.0
            for i, target_s in enumerate(CHECKPOINTS):
                sleep_needed = target_s - (time.monotonic() - t_idle_start)
                if sleep_needed > 0:
                    label = f"{target_s}s"
                    print(f"\n  Sleeping {sleep_needed:.1f}s until {label} checkpoint...")
                    await asyncio.sleep(sleep_needed)

                elapsed = time.monotonic() - t_idle_start
                label = f"{target_s}s"
                print(f"\n== Checkpoint {label} (actual elapsed={elapsed:.1f}s) ==")

                result = await run_checkpoint(http, api_key, session_id, label, elapsed)
                report.checkpoints.append(result)

                alive_char = "y" if result.session_get_status == 200 else "n"
                history_char = "y" if result.events_get_status == 200 else "n"
                stream_char = "y" if result.post_stream_ok else "n"
                print(
                    f"  session_GET={result.session_get_status} ({alive_char})"
                    f"  events_GET={result.events_get_status} ({history_char})"
                    f"  stream_resume={stream_char}"
                    f"  stop_reason={result.stop_reason!r}"
                )
                if result.error:
                    print(f"  ERROR: {result.error}")

                # If session is dead, no point probing further checkpoints.
                if result.session_get_status not in (200, None) or not result.post_stream_ok:
                    print(f"  ** Session appears dead at {label} — stopping checkpoint loop **")
                    break

            # --- Probe: POST to a GARBAGE session id via SDK ---
            print("\n== Probe: events.send to garbage session id (SDK) ==")
            sdk_client = anthropic.AsyncAnthropic(api_key=api_key)
            garbage_session_id = "sess_does_not_exist_0000"
            try:
                await sdk_client.beta.sessions.events.send(
                    garbage_session_id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": "probe"}],
                        }
                    ],
                )
                print("  GARBAGE_SEND: no error raised (unexpected)")
            except anthropic.NotFoundError as e:
                print(
                    f"  GARBAGE_SEND: status_code={e.status_code}"
                    f" type={getattr(e, 'type', None)!r}"
                    f" body={e.body!r}"
                )
            except anthropic.APIStatusError as e:
                print(
                    f"  GARBAGE_SEND: status_code={e.status_code}"
                    f" type={getattr(e, 'type', None)!r}"
                    f" body={e.body!r}"
                )

            # --- Probe: DELETE the real session, then POST to it via SDK ---
            print("\n== Probe: events.send to DELETED session id (SDK) ==")
            del_status = await delete_session(http, api_key, session_id)
            print(f"  Deleted session {session_id} -> HTTP {del_status}")
            try:
                await sdk_client.beta.sessions.events.send(
                    session_id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": "probe"}],
                        }
                    ],
                )
                print("  DELETED_SEND: no error raised (unexpected)")
            except anthropic.NotFoundError as e:
                print(
                    f"  DELETED_SEND: status_code={e.status_code}"
                    f" type={getattr(e, 'type', None)!r}"
                    f" body={e.body!r}"
                )
            except anthropic.APIStatusError as e:
                print(
                    f"  DELETED_SEND: status_code={e.status_code}"
                    f" type={getattr(e, 'type', None)!r}"
                    f" body={e.body!r}"
                )

        finally:
            await cleanup(http, api_key, agent_id, env_id)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    alive_map: dict[str, str] = {}
    first_failure_at: str | None = None
    failure_mode: str | None = None

    for cp in report.checkpoints:
        session_ok = cp.session_get_status == 200
        stream_ok = cp.post_stream_ok
        alive = session_ok and stream_ok
        alive_map[cp.label] = "y" if alive else "n"
        if not alive and first_failure_at is None:
            first_failure_at = cp.label
            if cp.error:
                failure_mode = cp.error
            elif not session_ok:
                failure_mode = f"session_GET={cp.session_get_status}"
            elif not stream_ok:
                failure_mode = f"stream_resume failed; stop_reason={cp.stop_reason!r}"

    # Fill in any checkpoints we never reached (session died early).
    for target_s in CHECKPOINTS:
        lbl = f"{target_s}s"
        if lbl not in alive_map:
            alive_map[lbl] = "?"

    alive_parts = " ".join(f"{lbl}={v}" for lbl, v in alive_map.items())
    print(
        f"IDLE_TIMEOUT: alive_at {alive_parts}; "
        f"first_failure_at={first_failure_at or 'none_within_10m'}; "
        f"failure_mode={failure_mode or 'n/a'}"
    )


if __name__ == "__main__":
    asyncio.run(main())
