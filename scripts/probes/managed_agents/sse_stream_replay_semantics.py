"""Probe: does GET /v1/sessions/{id}/events/stream replay historical events?

Question answered
-----------------
When you open an SSE stream on a session that already has a completed turn in
its event log, does the stream immediately deliver all historical events before
switching to live ones (REPLAY), or does it deliver only events that occur
after stream-open time (LIVE-ONLY)?

Why it matters
--------------
The `send_message_and_wait` MCP tool (sub-plan 2 §8) follows the pattern:
  1. Open SSE stream.
  2. POST user message.
  3. Collect events from the stream until session.status_idle.
  4. Return collected events as the "turn reply."

If the stream replays history from the session's beginning, step 4 will include
events from prior turns, polluting the returned `SessionReply.events` list.

If the stream is live-only (events from stream-open time onward), no pollution
occurs — but the "open before send" ordering (which prevents a race where a
fast idle fires in the gap between send and stream-open) must still be
preserved.

If the stream replays but the sub-plan 2 design's dedup-by-id is in effect, the
question becomes: does the dedup set (`seen`) get seeded with prior-turn event
ids before the stream opens? The current impl sketch in the spec does NOT pre-
seed `seen` — it starts empty. If there is replay, events from prior turns will
pass the dedup check and land in `events`.

Protocol
--------
1. Create a session.
2. Run TURN 1: send a message, open a stream, drain until session.status_idle.
   Collect all turn-1 event ids. Close stream.
3. Record how many turn-1 events existed.
4. Open a FRESH stream (no message sent yet). Immediately read events for up to
   PROBE_WINDOW_SECONDS. Classify each as:
     - "historical" if its id matches a turn-1 event id
     - "unexpected_new" if it has an unknown id (should not exist; no message sent)
     - "none" if no events arrive within the window
5. Print a pass/fail summary.

Pass
----
  No events arrive within PROBE_WINDOW_SECONDS on the fresh stream before any
  message is sent. The stream is live-only (or at minimum does not deliver prior
  turn events). The sub-plan 2 impl can safely collect events from stream-open
  time without pre-seeding `seen`.

Fail — replay confirmed
-----------------------
  Historical events arrive on the fresh stream. The sub-plan 2 impl MUST pre-
  seed `seen` with all prior event ids from GET /events before opening the
  stream, or the returned `SessionReply.events` will be polluted.

Fail — unexpected new events
-----------------------------
  New unknown-id events arrive before a message is sent. This would indicate
  MA is generating spontaneous events, which is unexpected.

Usage
-----
    uv run python scripts/probes/managed_agents/sse_stream_replay_semantics.py

Requires ANTHROPIC_API_KEY. Creates one agent + env + session and cleans them
up (archive agent, delete env).
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
from httpx_sse import aconnect_sse

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"

# How long to hold the fresh stream open waiting for replay events.
# If replay occurs, historical events should arrive almost immediately.
# 5s is generous; adjust down if the probe is too slow in practice.
PROBE_WINDOW_SECONDS = 5.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers(api_key: str, *, json_ct: bool = True) -> dict[str, str]:
    h: dict[str, str] = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


def _sse_headers(api_key: str) -> dict[str, str]:
    return {k: v for k, v in _headers(api_key).items() if k != "content-type"}


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def _create_agent_env(http: httpx.AsyncClient, api_key: str) -> tuple[str, int, str]:
    suffix = uuid.uuid4().hex[:8]
    r = await http.post(
        f"{API_BASE}/agents",
        headers=_headers(api_key),
        json={
            "name": f"probe-sse-replay-{suffix}",
            "model": {"id": "claude-haiku-4-5", "speed": "standard"},
            "system": "You are a probe agent. Reply in exactly one short sentence.",
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
        headers=_headers(api_key),
        json={
            "name": f"probe-sse-replay-env-{suffix}",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    r.raise_for_status()
    env_id: str = r.json()["id"]
    return agent_id, agent_version, env_id


async def _create_session(
    http: httpx.AsyncClient,
    api_key: str,
    agent_id: str,
    agent_version: int,
    env_id: str,
) -> str:
    r = await http.post(
        f"{API_BASE}/sessions",
        headers=_headers(api_key),
        json={
            "agent": {"type": "agent", "id": agent_id, "version": agent_version},
            "environment_id": env_id,
            "resources": [],
            "vault_ids": [],
        },
    )
    r.raise_for_status()
    return r.json()["id"]


async def _cleanup(http: httpx.AsyncClient, api_key: str, agent_id: str, env_id: str) -> None:
    print("\n== Cleanup ==")
    for label, coro in [
        (
            f"archive agent {agent_id}",
            http.post(
                f"{API_BASE}/agents/{agent_id}/archive",
                headers=_headers(api_key, json_ct=False),
            ),
        ),
        (
            f"delete env {env_id}",
            http.delete(
                f"{API_BASE}/environments/{env_id}",
                headers=_headers(api_key, json_ct=False),
            ),
        ),
    ]:
        try:
            r = await coro
            print(f"  {label} -> {r.status_code}")
        except Exception as e:
            print(f"  {label} FAILED: {e}")


# ---------------------------------------------------------------------------
# Turn 1: run a complete turn and collect event ids
# ---------------------------------------------------------------------------


@dataclass
class Turn1Result:
    event_ids: set[str] = field(default_factory=set)
    event_count: int = 0
    ended_cleanly: bool = False


async def _run_turn1(http: httpx.AsyncClient, api_key: str, session_id: str) -> Turn1Result:
    """Open SSE stream, post a message, drain to session.status_idle. Collect ids."""
    result = Turn1Result()
    url = f"{API_BASE}/sessions/{session_id}/events/stream"
    sse_hdrs = _sse_headers(api_key)

    async with aconnect_sse(
        http,
        "GET",
        url,
        headers=sse_hdrs,
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    ) as es:
        if es.response.status_code >= 400:
            raise RuntimeError(
                f"Turn1 stream connect failed: HTTP {es.response.status_code}"
            )
        print(f"  [turn1] stream connected HTTP {es.response.status_code}")

        # Post message after stream is open.
        r = await http.post(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=_headers(api_key),
            json={
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": "Say 'probe complete' in one sentence."}],
                    }
                ]
            },
        )
        r.raise_for_status()
        print("  [turn1] user.message posted")

        async for sse in es.aiter_sse():
            try:
                raw = json.loads(sse.data)
            except Exception:
                continue
            eid = raw.get("id")
            etype = raw.get("type", "?")
            if not eid:
                continue
            result.event_ids.add(eid)
            result.event_count += 1
            print(f"  [turn1] id={eid} type={etype}")
            if etype == "session.status_idle":
                result.ended_cleanly = True
                break

    return result


# ---------------------------------------------------------------------------
# Probe stream: open fresh stream, listen for PROBE_WINDOW_SECONDS
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    connect_status: int | None = None
    historical_ids_received: list[str] = field(default_factory=list)
    unexpected_new_ids: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    connect_error: str | None = None


async def _run_probe_stream(
    api_key: str,
    session_id: str,
    turn1_ids: set[str],
) -> ProbeResult:
    """Open a fresh stream (no message sent), collect events for PROBE_WINDOW_SECONDS."""
    result = ProbeResult()
    url = f"{API_BASE}/sessions/{session_id}/events/stream"
    sse_hdrs = _sse_headers(api_key)
    t0 = time.monotonic()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_hdrs) as es:
                result.connect_status = es.response.status_code
                print(f"  [probe] fresh stream connected HTTP {result.connect_status}")
                if result.connect_status >= 400:
                    result.connect_error = f"HTTP {result.connect_status}"
                    return result

                # Read events up to the probe window. We use asyncio.wait_for
                # on each next() so we can time out without blocking forever.
                stream_iter = es.aiter_sse().__aiter__()
                deadline = time.monotonic() + PROBE_WINDOW_SECONDS
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        sse = await asyncio.wait_for(
                            stream_iter.__anext__(),  # type: ignore[attr-defined]
                            timeout=remaining,
                        )
                    except (TimeoutError, asyncio.TimeoutError, StopAsyncIteration):
                        break
                    try:
                        raw = json.loads(sse.data)
                    except Exception:
                        continue
                    eid = raw.get("id")
                    etype = raw.get("type", "?")
                    if not eid:
                        continue
                    print(f"  [probe] received id={eid} type={etype}")
                    if eid in turn1_ids:
                        result.historical_ids_received.append(eid)
                    else:
                        result.unexpected_new_ids.append(eid)

    except Exception as e:
        result.connect_error = str(e)
        print(f"  [probe] exception: {e}")

    result.elapsed_s = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    project_root = Path(__file__).resolve().parents[3]
    load_dotenv(project_root / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sibling_env = project_root.parent / "daimon-cma" / ".env"
        if sibling_env.exists():
            load_dotenv(sibling_env)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    async with httpx.AsyncClient(timeout=60) as http:
        print("== Setup: creating agent + env + session ==")
        agent_id, agent_version, env_id = await _create_agent_env(http, api_key)
        session_id = await _create_session(http, api_key, agent_id, agent_version, env_id)
        print(f"  agent={agent_id} v={agent_version}")
        print(f"  env={env_id}")
        print(f"  session={session_id}")

        try:
            # --- Turn 1: complete a turn and collect its event ids ---
            print("\n== Turn 1: running a complete turn ==")
            turn1 = await _run_turn1(http, api_key, session_id)
            print(
                f"  Turn 1 done: events={turn1.event_count} "
                f"ended_cleanly={turn1.ended_cleanly} "
                f"ids={sorted(turn1.event_ids)}"
            )
            if not turn1.ended_cleanly:
                raise RuntimeError("Turn 1 did not reach session.status_idle; aborting probe.")

        except Exception as e:
            await _cleanup(http, api_key, agent_id, env_id)
            raise

    # --- Probe stream: open a fresh stream WITHOUT sending a message ---
    # (Use a separate client to ensure no connection reuse.)
    print(
        f"\n== Probe stream: opening fresh SSE stream, no message, "
        f"listening {PROBE_WINDOW_SECONDS}s =="
    )
    probe = await _run_probe_stream(api_key, session_id, turn1.event_ids)

    # Cleanup
    async with httpx.AsyncClient(timeout=30) as http:
        await _cleanup(http, api_key, agent_id, env_id)

    # --- Analysis ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(f"  turn1_event_count       : {turn1.event_count}")
    print(f"  probe_window_s          : {PROBE_WINDOW_SECONDS}")
    print(f"  probe_elapsed_s         : {probe.elapsed_s:.2f}")
    print(f"  probe_connect_status    : {probe.connect_status}")
    print(f"  historical_ids_received : {len(probe.historical_ids_received)}")
    print(f"  unexpected_new_ids      : {len(probe.unexpected_new_ids)}")

    if probe.connect_error:
        print(f"\nRESULT: INCONCLUSIVE — stream connect error: {probe.connect_error}")
        return

    replay_confirmed = len(probe.historical_ids_received) > 0
    spontaneous = len(probe.unexpected_new_ids) > 0

    if not replay_confirmed and not spontaneous:
        print(
            "\nRESULT: PASS — LIVE-ONLY\n"
            "  No events arrived on the fresh stream within the probe window.\n"
            "  The stream does NOT replay historical events.\n"
            "  send_message_and_wait does NOT need to pre-seed `seen` with prior event ids."
        )
    elif replay_confirmed:
        print(
            f"\nRESULT: FAIL — REPLAY CONFIRMED\n"
            f"  {len(probe.historical_ids_received)} historical event(s) arrived on the "
            f"fresh stream.\n"
            f"  The stream REPLAYS prior-turn events before delivering live ones.\n"
            f"  send_message_and_wait MUST pre-seed `seen` with GET /events ids before\n"
            f"  opening the SSE stream, or SessionReply.events will be polluted."
        )
    else:
        print(
            f"\nRESULT: UNEXPECTED — {len(probe.unexpected_new_ids)} unknown-id event(s) "
            f"arrived before any message was sent. Investigate."
        )

    # Machine-readable one-liner for scripting / CI.
    print(
        f"\nSSE_REPLAY_SEMANTICS: "
        f"replay={'yes' if replay_confirmed else 'no'} "
        f"historical_received={len(probe.historical_ids_received)} "
        f"unexpected_new={len(probe.unexpected_new_ids)} "
        f"window_s={PROBE_WINDOW_SECONDS}"
    )


if __name__ == "__main__":
    asyncio.run(main())
