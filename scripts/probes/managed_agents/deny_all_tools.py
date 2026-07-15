"""Probe: what happens when you deny ALL tools in a requires_action MA session?

Questions answered:
  1. Does the agent produce a text response after deny-all?
  2. Does it try more tools after deny-all?
  3. What is the final stop_reason.type?
  4. Is there SSE output to consume after the deny POST?
  5. How many agent.message events appear, and what do they say?

Methodology:
  - Create an agent whose tools use permission_policy="always_ask" (via MCP deepwiki)
  - Send a message that forces immediate tool use
  - Wait for requires_action idle
  - Deny all pending tool use IDs in one POST
  - Consume the stream until the next terminal idle
  - Report every SSE event and the final state

Runs multiple experiments:
  EXP1 — deny-all on a single pending tool
  EXP2 — deny-all on multiple parallel tools (if the agent issues >1)

Run:
    set -a && source .env && set +a
    uv run python scripts/probes/managed_agents/deny_all_tools.py

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
DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"


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


# -- Data structures ---------------------------------------------------------


@dataclass
class RawEvent:
    t_mono: float
    event_type: str
    event_id: str
    raw: dict
    phase: str  # "pre_deny", "post_deny"


@dataclass
class ExperimentResult:
    name: str
    session_id: str = ""
    events: list[RawEvent] = field(default_factory=list)
    requires_action_stop: dict = field(default_factory=dict)
    pending_tool_ids: list[str] = field(default_factory=list)
    deny_http_status: int | None = None
    deny_response_body: str = ""
    deny_post_t: float | None = None
    final_stop_reason: dict = field(default_factory=dict)
    final_idle_t: float | None = None
    agent_messages_after_deny: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def latency_to_final_idle_ms(self) -> float | None:
        if self.deny_post_t and self.final_idle_t:
            return (self.final_idle_t - self.deny_post_t) * 1000
        return None


# -- Infrastructure ----------------------------------------------------------


async def create_env(http: httpx.AsyncClient, api_key: str, suffix: str) -> str:
    r = await http.post(
        f"{API_BASE}/environments",
        headers=api_headers(api_key),
        json={
            "name": f"probe-denyall-env-{suffix}",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    r.raise_for_status()
    return r.json()["id"]


async def create_agent(
    http: httpx.AsyncClient, api_key: str, suffix: str, *, parallel_tools: bool = False
) -> tuple[str, int]:
    """Create an agent wired to deepwiki MCP with always-ask permission policy."""
    system = (
        "You MUST call read_wiki_structure immediately in your first response. "
        "Use repoName='facebook/react'. Do not ask for permission. Call the tool now."
    )
    if parallel_tools:
        system = (
            "You MUST call read_wiki_structure TWICE in your FIRST response "
            "as parallel tool calls: once for repoName='facebook/react' and once for "
            "repoName='vercel/next.js'. Emit both tool_use blocks in the same assistant turn."
        )
    body = {
        "name": f"probe-denyall-{suffix}",
        "model": {"id": "claude-haiku-4-5", "speed": "standard"},
        "system": system,
        "skills": [],
        "tools": [
            {
                "type": "mcp_toolset",
                "mcp_server_name": "deepwiki",
                "permission_policy": {"policy": "always_ask"},
            }
        ],
        "mcp_servers": [{"type": "url", "url": DEEPWIKI_URL, "name": "deepwiki"}],
    }
    r = await http.post(f"{API_BASE}/agents", headers=api_headers(api_key), json=body)
    if r.status_code >= 400:
        # Fallback: try without permission_policy in tools (older API shape)
        body["tools"] = [{"type": "mcp_toolset", "mcp_server_name": "deepwiki"}]
        r = await http.post(f"{API_BASE}/agents", headers=api_headers(api_key), json=body)
    r.raise_for_status()
    j = r.json()
    return j["id"], j.get("version", 1)


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


async def deny_tools(
    http: httpx.AsyncClient, api_key: str, session_id: str, tool_ids: list[str]
) -> tuple[float, int, str]:
    """POST deny confirmations for all tool_ids. Returns (mono_time, status, body_text)."""
    events = [
        {"type": "user.tool_confirmation", "tool_use_id": tid, "result": "deny"}
        for tid in tool_ids
    ]
    r = await http.post(
        f"{API_BASE}/sessions/{session_id}/events",
        headers=api_headers(api_key),
        json={"events": events},
    )
    t = time.monotonic()
    body_text = r.text[:1000]
    print(f"    [deny] POST {len(tool_ids)} denial(s) -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print(f"    [deny] error body: {body_text}")
    return t, r.status_code, body_text


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
            except Exception as e:  # noqa: BLE001
                print(f"  archive agent failed: {e}")
        try:
            r = await http.delete(
                f"{API_BASE}/environments/{env_id}",
                headers=api_headers(api_key, json_ct=False),
            )
            print(f"  delete env {env_id} -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"  delete env failed: {e}")


# -- Core experiment driver --------------------------------------------------


async def run_deny_all_experiment(
    api_key: str,
    agent_id: str,
    version: int,
    env_id: str,
    name: str,
    user_prompt: str,
    *,
    stream_timeout_after_deny: float = 60.0,
) -> ExperimentResult:
    result = ExperimentResult(name=name)

    async with httpx.AsyncClient(timeout=30) as http:
        result.session_id = await create_session(http, api_key, agent_id, version, env_id)
    print(f"  session={result.session_id}")

    url = f"{API_BASE}/sessions/{result.session_id}/events/stream"

    # Phase 1: Stream until requires_action idle, collecting tool use ids.
    print("  Phase 1: driving to requires_action...")
    phase = "pre_deny"
    seen_tool_uses: list[str] = []
    requires_action_found = False

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                print(f"  [stream] connected HTTP {es.response.status_code}")

                # Send the trigger message from a separate client
                async with httpx.AsyncClient(timeout=30) as msg_http:
                    await send_message(msg_http, api_key, result.session_id, user_prompt)
                    print(f"  [ctrl] user.message sent: {user_prompt!r}")

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
                        phase=phase,
                    )
                    result.events.append(ev)

                    # Print every event
                    if etype == "agent.message":
                        content = raw.get("content", [])
                        text_parts = [
                            b.get("text", "") for b in content if b.get("type") == "text"
                        ]
                        snippet = " | ".join(text_parts)[:120]
                        print(f"    [{phase}] agent.message: {snippet!r}")
                    elif etype == "agent.mcp_tool_use":
                        tid = raw.get("id", "")
                        tname = raw.get("name", "?")
                        tinput = json.dumps(raw.get("input", {}))[:80]
                        print(f"    [{phase}] mcp_tool_use id={eid[:24]} name={tname} input={tinput}")
                        seen_tool_uses.append(eid)
                    elif etype == "agent.mcp_tool_result":
                        print(f"    [{phase}] mcp_tool_result for tool_use_id={raw.get('tool_use_id', '?')[:24]}")
                    elif etype == "session.status_idle":
                        stop = raw.get("stop_reason", {})
                        print(f"    [{phase}] session.status_idle stop_reason={json.dumps(stop)}")

                        if stop.get("type") == "requires_action":
                            result.requires_action_stop = stop
                            # Collect pending ids: prefer stop_reason.event_ids, fallback to seen
                            pending = stop.get("event_ids") or [
                                tid for tid in seen_tool_uses
                                if tid not in {p for p in result.pending_tool_ids}
                            ]
                            result.pending_tool_ids = pending
                            requires_action_found = True
                            print(f"    [REQUIRES_ACTION] pending tool ids: {pending}")
                            break
                        else:
                            # Reached a terminal idle without requires_action
                            result.final_stop_reason = stop
                            print(f"    [TERMINAL_IDLE] type={stop.get('type')} — no requires_action reached")
                            return result
                    elif etype == "session.error":
                        err = raw.get("error", {})
                        print(f"    [{phase}] session.error: {json.dumps(err)}")
                        result.error = f"session.error: {json.dumps(err)}"
                        return result
                    else:
                        print(f"    [{phase}] {etype} id={eid[:24]}")

                    if len(result.events) > 200:
                        print("    [safety cap] too many events in phase 1")
                        break

    except Exception as e:  # noqa: BLE001
        import traceback
        result.error = f"{type(e).__name__}: {e}"
        print(f"  [ERROR phase 1] {type(e).__name__}: {e}")
        traceback.print_exc()
        return result

    if not requires_action_found:
        result.error = "requires_action idle never arrived"
        print(f"  [MISS] requires_action idle never arrived — probe inconclusive")
        return result

    # Phase 2: Deny all pending tools
    print(f"\n  Phase 2: denying all {len(result.pending_tool_ids)} tool(s)...")
    async with httpx.AsyncClient(timeout=30) as deny_http:
        deny_t, deny_status, deny_body = await deny_tools(
            deny_http, api_key, result.session_id, result.pending_tool_ids
        )
        result.deny_http_status = deny_status
        result.deny_response_body = deny_body
        result.deny_post_t = deny_t

    if deny_status >= 400:
        result.error = f"deny POST failed: HTTP {deny_status} {deny_body}"
        return result

    # Phase 3: Stream to next terminal idle, collecting everything that arrives
    print(f"\n  Phase 3: consuming stream after deny-all (timeout={stream_timeout_after_deny}s)...")
    phase = "post_deny"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, read=stream_timeout_after_deny, write=30.0, pool=10.0
            )
        ) as http:
            async with aconnect_sse(http, "GET", url, headers=sse_headers(api_key)) as es:
                print(f"  [stream2] connected HTTP {es.response.status_code}")
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
                        phase=phase,
                    )
                    result.events.append(ev)

                    if etype == "agent.message":
                        content = raw.get("content", [])
                        text_parts = [
                            b.get("text", "") for b in content if b.get("type") == "text"
                        ]
                        full_text = " ".join(text_parts)
                        snippet = full_text[:200]
                        result.agent_messages_after_deny.append(full_text)
                        print(f"    [{phase}] agent.message: {snippet!r}")
                    elif etype == "agent.mcp_tool_use":
                        tname = raw.get("name", "?")
                        tinput = json.dumps(raw.get("input", {}))[:80]
                        print(f"    [{phase}] mcp_tool_use id={eid[:24]} name={tname} input={tinput}")
                    elif etype == "agent.mcp_tool_result":
                        print(f"    [{phase}] mcp_tool_result for tool_use_id={raw.get('tool_use_id', '?')[:24]}")
                    elif etype == "session.status_idle":
                        stop = raw.get("stop_reason", {})
                        result.final_stop_reason = stop
                        result.final_idle_t = ev.t_mono
                        print(f"    [{phase}] session.status_idle stop_reason={json.dumps(stop)}")
                        break
                    elif etype == "session.error":
                        err = raw.get("error", {})
                        print(f"    [{phase}] session.error: {json.dumps(err)}")
                        result.error = f"session.error after deny: {json.dumps(err)}"
                        break
                    else:
                        print(f"    [{phase}] {etype} id={eid[:24]}")

                    if len([e for e in result.events if e.phase == "post_deny"]) > 200:
                        print("    [safety cap] too many events in phase 3")
                        break

    except httpx.ReadTimeout:
        print(f"  [stream2] ReadTimeout after {stream_timeout_after_deny}s — no more events arrived")
        result.error = f"ReadTimeout: no terminal idle within {stream_timeout_after_deny}s after deny"
    except Exception as e:  # noqa: BLE001
        import traceback
        result.error = f"{type(e).__name__}: {e}"
        print(f"  [ERROR phase 3] {type(e).__name__}: {e}")
        traceback.print_exc()

    return result


# -- Experiments -------------------------------------------------------------


async def exp_deny_single_tool(
    api_key: str, agent_id: str, version: int, env_id: str
) -> ExperimentResult:
    print("\n" + "=" * 60)
    print("EXP1: Deny-all on a single tool call")
    print("=" * 60)
    return await run_deny_all_experiment(
        api_key,
        agent_id,
        version,
        env_id,
        name="deny_single_tool",
        user_prompt=(
            "Use the read_wiki_structure tool to look up the repository 'facebook/react'. "
            "You must call the tool right now."
        ),
    )


async def exp_deny_parallel_tools(
    api_key: str, agent_id_parallel: str, version_parallel: int, env_id: str
) -> ExperimentResult:
    print("\n" + "=" * 60)
    print("EXP2: Deny-all on multiple parallel tool calls")
    print("=" * 60)
    return await run_deny_all_experiment(
        api_key,
        agent_id_parallel,
        version_parallel,
        env_id,
        name="deny_parallel_tools",
        user_prompt=(
            "Use read_wiki_structure twice in parallel: once for 'facebook/react' "
            "and once for 'vercel/next.js'. Call both tools right now in the same turn."
        ),
    )


# -- Analysis and summary ----------------------------------------------------


def analyze(result: ExperimentResult) -> None:
    print(f"\n--- {result.name} ---")
    if result.error:
        print(f"  ERROR: {result.error}")

    pre_events = [e for e in result.events if e.phase == "pre_deny"]
    post_events = [e for e in result.events if e.phase == "post_deny"]
    pre_types = [e.event_type for e in pre_events]
    post_types = [e.event_type for e in post_events]

    print(f"  requires_action stop_reason: {json.dumps(result.requires_action_stop)}")
    print(f"  pending tool ids: {result.pending_tool_ids}")
    print(f"  deny HTTP status: {result.deny_http_status}")
    print(f"  deny response body: {result.deny_response_body[:200]!r}")
    print(f"  pre-deny event types:  {pre_types}")
    print(f"  post-deny event types: {post_types}")
    print(f"  final stop_reason: {json.dumps(result.final_stop_reason)}")
    if result.latency_to_final_idle_ms is not None:
        print(f"  latency deny→final idle: {result.latency_to_final_idle_ms:.0f}ms")
    print(f"  agent messages after deny ({len(result.agent_messages_after_deny)}):")
    for i, msg in enumerate(result.agent_messages_after_deny, 1):
        print(f"    [{i}] {msg[:300]!r}")

    # Key verdict
    final_type = result.final_stop_reason.get("type", "UNKNOWN")
    post_tool_uses = [e for e in post_events if e.event_type == "agent.mcp_tool_use"]
    post_agent_msgs = [e for e in post_events if e.event_type == "agent.message"]

    print()
    if result.error:
        print(f"  VERDICT: inconclusive — {result.error}")
    elif post_agent_msgs and final_type == "end_turn":
        print(f"  VERDICT: agent responded with text after deny-all, then ended turn normally.")
        print(f"           {len(post_agent_msgs)} agent.message(s), stop_reason=end_turn.")
        print(f"           The turn continues — driver must consume this response.")
    elif not post_agent_msgs and final_type == "end_turn":
        print(f"  VERDICT: deny-all produced end_turn idle with NO agent.message text.")
        print(f"           Turn ends cleanly but silently.")
    elif post_tool_uses:
        print(f"  VERDICT: agent re-tried tools after deny-all ({len(post_tool_uses)} new tool uses).")
        print(f"           final stop_reason={final_type!r}")
    elif final_type == "requires_action":
        print(f"  VERDICT: session re-entered requires_action after deny-all.")
        print(f"           driver must handle deny → requires_action loop.")
    else:
        print(f"  VERDICT: final_stop_reason={final_type!r}, post_agent_msgs={len(post_agent_msgs)}")
        print(f"           Inspect post-deny events above for details.")


# -- Main --------------------------------------------------------------------


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DAIMON_ANTHROPIC__API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY not set")

    suffix = uuid.uuid4().hex[:8]
    agent_ids: list[str] = []
    env_id: str | None = None

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            print("Creating probe resources...")
            env_id = await create_env(http, api_key, suffix)
            agent_id, version = await create_agent(http, api_key, suffix)
            agent_id_parallel, version_parallel = await create_agent(
                http, api_key, f"{suffix}-par", parallel_tools=True
            )
            agent_ids = [agent_id, agent_id_parallel]
            print(f"  env={env_id}")
            print(f"  agent (single tool)={agent_id} v{version}")
            print(f"  agent (parallel tools)={agent_id_parallel} v{version_parallel}")

        r1 = await exp_deny_single_tool(api_key, agent_id, version, env_id)
        r2 = await exp_deny_parallel_tools(api_key, agent_id_parallel, version_parallel, env_id)

    finally:
        if env_id:
            await cleanup(api_key, agent_ids, env_id)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    analyze(r1)
    analyze(r2)

    # JSON dump for forensic analysis
    dump: dict = {}
    for r in [r1, r2]:
        dump[r.name] = {
            "session_id": r.session_id,
            "requires_action_stop": r.requires_action_stop,
            "pending_tool_ids": r.pending_tool_ids,
            "deny_http_status": r.deny_http_status,
            "deny_response_body": r.deny_response_body,
            "final_stop_reason": r.final_stop_reason,
            "latency_to_final_idle_ms": r.latency_to_final_idle_ms,
            "agent_messages_after_deny": r.agent_messages_after_deny,
            "pre_deny_event_types": [e.event_type for e in r.events if e.phase == "pre_deny"],
            "post_deny_event_types": [e.event_type for e in r.events if e.phase == "post_deny"],
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

    dump_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deny_all_tools_results.json")
    with open(dump_path, "w") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"\nFull event dump written to {dump_path}")

    # Key findings
    print("\n" + "=" * 60)
    print("KEY FINDINGS")
    print("=" * 60)
    for r in [r1, r2]:
        final_type = r.final_stop_reason.get("type", "UNKNOWN")
        post_msgs = len(r.agent_messages_after_deny)
        post_tools = len([e for e in r.events if e.phase == "post_deny" and e.event_type == "agent.mcp_tool_use"])
        print(f"  {r.name}:")
        print(f"    deny HTTP status : {r.deny_http_status}")
        print(f"    final stop_reason: {final_type!r}")
        print(f"    agent text msgs  : {post_msgs}")
        print(f"    new tool calls   : {post_tools}")
        if r.agent_messages_after_deny:
            print(f"    first msg snippet: {r.agent_messages_after_deny[0][:120]!r}")
        if r.error:
            print(f"    ERROR: {r.error}")
        print()

    # Implications for daimon turn driver
    print("IMPLICATIONS FOR DAIMON TURN DRIVER:")
    r1_final = r1.final_stop_reason.get("type")
    if r1_final == "end_turn" and r1.agent_messages_after_deny:
        print("  - deny-all → agent produces text → end_turn: driver must render the post-deny text.")
        print("    The requires_action→deny→end_turn path is a full text turn, not silent.")
    elif r1_final == "end_turn" and not r1.agent_messages_after_deny:
        print("  - deny-all → end_turn with NO text: driver can treat as silent turn end.")
    elif r1_final == "requires_action":
        print("  - deny-all → requires_action again: potential deny loop. Driver needs a guard.")
    elif r1.error:
        print(f"  - EXP1 errored: {r1.error}")
    else:
        print(f"  - EXP1 final stop_reason={r1_final!r}: inspect results above.")


if __name__ == "__main__":
    asyncio.run(main())
