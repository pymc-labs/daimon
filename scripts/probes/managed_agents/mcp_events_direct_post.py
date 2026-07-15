"""Probe sessions.events.create as a direct-post mechanism (agent-to-agent send).

Spec §5.1 `sessions.send_message` bypasses `daimon.core.sessions` and posts
directly: `client.beta.agents.sessions.events.create(session_id, events=[...])`.

Questions:
  A. Does events.create accept `user.message` outside of a turn?
  B. What happens if the session is currently mid-turn (SSE stream open,
     agent running)? Queued, rejected, or concurrent-written?
  C. Does the posted event appear in GET /events? Does it trigger a new turn
     automatically or does the caller need to open a stream?

Run:
    uv run python scripts/probes/managed_agents/mcp_events_direct_post.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import httpx
from dotenv import load_dotenv
from httpx_sse import aconnect_sse

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


def hdrs(json_ct: bool = True) -> dict[str, str]:
    h = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


async def setup(http: httpx.AsyncClient) -> tuple[str, str, str, int]:
    s = uuid.uuid4().hex[:8]
    r = await http.post(
        f"{API_BASE}/agents",
        headers=hdrs(),
        json={
            "name": f"probe-evt-{s}",
            "model": {"id": "claude-haiku-4-5", "speed": "standard"},
            "system": "Reply with one short sentence.",
            "skills": [],
            "tools": [],
            "mcp_servers": [],
        },
    )
    r.raise_for_status()
    a = r.json()
    r = await http.post(
        f"{API_BASE}/environments",
        headers=hdrs(),
        json={"name": f"probe-env-{s}", "config": {"type": "cloud", "networking": {"type": "unrestricted"}}},
    )
    r.raise_for_status()
    env_id = r.json()["id"]
    r = await http.post(
        f"{API_BASE}/sessions",
        headers=hdrs(),
        json={
            "agent": {"type": "agent", "id": a["id"], "version": a["version"]},
            "environment_id": env_id,
            "resources": [],
            "vault_ids": [],
        },
    )
    r.raise_for_status()
    return a["id"], env_id, r.json()["id"], a["version"]


async def post_event(http, session_id, body):
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events", headers=hdrs(), json=body
    )
    print(f"  POST events: {r.status_code} {r.text[:250]}")
    return r


async def get_events(http, session_id, **params):
    r = await http.get(
        f"{API_BASE}/sessions/{session_id}/events", headers=hdrs(json_ct=False), params=params
    )
    return r.status_code, r.json() if r.status_code == 200 else r.text


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=60.0) as http:
        agent_id, env_id, sid, av = await setup(http)
        print(f"agent={agent_id} env={env_id} session={sid}")

        # --- A. Post user.message outside of any stream ---
        print("\n=== A. Post user.message outside a turn ===")
        await post_event(
            http,
            sid,
            {"events": [{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}]},
        )
        # Check GET /events — does it show up? Does MA start a turn on its own?
        await asyncio.sleep(1.0)
        status, data = await get_events(http, sid, limit=50, order="asc")
        evts = data.get("data", []) if isinstance(data, dict) else []
        print(f"  events count after post: {len(evts)}")
        for e in evts[:10]:
            print(f"    {e.get('type')} id={e.get('id')}")

        # --- B. Open stream, wait to see if MA drives the turn, or if we need to nudge ---
        print("\n=== B. Open stream after a 3s gap — does MA auto-drive? ===")
        saw_stream_events = 0
        sse_h = {k: v for k, v in hdrs().items() if k != "content-type"}
        t0 = time.monotonic()
        try:
            async with aconnect_sse(
                http,
                "GET",
                f"{API_BASE}/sessions/{sid}/events/stream",
                headers=sse_h,
                timeout=httpx.Timeout(connect=10.0, read=10.0, write=30.0, pool=10.0),
            ) as es:
                async for ev in es.aiter_sse():
                    saw_stream_events += 1
                    if saw_stream_events <= 5:
                        try:
                            raw = json.loads(ev.data)
                            print(f"  stream evt #{saw_stream_events} type={raw.get('type')} id={raw.get('id')}")
                        except Exception:
                            print(f"  stream evt #{saw_stream_events} raw={ev.data[:80]}")
                    if time.monotonic() - t0 > 8:
                        break
        except Exception as e:  # noqa: BLE001
            print(f"  stream err: {type(e).__name__}: {e}")
        print(f"  stream events in 8s: {saw_stream_events}")

        # --- C. Concurrent write during active turn ---
        print("\n=== C. Post user.message mid-turn (stream open, turn in flight) ===")
        async def drive_turn_and_post():
            posted = asyncio.Event()
            seen = {"count": 0}

            async def stream_task():
                async with aconnect_sse(
                    http,
                    "GET",
                    f"{API_BASE}/sessions/{sid}/events/stream",
                    headers=sse_h,
                    timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
                ) as es:
                    await post_event(
                        http,
                        sid,
                        {"events": [{"type": "user.message", "content": [{"type": "text", "text": "count slowly to ten"}]}]},
                    )
                    async for ev in es.aiter_sse():
                        seen["count"] += 1
                        try:
                            raw = json.loads(ev.data)
                            et = raw.get("type", "?")
                            if seen["count"] <= 20:
                                print(f"    stream: {et}")
                            if et == "agent.text_delta" and not posted.is_set():
                                posted.set()
                                # Mid-turn: try to post another user.message.
                                r = await http.post(
                                    f"{API_BASE}/sessions/{sid}/events",
                                    headers=hdrs(),
                                    json={"events": [{"type": "user.message", "content": [{"type": "text", "text": "INTERRUPT"}]}]},
                                )
                                print(f"    [mid-turn POST /events] {r.status_code} {r.text[:200]}")
                            if et in ("session.turn_complete", "agent.turn_end", "turn.completed"):
                                break
                        except Exception:
                            pass

            await asyncio.wait_for(stream_task(), timeout=30.0)

        try:
            await drive_turn_and_post()
        except TimeoutError:
            print("    (timed out after 30s)")

        print("\n=== Cleanup ===")
        r = await http.post(f"{API_BASE}/agents/{agent_id}/archive", headers=hdrs())
        print(f"  archive agent: {r.status_code}")
        r = await http.delete(f"{API_BASE}/environments/{env_id}", headers=hdrs())
        print(f"  delete env: {r.status_code}")

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
