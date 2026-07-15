"""Probe: characterize MA behavior when user.interrupt is sent mid-turn.

Questions answered:
  1. What stop_reason.type does session.status_idle carry after interrupt?
     (end_turn? retries_exhausted? something else?)
  2. What events appear between user.interrupt and the terminal idle?
     (partial content? error events? nothing?)
  3. How fast is the ack (ms from POST to terminal idle)?
  4. Does timing differ when interrupted during streaming vs tool execution?
  5. What happens when interrupting an already-idle session?
  6. Does the ack-waiter stream (opened after interrupt) see pre-interrupt events?

Three experiments:
  EXP1 — interrupt during text streaming (long verbose response)
  EXP2 — interrupt an already-idle session
  EXP3 — interrupt during tool execution (MCP tool call with network I/O)

Usage:
    set -a && source .env && set +a
    uv run python scripts/probes/managed_agents/interrupt_behavior.py

Requires ANTHROPIC_API_KEY.
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


def api_headers(api_key: str, json_ct: bool = True) -> dict[str, str]:
    h = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
    }
    if json_ct:
        h["content-type"] = "application/json"
    return h


def sse_headers(api_key: str) -> dict[str, str]:
    return {k: v for k, v in api_headers(api_key).items() if k != "content-type"}


# -- Forensic event log -----------------------------------------------------

@dataclass
class RawEvent:
    t_mono: float
    event_type: str
    event_id: str
    raw: dict
    phase: str  # "pre_interrupt", "post_interrupt", "ack_stream"


@dataclass
class ExperimentResult:
    name: str
    events: list[RawEvent] = field(default_factory=list)
    interrupt_post_t: float | None = None
    interrupt_http_status: int | None = None
    terminal_idle_t: float | None = None
    terminal_stop_reason: dict | None = None
    ack_stream_events: list[RawEvent] = field(default_factory=list)
    error: str | None = None

    @property
    def ack_latency_ms(self) -> float | None:
        if self.interrupt_post_t and self.terminal_idle_t:
            return (self.terminal_idle_t - self.interrupt_post_t) * 1000
        return None

    @property
    def pre_interrupt_count(self) -> int:
        return sum(1 for e in self.events if e.phase == "pre_interrupt")

    @property
    def post_interrupt_count(self) -> int:
        return sum(1 for e in self.events if e.phase == "post_interrupt")


# -- Setup helpers -----------------------------------------------------------

async def create_agent(
    http: httpx.AsyncClient,
    api_key: str,
    suffix: str,
    *,
    with_tools: bool = False,
) -> tuple[str, int]:
    body: dict = {
        "name": f"probe-interrupt-{suffix}",
        "model": {"id": "claude-haiku-4-5", "speed": "standard"},
        "system": "You are a probe assistant. Follow instructions exactly.",
        "skills": [],
        "tools": [],
        "mcp_servers": [],
    }
    if with_tools:
        body["tools"] = [{"type": "mcp_toolset", "mcp_server_name": "deepwiki"}]
        body["mcp_servers"] = [
            {"type": "url", "url": "https://mcp.deepwiki.com/mcp", "name": "deepwiki"}
        ]
    r = await http.post(f"{API_BASE}/agents", headers=api_headers(api_key), json=body)
    r.raise_for_status()
    j = r.json()
    return j["id"], j["version"]


async def create_env(http: httpx.AsyncClient, api_key: str, suffix: str) -> str:
    r = await http.post(
        f"{API_BASE}/environments",
        headers=api_headers(api_key),
        json={
            "name": f"probe-interrupt-env-{suffix}",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    r.raise_for_status()
    return r.json()["id"]


async def create_session(
    http: httpx.AsyncClient, api_key: str, agent_id: str, version: int, env_id: str
) -> str:
    r = await http.post(
        f"{API_BASE}/sessions",
        headers=api_headers(api_key),
        json={
            "agent": {"type": "agent", "id": agent_id, "version": version},
            "environment_id": env_id,
            "resources": [],
            "vault_ids": [],
        },
    )
    r.raise_for_status()
    return r.json()["id"]


async def send_message(
    http: httpx.AsyncClient, api_key: str, session_id: str, text: str
) -> None:
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=api_headers(api_key),
        json={"events": [{"type": "user.message", "content": [{"type": "text", "text": text}]}]},
    )
    r.raise_for_status()


async def send_interrupt(
    http: httpx.AsyncClient, api_key: str, session_id: str
) -> tuple[float, int]:
    """POST user.interrupt. Returns (monotonic_time, http_status)."""
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=api_headers(api_key),
        json={"events": [{"type": "user.interrupt"}]},
    )
    t = time.monotonic()
    print(f"    [interrupt] POST -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"    [interrupt] body: {r.text[:300]}")
    return t, r.status_code


async def cleanup(api_key: str, agent_ids: list[str], env_id: str) -> None:
    print("\n== Cleanup ==")
    async with httpx.AsyncClient(timeout=30) as http:
        for aid in agent_ids:
            try:
                r = await http.post(
                    f"{API_BASE}/agents/{aid}/archive",
                    headers=api_headers(api_key, json_ct=False),
                )
                print(f"  archive agent {aid} -> {r.status_code}")
            except Exception as e:
                print(f"  archive agent failed: {e}")
        try:
            r = await http.delete(
                f"{API_BASE}/environments/{env_id}",
                headers=api_headers(api_key, json_ct=False),
            )
            print(f"  delete env {env_id} -> {r.status_code}")
        except Exception as e:
            print(f"  delete env failed: {e}")


# -- Experiment 1: Interrupt during text streaming ---------------------------

async def exp_interrupt_during_streaming(
    api_key: str, agent_id: str, version: int, env_id: str
) -> ExperimentResult:
    print("\n" + "=" * 60)
    print("EXP1: Interrupt during text streaming")
    print("=" * 60)

    result = ExperimentResult(name="interrupt_during_streaming")

    async with httpx.AsyncClient(timeout=30) as http:
        session_id = await create_session(http, api_key, agent_id, version, env_id)
    print(f"  session={session_id}")

    EVENTS_BEFORE_INTERRUPT = 3
    interrupt_fired = False

    url = f"{API_BASE}/sessions/{session_id}/events/stream"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                print(f"  [stream] connected HTTP {es.response.status_code}")

                # Send message from a separate client to avoid SSE connection interference
                msg_http = httpx.AsyncClient(timeout=30)
                try:
                    prompt = (
                        "Write a very long and detailed essay about the history of mathematics, "
                        "from ancient Babylon through modern times. Include at least 20 paragraphs. "
                        "Be extremely verbose and detailed."
                    )
                    await send_message(msg_http, api_key, session_id, prompt)
                    print("  [ctrl] user.message sent")
                finally:
                    await msg_http.aclose()

                async for sse in es.aiter_sse():
                    try:
                        raw = json.loads(sse.data)
                    except Exception:
                        continue
                    eid = raw.get("id", "")
                    etype = raw.get("type", "?")
                    if not eid:
                        continue

                    phase = "post_interrupt" if interrupt_fired else "pre_interrupt"
                    ev = RawEvent(
                        t_mono=time.monotonic(),
                        event_type=etype,
                        event_id=eid,
                        raw=raw,
                        phase=phase,
                    )
                    result.events.append(ev)
                    print(f"    [{phase}] t={ev.t_mono:.3f} type={etype} id={eid[:20]}")

                    if etype == "session.status_idle":
                        stop = raw.get("stop_reason", {})
                        result.terminal_stop_reason = stop
                        result.terminal_idle_t = ev.t_mono
                        print(f"    [IDLE] stop_reason={json.dumps(stop)}")
                        break

                    if not interrupt_fired and len(result.events) >= EVENTS_BEFORE_INTERRUPT:
                        interrupt_fired = True
                        async with httpx.AsyncClient(timeout=30) as int_http:
                            t, status = await send_interrupt(int_http, api_key, session_id)
                            result.interrupt_post_t = t
                            result.interrupt_http_status = status

        # Now open a second stream to see what the ack-waiter would see
        print("  [ack_stream] opening second stream to check what it sees...")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                print(f"  [ack_stream] connected HTTP {es.response.status_code}")
                async for sse in es.aiter_sse():
                    try:
                        raw = json.loads(sse.data)
                    except Exception:
                        continue
                    eid = raw.get("id", "")
                    etype = raw.get("type", "?")
                    if not eid:
                        continue
                    ev = RawEvent(
                        t_mono=time.monotonic(),
                        event_type=etype,
                        event_id=eid,
                        raw=raw,
                        phase="ack_stream",
                    )
                    result.ack_stream_events.append(ev)
                    print(f"    [ack_stream] type={etype} id={eid[:20]}")
                    if etype == "session.status_idle":
                        break
                    if len(result.ack_stream_events) > 50:
                        print("    [ack_stream] safety cap — too many events")
                        break

    except Exception as e:
        import traceback
        result.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(f"  [ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()

    return result


# -- Experiment 2: Interrupt an already-idle session -------------------------

async def exp_interrupt_idle_session(
    api_key: str, agent_id: str, version: int, env_id: str
) -> ExperimentResult:
    print("\n" + "=" * 60)
    print("EXP2: Interrupt an already-idle session")
    print("=" * 60)

    result = ExperimentResult(name="interrupt_idle_session")

    async with httpx.AsyncClient(timeout=30) as http:
        session_id = await create_session(http, api_key, agent_id, version, env_id)
    print(f"  session={session_id}")

    # First: run a turn to completion so the session reaches idle
    url = f"{API_BASE}/sessions/{session_id}/events/stream"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                async with httpx.AsyncClient(timeout=30) as msg_http:
                    await send_message(msg_http, api_key, session_id, "Say 'hello' and nothing else.")
                async for sse in es.aiter_sse():
                    try:
                        raw = json.loads(sse.data)
                    except Exception:
                        continue
                    etype = raw.get("type", "?")
                    eid = raw.get("id", "")
                    if eid:
                        print(f"    [warmup] type={etype}")
                    if etype == "session.status_idle":
                        print("    [warmup] session reached idle")
                        break

        # Now interrupt the idle session
        print("  [ctrl] session is idle — sending user.interrupt...")
        async with httpx.AsyncClient(timeout=30) as int_http:
            t, status = await send_interrupt(int_http, api_key, session_id)
            result.interrupt_post_t = t
            result.interrupt_http_status = status

        # Open a stream to see what (if anything) appears
        print("  [post_interrupt_stream] watching for events after interrupt-on-idle...")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=10.0, write=30.0, pool=10.0)
        ) as http:
            try:
                async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                    async for sse in es.aiter_sse():
                        try:
                            raw = json.loads(sse.data)
                        except Exception:
                            continue
                        eid = raw.get("id", "")
                        etype = raw.get("type", "?")
                        if not eid:
                            continue
                        ev = RawEvent(
                            t_mono=time.monotonic(),
                            event_type=etype,
                            event_id=eid,
                            raw=raw,
                            phase="post_interrupt",
                        )
                        result.events.append(ev)
                        print(f"    [post] type={etype} id={eid[:20]}")
                        if etype == "session.status_idle":
                            result.terminal_stop_reason = raw.get("stop_reason", {})
                            result.terminal_idle_t = ev.t_mono
                            break
                        if len(result.events) > 30:
                            print("    [post] safety cap")
                            break
            except httpx.ReadTimeout:
                print("  [post_interrupt_stream] read timeout (no events — expected for idle)")

    except Exception as e:
        result.error = str(e)
        print(f"  [ERROR] {e}")

    return result


# -- Experiment 3: Interrupt during tool execution ---------------------------

async def exp_interrupt_during_tool(
    api_key: str, agent_id_tools: str, version_tools: int, env_id: str
) -> ExperimentResult:
    print("\n" + "=" * 60)
    print("EXP3: Interrupt during tool execution (MCP tool call)")
    print("=" * 60)

    result = ExperimentResult(name="interrupt_during_tool")

    async with httpx.AsyncClient(timeout=30) as http:
        session_id = await create_session(http, api_key, agent_id_tools, version_tools, env_id)
    print(f"  session={session_id}")

    interrupt_fired = False
    url = f"{API_BASE}/sessions/{session_id}/events/stream"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                async with httpx.AsyncClient(timeout=30) as msg_http:
                    prompt = (
                        "Use the read_wiki_structure tool to look up the repository 'facebook/react'. "
                        "Then summarize what you found in detail."
                    )
                    await send_message(msg_http, api_key, session_id, prompt)
                    print("  [ctrl] user.message sent (tool-using prompt)")

                async for sse in es.aiter_sse():
                    try:
                        raw = json.loads(sse.data)
                    except Exception:
                        continue
                    eid = raw.get("id", "")
                    etype = raw.get("type", "?")
                    if not eid:
                        continue

                    phase = "post_interrupt" if interrupt_fired else "pre_interrupt"
                    ev = RawEvent(
                        t_mono=time.monotonic(),
                        event_type=etype,
                        event_id=eid,
                        raw=raw,
                        phase=phase,
                    )
                    result.events.append(ev)
                    print(f"    [{phase}] type={etype} id={eid[:20]}")

                    # Fire interrupt when we see the first tool use event
                    if not interrupt_fired and etype in (
                        "agent.mcp_tool_use",
                        "session.status_idle",
                    ):
                        if etype == "session.status_idle":
                            stop = raw.get("stop_reason", {})
                            if stop.get("type") == "requires_action":
                                # Tool approval pause — interrupt here
                                print("    [ctrl] session paused for tool approval — interrupting")
                            else:
                                result.terminal_stop_reason = stop
                                result.terminal_idle_t = ev.t_mono
                                break
                        else:
                            print(f"    [ctrl] saw tool use — interrupting mid-tool")

                        interrupt_fired = True
                        async with httpx.AsyncClient(timeout=30) as int_http:
                            t, status = await send_interrupt(int_http, api_key, session_id)
                            result.interrupt_post_t = t
                            result.interrupt_http_status = status

                    if etype == "session.status_idle" and interrupt_fired:
                        result.terminal_stop_reason = raw.get("stop_reason", {})
                        result.terminal_idle_t = ev.t_mono
                        break

                    if len(result.events) > 100:
                        print("    safety cap")
                        break

    except Exception as e:
        result.error = str(e)
        print(f"  [ERROR] {e}")

    return result


# -- Analysis and summary ---------------------------------------------------

def analyze(result: ExperimentResult) -> None:
    print(f"\n--- {result.name} ---")
    if result.error:
        print(f"  ERROR: {result.error}")
        return

    print(f"  Pre-interrupt events: {result.pre_interrupt_count}")
    print(f"  Post-interrupt events: {result.post_interrupt_count}")
    print(f"  Interrupt HTTP status: {result.interrupt_http_status}")
    print(f"  Ack latency: {result.ack_latency_ms:.0f}ms" if result.ack_latency_ms else "  Ack latency: N/A")
    print(f"  Terminal stop_reason: {json.dumps(result.terminal_stop_reason)}")

    # Event type breakdown
    pre_types = [e.event_type for e in result.events if e.phase == "pre_interrupt"]
    post_types = [e.event_type for e in result.events if e.phase == "post_interrupt"]
    print(f"  Pre-interrupt event types: {pre_types}")
    print(f"  Post-interrupt event types: {post_types}")

    # Ack stream analysis
    if result.ack_stream_events:
        ack_types = [e.event_type for e in result.ack_stream_events]
        main_ids = {e.event_id for e in result.events}
        ack_ids = {e.event_id for e in result.ack_stream_events}
        replay_count = len(ack_ids & main_ids)
        new_count = len(ack_ids - main_ids)
        print(f"  Ack-stream events: {len(result.ack_stream_events)} total")
        print(f"  Ack-stream types: {ack_types}")
        print(f"  Ack-stream replayed (seen on main): {replay_count}")
        print(f"  Ack-stream new (not on main): {new_count}")


# -- Main -------------------------------------------------------------------

async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    suffix = uuid.uuid4().hex[:8]
    agent_ids: list[str] = []

    async with httpx.AsyncClient(timeout=30) as http:
        print("Creating probe resources...")
        env_id = await create_env(http, api_key, suffix)
        agent_id, version = await create_agent(http, api_key, suffix)
        agent_id_tools, version_tools = await create_agent(
            http, api_key, f"{suffix}-tools", with_tools=True
        )
        agent_ids = [agent_id, agent_id_tools]
        print(f"  env={env_id}")
        print(f"  agent (no tools)={agent_id} v{version}")
        print(f"  agent (tools)={agent_id_tools} v{version_tools}")

    try:
        r1 = await exp_interrupt_during_streaming(api_key, agent_id, version, env_id)
        r2 = await exp_interrupt_idle_session(api_key, agent_id, version, env_id)
        r3 = await exp_interrupt_during_tool(api_key, agent_id_tools, version_tools, env_id)
    finally:
        await cleanup(api_key, agent_ids, env_id)

    # -- Summary --
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in [r1, r2, r3]:
        analyze(r)

    # -- JSON dump for forensic analysis --
    dump_path = os.path.join(os.path.dirname(__file__), "interrupt_behavior_results.json")
    dump = {}
    for r in [r1, r2, r3]:
        dump[r.name] = {
            "interrupt_http_status": r.interrupt_http_status,
            "ack_latency_ms": r.ack_latency_ms,
            "terminal_stop_reason": r.terminal_stop_reason,
            "pre_interrupt_count": r.pre_interrupt_count,
            "post_interrupt_count": r.post_interrupt_count,
            "pre_interrupt_types": [e.event_type for e in r.events if e.phase == "pre_interrupt"],
            "post_interrupt_types": [e.event_type for e in r.events if e.phase == "post_interrupt"],
            "ack_stream_event_count": len(r.ack_stream_events),
            "ack_stream_types": [e.event_type for e in r.ack_stream_events],
            "events": [
                {
                    "phase": e.phase,
                    "type": e.event_type,
                    "id": e.event_id,
                    "raw": e.raw,
                }
                for e in r.events
            ],
            "error": r.error,
        }
    # Write next to the spike README, not in scripts/probes
    dump_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "results.json",
    )
    with open(dump_path, "w") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"\nFull event dump written to {dump_path}")

    # -- Verdict helper --
    print("\n" + "=" * 60)
    print("KEY FINDINGS")
    print("=" * 60)
    if r1.terminal_stop_reason:
        sr_type = r1.terminal_stop_reason.get("type", "UNKNOWN")
        print(f"  1. stop_reason.type after interrupt during streaming: {sr_type}")
        if sr_type == "end_turn":
            print("     -> Our _TERMINAL_STOP_REASONS includes this. send_interrupt_and_wait will ack correctly.")
        elif sr_type == "requires_action":
            print("     -> WARNING: requires_action is NOT in _TERMINAL_STOP_REASONS. send_interrupt_and_wait will timeout!")
        else:
            print(f"     -> UNEXPECTED type. Check if _TERMINAL_STOP_REASONS needs updating.")

    if r1.ack_latency_ms is not None:
        print(f"  2. Ack latency (streaming): {r1.ack_latency_ms:.0f}ms")
    if r3.ack_latency_ms is not None:
        print(f"  3. Ack latency (tool): {r3.ack_latency_ms:.0f}ms")

    print(f"  4. Interrupt-on-idle HTTP status: {r2.interrupt_http_status}")
    if r2.interrupt_http_status and r2.interrupt_http_status < 400:
        print("     -> No-op or accepted. Check if it triggers new events.")
    else:
        print("     -> Rejected. Interrupting idle is an error.")

    if r1.ack_stream_events:
        main_ids = {e.event_id for e in r1.events}
        ack_ids = {e.event_id for e in r1.ack_stream_events}
        replay = len(ack_ids & main_ids)
        print(f"  5. Ack-waiter stream replays: {replay}/{len(ack_ids)} events were pre-interrupt")
        print(f"     -> {'Dedup needed' if replay > 0 else 'Clean stream, no dedup needed'}")


if __name__ == "__main__":
    asyncio.run(main())
