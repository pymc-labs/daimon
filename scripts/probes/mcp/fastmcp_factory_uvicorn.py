"""Probe: boot the FastMCP ASGI factory under real uvicorn, hit /healthz + tool call.

Question: does `uvicorn ... --factory` accept our `create_app` shape? Does the
lifespan boot correctly (so the StreamableHTTPSessionManager task group is
initialized without asgi-lifespan)?

Run:
    uv run --with fastmcp --with pyjwt --with uvicorn python scripts/probes/mcp/fastmcp_factory_uvicorn.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    env = {**os.environ, "PYTHONPATH": here}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "_factory:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8771",
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=here,
    )
    try:
        # Wait for /healthz.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient() as h:
                    r = await h.get("http://127.0.0.1:8771/healthz", timeout=0.5)
                    if r.status_code == 200:
                        break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            print("healthz never came up")
            return

        print("[healthz] 200 OK")

        # Auth reject.
        async with httpx.AsyncClient() as h:
            r = await h.post("http://127.0.0.1:8771/mcp", json={})
            print(f"[no auth] status={r.status_code} body={r.text[:80]}")

        # Authed MCP call.
        transport = StreamableHttpTransport(
            url="http://127.0.0.1:8771/mcp",
            headers={"Authorization": "Bearer probe-token-cli:testuser"},
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            print(f"[authed] tools: {[t.name for t in tools]}")
            result = await client.call_tool("whoami", {})
            print(f"[authed] whoami: {result.structured_content}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
