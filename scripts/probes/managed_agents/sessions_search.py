"""Does /v1/sessions/search accept some other param name?

We got 400 "unexpected query parameter: query" — which means the route
exists and the 400 is argument-shaped, not route-shaped. Try alternatives.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"


async def probe(http: httpx.AsyncClient, method: str, path: str, **kwargs) -> None:
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }
    url = f"{API_BASE}{path}"
    print(f"\n{method} {url} {kwargs.get('params', kwargs.get('json', ''))}")
    r = await http.request(method, url, headers=headers, **kwargs)
    print(f"  {r.status_code}: {r.text[:300]}")


async def main() -> None:
    load_dotenv()
    async with httpx.AsyncClient(timeout=30) as http:
        # Try each plausible param name with GET
        for param in ("q", "search", "text", "content", "keyword", "filter"):
            await probe(http, "GET", "/sessions/search", params={param: "hello"})

        # Maybe POST?
        await probe(http, "POST", "/sessions/search", json={"query": "hello"})
        await probe(http, "POST", "/sessions/search", json={"q": "hello"})
        await probe(http, "POST", "/sessions/search", json={})

        # Bare GET — maybe the "unexpected query parameter" rejection is the only hint
        await probe(http, "GET", "/sessions/search")


if __name__ == "__main__":
    asyncio.run(main())
