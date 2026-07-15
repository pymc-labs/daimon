"""Probe viability of GET /v1/sessions/{id}/events as a reconnect replay source.

Two questions this probe answers:

1. **Propagation lag.** When an event is delivered over the SSE stream, how
   quickly does it appear in GET /events? If GET lags SSE by seconds, a fast
   reconnect could momentarily rebuild a stale state. Measurement: for every
   SSE event received, immediately issue a GET /events and record whether the
   event id is present, plus wall-clock delta.

2. **Event history size & pagination.** How large can a session's event log
   grow, what `limit` values does the API honor, and how do you walk full
   history? Measurement: accumulate a few turns, then sweep limits
   (1 / 10 / 100 / 1_000 / 10_000) and walk pagination to find total count,
   max per-page, page-token shape, and response byte size.

Usage:
    uv run python scripts/probes/managed_agents/events_endpoint_viability.py

Requires ANTHROPIC_API_KEY. Creates one cheap agent + env + session and
cleans them up (archive agent, delete env).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv
from httpx_sse import aconnect_sse

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


def headers(api_key: str, json_ct: bool = True) -> dict[str, str]:
    h = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


@dataclass
class SseObservation:
    event_id: str
    event_type: str
    t_sse_received: float  # monotonic seconds
    t_get_returned: float | None = None  # when a GET first included this id
    get_poll_count: int = 0


@dataclass
class TurnReport:
    sse_events: list[SseObservation] = field(default_factory=list)


async def create_minimal_agent_env(http: httpx.AsyncClient, api_key: str) -> tuple[str, int, str]:
    suffix = uuid.uuid4().hex[:8]
    r = await http.post(
        f"{API_BASE}/agents",
        headers=headers(api_key),
        json={
            "name": f"probe-events-{suffix}",
            "model": {"id": "claude-haiku-4-5", "speed": "standard"},
            "system": "You are a probe. Reply concisely.",
            "skills": [],
            "tools": [],
            "mcp_servers": [],
        },
    )
    r.raise_for_status()
    a = r.json()
    agent_id = a["id"]
    agent_version = a["version"]

    r = await http.post(
        f"{API_BASE}/environments",
        headers=headers(api_key),
        json={
            "name": f"probe-events-env-{suffix}",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    r.raise_for_status()
    env_id = r.json()["id"]
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
    return r.json()["id"]


async def send_user_message(
    http: httpx.AsyncClient, api_key: str, session_id: str, text: str
) -> None:
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=headers(api_key),
        json={"events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]},
    )
    r.raise_for_status()


async def probe_one_turn_lag(
    http: httpx.AsyncClient, api_key: str, session_id: str, prompt: str
) -> TurnReport:
    """Open SSE, send a message, and for each SSE event poll GET /events until
    the event id is visible. Reports per-event lag."""
    report = TurnReport()
    sse_hdrs = {k: v for k, v in headers(api_key).items() if k != "content-type"}

    async with aconnect_sse(
        http,
        "GET",
        f"{API_BASE}/sessions/{session_id}/events/stream",
        headers=sse_hdrs,
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    ) as es:
        # Send after stream opens (matches sse-smoke invariant).
        await send_user_message(http, api_key, session_id, prompt)

        async for sse_event in es.aiter_sse():
            try:
                raw = json.loads(sse_event.data)
            except Exception:
                continue
            eid = raw.get("id")
            etype = raw.get("type", "?")
            if not eid:
                continue

            obs = SseObservation(event_id=eid, event_type=etype, t_sse_received=time.monotonic())
            report.sse_events.append(obs)

            # Poll GET /events up to ~3s, 150ms cadence, until this id appears.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                obs.get_poll_count += 1
                r = await http.get(
                    f"{API_BASE}/sessions/{session_id}/events",
                    headers=headers(api_key, json_ct=False),
                    params={"limit": 200, "order": "desc"},
                )
                r.raise_for_status()
                ids = {e.get("id") for e in r.json().get("data", [])}
                if eid in ids:
                    obs.t_get_returned = time.monotonic()
                    break
                await asyncio.sleep(0.15)

            if etype == "session.status_idle":
                break

    return report


async def probe_pagination(http: httpx.AsyncClient, api_key: str, session_id: str) -> None:
    """Sweep limits, walk full pagination, characterize page-token shape."""
    print("\n== Pagination & size characterization ==")

    # 1. Walk full history to get ground-truth total.
    total = 0
    page: str | None = None
    pages_walked = 0
    page_token_keys: set[str] = set()
    first_page_body_bytes = 0
    while True:
        params: dict[str, str | int] = {"limit": 100, "order": "asc"}
        if page is not None:
            params["page"] = page
        r = await http.get(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=headers(api_key, json_ct=False),
            params=params,
        )
        r.raise_for_status()
        body = r.json()
        if pages_walked == 0:
            first_page_body_bytes = len(r.content)
            page_token_keys = {k for k in body.keys() if k != "data"}
        data = body.get("data", [])
        total += len(data)
        pages_walked += 1
        page = body.get("next_page_token") or body.get("next_page")
        if not page:
            break
    print(
        f"  walked pagination: total_events={total} pages={pages_walked} "
        f"top_level_keys_besides_data={sorted(page_token_keys)}"
    )
    print(f"  first-page response bytes (limit=100): {first_page_body_bytes}")

    # 2. Sweep limit values — see what the API honors.
    print("\n  limit sweep (order=asc, one request each):")
    for lim in [1, 10, 100, 1_000, 10_000, 100_000]:
        t0 = time.monotonic()
        r = await http.get(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=headers(api_key, json_ct=False),
            params={"limit": lim, "order": "asc"},
        )
        dt_ms = (time.monotonic() - t0) * 1000
        if r.status_code >= 400:
            print(f"    limit={lim:>7} -> HTTP {r.status_code} body={r.text[:200]}")
            continue
        body = r.json()
        data = body.get("data", [])
        extra_keys = sorted(k for k in body.keys() if k != "data")
        has_next = bool(body.get("next_page_token") or body.get("next_page"))
        print(
            f"    limit={lim:>7} returned={len(data):>4} bytes={len(r.content):>7} "
            f"t={dt_ms:>6.0f}ms next_page={has_next} keys_besides_data={extra_keys}"
        )


def summarize_lag(report: TurnReport) -> None:
    print("\n== Propagation lag (SSE -> GET /events) ==")
    print(f"  SSE events observed: {len(report.sse_events)}")
    lags_ms: list[float] = []
    never_visible = 0
    for obs in report.sse_events:
        if obs.t_get_returned is None:
            never_visible += 1
            print(
                f"    {obs.event_type:<32} id={obs.event_id} "
                f"NOT VISIBLE within 3s ({obs.get_poll_count} polls)"
            )
            continue
        lag_ms = (obs.t_get_returned - obs.t_sse_received) * 1000
        lags_ms.append(lag_ms)
        print(
            f"    {obs.event_type:<32} id={obs.event_id} "
            f"lag={lag_ms:>6.0f}ms polls={obs.get_poll_count}"
        )
    if lags_ms:
        lags_sorted = sorted(lags_ms)
        n = len(lags_sorted)
        print(
            f"\n  lag stats: n={n} min={min(lags_ms):.0f}ms "
            f"median={lags_sorted[n // 2]:.0f}ms max={max(lags_ms):.0f}ms"
        )
    if never_visible:
        print(f"  ** {never_visible} events never appeared in GET within 3s **")


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


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    async with httpx.AsyncClient(timeout=60) as http:
        print("Creating probe agent + env + session...")
        agent_id, agent_version, env_id = await create_minimal_agent_env(http, api_key)
        session_id = await create_session(http, api_key, agent_id, agent_version, env_id)
        print(f"  agent={agent_id} v={agent_version}  env={env_id}")
        print(f"  session={session_id}")

        try:
            # Turn 1 — lag measurement.
            print("\n== Turn 1: propagation-lag probe ==")
            rep = await probe_one_turn_lag(http, api_key, session_id, "Reply with exactly: hi.")
            summarize_lag(rep)

            # Turn 2–4 — accumulate more events for pagination probe.
            for i, p in enumerate(
                [
                    "Count from 1 to 5, each on its own line.",
                    "Name three colors, one per line.",
                    "Say goodbye in five words.",
                ],
                start=2,
            ):
                print(f"\n== Turn {i}: accumulating events (short) ==")
                # Quick streamless send + drain via GET once StatusIdle shows up.
                await send_user_message(http, api_key, session_id, p)
                # Wait for StatusIdle via GET polling (cheap, no SSE needed).
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    r = await http.get(
                        f"{API_BASE}/sessions/{session_id}/events",
                        headers=headers(api_key, json_ct=False),
                        params={"limit": 20, "order": "desc"},
                    )
                    r.raise_for_status()
                    data = r.json().get("data", [])
                    if data and data[0].get("type") == "session.status_idle":
                        break
                    await asyncio.sleep(0.5)

            await probe_pagination(http, api_key, session_id)

        finally:
            await cleanup(http, api_key, agent_id, env_id)


if __name__ == "__main__":
    asyncio.run(main())
