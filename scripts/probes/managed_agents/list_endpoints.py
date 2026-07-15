"""Narrow probe: does the Managed Agents API have list/search endpoints?

Our ManagedAgentsClient doesn't wrap any `list_sessions` or `list_agents` —
but the API might support them. If it does, a bunch of spec §3 tool designs
get simpler (sessions.list / agents.list become API calls, not DB scans).

Probing:
  GET /v1/agents
  GET /v1/sessions
  GET /v1/sessions?agent_id=...
  GET /v1/skills     (client has list_skills; confirm shape)
  GET /v1/vaults
  (and anything search-shaped)
"""

from __future__ import annotations

import asyncio
import os

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA_AGENTS = "managed-agents-2026-04-01"
BETA_SKILLS = "skills-2025-10-02"


async def probe(
    client: httpx.AsyncClient, method: str, path: str, beta: str = BETA_AGENTS, **kwargs
) -> None:
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta,
        "content-type": "application/json",
    }
    url = f"{API_BASE}{path}"
    print(f"\n{method} {url}")
    try:
        r = await client.request(method, url, headers=headers, **kwargs)
        print(f"  {r.status_code}")
        body = r.text
        if len(body) > 500:
            body = body[:500] + f"... (truncated, full len={len(r.text)})"
        print(f"  body: {body}")
        if r.status_code < 300:
            try:
                j = r.json()
                if isinstance(j, dict):
                    print(f"  keys: {list(j.keys())}")
            except Exception:
                pass
    except Exception as e:
        print(f"  EXCEPTION {type(e).__name__}: {e}")


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30) as http:
        # Agent listing
        await probe(http, "GET", "/agents")
        await probe(http, "GET", "/agents?limit=5")

        # Session listing
        await probe(http, "GET", "/sessions")
        await probe(http, "GET", "/sessions?limit=5")

        # Skills
        await probe(http, "GET", "/skills", beta=BETA_SKILLS)

        # Vaults
        await probe(http, "GET", "/vaults")

        # Envs / environments
        await probe(http, "GET", "/environments")

        # Search-shaped
        await probe(http, "GET", "/sessions/search?query=hello")
        await probe(http, "GET", "/events/search?query=hello")


if __name__ == "__main__":
    asyncio.run(main())
