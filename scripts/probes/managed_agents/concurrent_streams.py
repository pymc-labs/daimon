"""Probe: can two SSE streams be open on the same session concurrently?

Two experiments:

1. CONCURRENT_STREAMS — open stream #1, send a long user.message, then immediately
   open stream #2 on the same session. Run both to session.status_idle.
   Observe: HTTP status on each, event overlap, whether one kills the other.

2. INTERRUPT_ACK — mid-turn, post user.interrupt, then open stream #2 to watch
   for the idle ack. Observe which stream(s) receive the ack.

Usage:
    uv run python scripts/probes/managed_agents/concurrent_streams.py

Requires ANTHROPIC_API_KEY. Creates one cheap agent + env + two sessions and
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


def sse_headers(api_key: str) -> dict[str, str]:
    return {k: v for k, v in headers(api_key).items() if k != "content-type"}


@dataclass
class StreamEvent:
    stream_id: int
    event_id: str
    event_type: str
    t: float  # monotonic


@dataclass
class StreamResult:
    stream_id: int
    connect_status: int | None = None
    connect_error: str | None = None
    events: list[StreamEvent] = field(default_factory=list)
    ended_cleanly: bool = False
    ended_error: str | None = None


async def create_minimal_agent_env(
    http: httpx.AsyncClient, api_key: str, suffix: str
) -> tuple[str, int, str]:
    r = await http.post(
        f"{API_BASE}/agents",
        headers=headers(api_key),
        json={
            "name": f"probe-concurrent-{suffix}",
            "model": {"id": "claude-haiku-4-5", "speed": "standard"},
            "system": "You are a probe assistant. Reply verbosely as instructed.",
            "skills": [],
            "tools": [],
            "mcp_servers": [],
        },
    )
    r.raise_for_status()
    a = r.json()
    agent_id, agent_version = a["id"], a["version"]

    r = await http.post(
        f"{API_BASE}/environments",
        headers=headers(api_key),
        json={
            "name": f"probe-concurrent-env-{suffix}",
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


async def send_user_interrupt(http: httpx.AsyncClient, api_key: str, session_id: str) -> None:
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=headers(api_key),
        json={"events": [{"type": "user.interrupt"}]},
    )
    print(f"  [interrupt] POST user.interrupt -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"  [interrupt] body: {r.text[:300]}")


async def consume_stream(
    api_key: str,
    session_id: str,
    stream_id: int,
    result: StreamResult,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Open an SSE stream and collect events until session.status_idle or stop_event."""
    url = f"{API_BASE}/sessions/{session_id}/events/stream"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            try:
                async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                    result.connect_status = es.response.status_code
                    print(f"  [stream{stream_id}] connected HTTP {result.connect_status}")
                    if result.connect_status >= 400:
                        result.connect_error = await es.response.aread()
                        print(f"  [stream{stream_id}] error body: {result.connect_error!r:.200}")
                        return
                    async for sse in es.aiter_sse():
                        if stop_event and stop_event.is_set():
                            break
                        try:
                            raw = json.loads(sse.data)
                        except Exception:
                            continue
                        eid = raw.get("id")
                        etype = raw.get("type", "?")
                        if not eid:
                            continue
                        ev = StreamEvent(
                            stream_id=stream_id,
                            event_id=eid,
                            event_type=etype,
                            t=time.monotonic(),
                        )
                        result.events.append(ev)
                        print(f"  [stream{stream_id}] t={ev.t:.3f} id={eid} type={etype}")
                        if etype == "session.status_idle":
                            result.ended_cleanly = True
                            break
            except httpx.HTTPStatusError as e:
                result.connect_status = e.response.status_code
                result.connect_error = str(e)
                print(f"  [stream{stream_id}] HTTPStatusError: {e}")
    except Exception as e:
        result.ended_error = str(e)
        print(f"  [stream{stream_id}] exception: {e}")


# ---------------------------------------------------------------------------
# Experiment 1: Concurrent streams from the start
# ---------------------------------------------------------------------------


async def experiment_concurrent_streams(
    api_key: str,
    agent_id: str,
    agent_version: int,
    env_id: str,
) -> tuple[StreamResult, StreamResult]:
    print("\n" + "=" * 60)
    print("EXPERIMENT 1: Concurrent streams — both open from the start")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=30) as http:
        session_id = await create_session(http, api_key, agent_id, agent_version, env_id)
    print(f"  session={session_id}")

    r1 = StreamResult(stream_id=1)
    r2 = StreamResult(stream_id=2)

    # Stream #1 task — will open, then we send the message, then it runs.
    # Stream #2 opens immediately after message is sent (~0 delay).
    stream1_ready = asyncio.Event()

    async def stream1_task() -> None:
        url = f"{API_BASE}/sessions/{session_id}/events/stream"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            ) as http:
                async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                    r1.connect_status = es.response.status_code
                    print(f"  [stream1] connected HTTP {r1.connect_status}")
                    if r1.connect_status >= 400:
                        r1.connect_error = str(await es.response.aread())
                        stream1_ready.set()
                        return
                    stream1_ready.set()  # signal: stream1 is up, ready to send message
                    async for sse in es.aiter_sse():
                        try:
                            raw = json.loads(sse.data)
                        except Exception:
                            continue
                        eid = raw.get("id")
                        etype = raw.get("type", "?")
                        if not eid:
                            continue
                        ev = StreamEvent(
                            stream_id=1, event_id=eid, event_type=etype, t=time.monotonic()
                        )
                        r1.events.append(ev)
                        print(f"  [stream1] t={ev.t:.3f} id={eid} type={etype}")
                        if etype == "session.status_idle":
                            r1.ended_cleanly = True
                            break
        except Exception as e:
            r1.ended_error = str(e)
            print(f"  [stream1] exception: {e}")
            stream1_ready.set()

    async def stream2_and_message_task() -> None:
        # Wait for stream1 to be connected.
        await stream1_ready.wait()
        # Send user message via a separate client.
        async with httpx.AsyncClient(timeout=30) as http2:
            prompt = (
                "Count from 1 to 20, one number per paragraph. "
                "For each number, write two sentences about what that number is famous for in mathematics or science."
            )
            print("  [ctrl] sending user.message...")
            await send_user_message(http2, api_key, session_id, prompt)
            print("  [ctrl] user.message sent, opening stream2 immediately")

        # Open stream2 right after message send — no sleep.
        await consume_stream(api_key, session_id, 2, r2)

    t1 = asyncio.create_task(stream1_task())
    t2 = asyncio.create_task(stream2_and_message_task())
    await asyncio.gather(t1, t2)

    return r1, r2


# ---------------------------------------------------------------------------
# Experiment 2: Interrupt ack — stream #2 opened mid-turn to catch the ack
# ---------------------------------------------------------------------------


async def experiment_interrupt_ack(
    api_key: str,
    agent_id: str,
    agent_version: int,
    env_id: str,
) -> tuple[StreamResult, StreamResult]:
    print("\n" + "=" * 60)
    print("EXPERIMENT 2: Interrupt ack — stream #2 opened after interrupt")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=30) as http:
        session_id = await create_session(http, api_key, agent_id, agent_version, env_id)
    print(f"  session={session_id}")

    r1 = StreamResult(stream_id=1)
    r2 = StreamResult(stream_id=2)

    # We want to interrupt mid-turn. Strategy:
    #   - Stream #1 opens.
    #   - Send the long message.
    #   - After stream1 receives N events (mid-turn), send user.interrupt.
    #   - Open stream2 immediately after interrupt.
    #   - Both run until session.status_idle (or error).

    interrupt_sent = asyncio.Event()
    stream2_done = asyncio.Event()
    EVENTS_BEFORE_INTERRUPT = 3  # fire interrupt after this many events on stream1

    async def stream1_with_interrupt_trigger() -> None:
        url = f"{API_BASE}/sessions/{session_id}/events/stream"
        interrupt_fired = False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            ) as http_s1:
                async with aconnect_sse(http_s1, "GET", url, headers=sse_headers(api_key)) as es:
                    r1.connect_status = es.response.status_code
                    print(f"  [stream1] connected HTTP {r1.connect_status}")
                    if r1.connect_status >= 400:
                        r1.connect_error = str(await es.response.aread())
                        interrupt_sent.set()
                        return

                    # Send message from within the stream1 context.
                    async with httpx.AsyncClient(timeout=30) as http_msg:
                        prompt = (
                            "Count from 1 to 30, one number per paragraph. "
                            "For each number, write two sentences explaining its significance in history or culture."
                        )
                        print("  [ctrl] sending user.message (exp2)...")
                        await send_user_message(http_msg, api_key, session_id, prompt)
                        print("  [ctrl] user.message sent (exp2)")

                    async for sse in es.aiter_sse():
                        try:
                            raw = json.loads(sse.data)
                        except Exception:
                            continue
                        eid = raw.get("id")
                        etype = raw.get("type", "?")
                        if not eid:
                            continue
                        ev = StreamEvent(
                            stream_id=1, event_id=eid, event_type=etype, t=time.monotonic()
                        )
                        r1.events.append(ev)
                        print(f"  [stream1] t={ev.t:.3f} id={eid} type={etype}")

                        # After N events, fire interrupt (once only).
                        if not interrupt_fired and len(r1.events) >= EVENTS_BEFORE_INTERRUPT:
                            interrupt_fired = True
                            async with httpx.AsyncClient(timeout=30) as http_int:
                                await send_user_interrupt(http_int, api_key, session_id)
                            interrupt_sent.set()

                        if etype == "session.status_idle":
                            r1.ended_cleanly = True
                            break
        except Exception as e:
            r1.ended_error = str(e)
            print(f"  [stream1] exception: {e}")
            interrupt_sent.set()

    async def stream2_after_interrupt() -> None:
        await interrupt_sent.wait()
        print("  [ctrl] interrupt sent, opening stream2 immediately")
        await consume_stream(api_key, session_id, 2, r2)
        stream2_done.set()

    t1 = asyncio.create_task(stream1_with_interrupt_trigger())
    t2 = asyncio.create_task(stream2_after_interrupt())
    await asyncio.gather(t1, t2)

    return r1, r2


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def analyze_concurrent(r1: StreamResult, r2: StreamResult) -> dict[str, str]:
    accepted_both = (
        r1.connect_status is not None
        and r1.connect_status < 400
        and r2.connect_status is not None
        and r2.connect_status < 400
    )
    ids1 = {e.event_id for e in r1.events}
    ids2 = {e.event_id for e in r2.events}
    overlap = ids1 & ids2
    events_split = bool(ids1 - ids2 or ids2 - ids1) and bool(ids1) and bool(ids2)
    events_same = ids1 == ids2 and bool(ids1)

    # Did stream2 opening kill stream1? Stream1 ends with error after stream2 opened.
    second_kills_first = bool(r1.ended_error) and not r1.ended_cleanly

    # Timing: for events in overlap, compare timestamps.
    staggered = False
    if overlap:
        by_id: dict[str, list[StreamEvent]] = {}
        for e in r1.events + r2.events:
            by_id.setdefault(e.event_id, []).append(e)
        deltas = []
        for eid, evs in by_id.items():
            if len(evs) == 2:
                deltas.append(abs(evs[0].t - evs[1].t))
        if deltas:
            avg_delta_ms = sum(deltas) / len(deltas) * 1000
            staggered = avg_delta_ms > 50  # >50ms average delta = staggered
            print(f"\n  [analysis] avg timing delta for shared events: {avg_delta_ms:.1f}ms")

    return {
        "accepted": "yes" if accepted_both else "no",
        "http_s1": str(r1.connect_status),
        "http_s2": str(r2.connect_status),
        "ids_s1": str(len(ids1)),
        "ids_s2": str(len(ids2)),
        "overlap_count": str(len(overlap)),
        "events_split": "yes" if events_split else ("no" if events_same else "one_empty"),
        "second_kills_first": "yes" if second_kills_first else "no",
        "s1_clean": "yes" if r1.ended_cleanly else "no",
        "s2_clean": "yes" if r2.ended_cleanly else "no",
        "delivery": "staggered" if staggered else "simultaneous",
    }


def analyze_interrupt(r1: StreamResult, r2: StreamResult) -> dict[str, str]:
    idle_on_s1 = any(e.event_type == "session.status_idle" for e in r1.events)
    idle_on_s2 = any(e.event_type == "session.status_idle" for e in r2.events)

    if idle_on_s1 and idle_on_s2:
        ack_visible = "both"
    elif idle_on_s1:
        ack_visible = "stream1"
    elif idle_on_s2:
        ack_visible = "stream2"
    else:
        ack_visible = "neither"

    return {
        "ack_visible_on": ack_visible,
        "s1_events": str(len(r1.events)),
        "s2_events": str(len(r2.events)),
        "s1_http": str(r1.connect_status),
        "s2_http": str(r2.connect_status),
    }


async def cleanup(api_key: str, agent_id: str, env_id: str) -> None:
    print("\n== Cleanup ==")
    async with httpx.AsyncClient(timeout=30) as http:
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

    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(timeout=30) as http:
        print("Creating probe agent + env...")
        agent_id, agent_version, env_id = await create_minimal_agent_env(http, api_key, suffix)
        print(f"  agent={agent_id} v={agent_version}  env={env_id}")

    try:
        c_r1, c_r2 = await experiment_concurrent_streams(api_key, agent_id, agent_version, env_id)
        c_stats = analyze_concurrent(c_r1, c_r2)

        i_r1, i_r2 = await experiment_interrupt_ack(api_key, agent_id, agent_version, env_id)
        i_stats = analyze_interrupt(i_r1, i_r2)

    finally:
        await cleanup(api_key, agent_id, env_id)

    # --- Final summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(
        f"CONCURRENT STREAMS: "
        f"accepted={c_stats['accepted']}, "
        f"http_s1={c_stats['http_s1']}, "
        f"http_s2={c_stats['http_s2']}, "
        f"events_s1={c_stats['ids_s1']}, "
        f"events_s2={c_stats['ids_s2']}, "
        f"overlap={c_stats['overlap_count']}, "
        f"events_split={c_stats['events_split']}, "
        f"second_kills_first={c_stats['second_kills_first']}, "
        f"delivery={c_stats['delivery']}, "
        f"s1_clean={c_stats['s1_clean']}, "
        f"s2_clean={c_stats['s2_clean']}"
    )
    print(
        f"INTERRUPT_ACK_VISIBLE_ON: {i_stats['ack_visible_on']} "
        f"(s1_events={i_stats['s1_events']}, s2_events={i_stats['s2_events']}, "
        f"s1_http={i_stats['s1_http']}, s2_http={i_stats['s2_http']})"
    )


if __name__ == "__main__":
    asyncio.run(main())
