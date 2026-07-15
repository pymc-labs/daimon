"""Probe: what does stop_reason on a requires_action idle actually contain?

Goals:
  1. Dump stop_reason verbatim for a requires_action idle.
  2. Check whether it lists event_ids (our parser assumes this).
  3. Force parallel tool use to see if multiple ids show up in one idle.
  4. After approving one, confirm the *next* idle's event_ids reflects only
     still-blocked tool uses (not previously-approved ones).

Run: uv run python scripts/probe_stop_reason.py
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from dotenv import load_dotenv
from httpx_sse import aconnect_sse

load_dotenv()

API_KEY = os.environ["ANTHROPIC_API_KEY"]
API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"
HEADERS = {
    "x-api-key": API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": BETA,
    "content-type": "application/json",
}
DEEPWIKI_URL = "https://mcp.deepwiki.com/mcp"


async def boot(c: httpx.AsyncClient) -> tuple[str, str, int]:
    re = await c.post(
        f"{API_BASE}/environments",
        headers=HEADERS,
        json={
            "name": "probe-stopreason-env",
            "config": {"type": "cloud", "networking": {"type": "unrestricted"}},
        },
    )
    re.raise_for_status()
    env_id = re.json()["id"]
    ra = await c.post(
        f"{API_BASE}/agents",
        headers=HEADERS,
        json={
            "name": "probe-stopreason-agent",
            "model": {"id": "claude-opus-4-7", "speed": "standard"},
            # Ask for TWO tool calls up front to try to force parallel tool use.
            "system": (
                "You must call read_wiki_structure TWICE in your FIRST response: "
                "once for repoName='facebook/react' and once for repoName='vercel/next.js'. "
                "Emit both tool_use blocks in the same assistant turn."
            ),
            "skills": [],
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "deepwiki"}],
            "mcp_servers": [{"type": "url", "url": DEEPWIKI_URL, "name": "deepwiki"}],
        },
    )
    ra.raise_for_status()
    j = ra.json()
    return env_id, j["id"], j.get("version", 1)


async def drive(c: httpx.AsyncClient, sid: str) -> None:
    h = {k: v for k, v in HEADERS.items() if k != "content-type"}

    async def post_user_msg():
        await c.post(
            f"{API_BASE}/sessions/{sid}/events",
            headers=HEADERS,
            json={
                "events": [{"type": "user.message", "content": [{"type": "text", "text": "Go."}]}]
            },
        )

    seen_tool_uses: list[str] = []
    confirmed: set[str] = set()
    idles_seen = 0

    async with aconnect_sse(c, "GET", f"{API_BASE}/sessions/{sid}/events/stream", headers=h) as es:
        asyncio.create_task(post_user_msg())
        async for sse in es.aiter_sse():
            raw = json.loads(sse.data)
            t = raw.get("type")
            if t == "agent.mcp_tool_use":
                tid = raw.get("id")
                seen_tool_uses.append(tid)
                print(f"mcp_tool_use id={tid} name={raw.get('name')}")
            elif t == "session.status_idle":
                idles_seen += 1
                stop = raw.get("stop_reason", {})
                print(f"\n=== IDLE #{idles_seen} ===")
                print(f"stop_reason (verbatim): {json.dumps(stop, indent=2)}")
                print(f"seen tool_uses so far: {seen_tool_uses}")
                print(f"already confirmed: {sorted(confirmed)}")

                if stop.get("type") != "requires_action":
                    print(f"idle is terminal (type={stop.get('type')}), exiting")
                    break

                # Determine pending ids — try event_ids first, else fall back.
                pending = stop.get("event_ids") or [
                    tid for tid in seen_tool_uses if tid not in confirmed
                ]
                print(f"pending to confirm this turn: {pending}")

                # Batch: approve all pending in one POST.
                if pending:
                    events = [
                        {"type": "user.tool_confirmation", "tool_use_id": tid, "result": "allow"}
                        for tid in pending
                    ]
                    r = await c.post(
                        f"{API_BASE}/sessions/{sid}/events",
                        headers=HEADERS,
                        json={"events": events},
                    )
                    print(f"batch-approve {pending} -> {r.status_code} {r.text[:400]}")
                    if r.status_code < 300:
                        for tid in pending:
                            confirmed.add(tid)
                    else:
                        print("batch confirmation POST failed, bailing")
                        break
                else:
                    print("no pending ids — unexpected, bailing")
                    break

                if idles_seen >= 6:
                    print("safety cap hit")
                    break
            elif t == "agent.mcp_tool_result":
                print(f"mcp_tool_result for {raw.get('tool_use_id')}")
            elif t == "agent.message":
                txt = "".join(
                    b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text"
                )
                print(f"agent.message: {txt[:160]!r}")
            elif t == "session.error":
                print(f"session.error: {raw.get('error')}")
                break


async def main() -> None:
    async with httpx.AsyncClient(timeout=180) as c:
        env_id, agent_id, version = await boot(c)
        print(f"env={env_id} agent={agent_id} v{version}")
        rs = await c.post(
            f"{API_BASE}/sessions",
            headers=HEADERS,
            json={
                "agent": {"type": "agent", "id": agent_id, "version": version},
                "environment_id": env_id,
                "resources": [],
                "vault_ids": [],
            },
        )
        rs.raise_for_status()
        sid = rs.json()["id"]
        print(f"session={sid}\n")
        await drive(c, sid)


if __name__ == "__main__":
    asyncio.run(main())
