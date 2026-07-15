"""Probe (spike 035): live MA agent moves large files sandbox→host via curl.

The make-or-break test for the publish_blog payload-limit fix. Spike 033 proved the
operator can't pull agent files (Approach 2 dead); 034 proved the host-side capability
upload endpoint. This proves the LIVE agent leg: given only a `PUT /upload/{token}` URL
(simulating the future MCP tool's return), a real MA agent moves files OFF its sandbox
byte-exact — the exact payloads the inline tool-arg channel could not carry:

  (a) a 55 KB EXISTING notebook — fetched into the sandbox, then curled up. The bytes
      never pass through the model token stream (the truncation case, eliminated).
  (b) a ~1 MB binary — produced in-sandbox by code, then curled up. Impossible to
      inline as base64; here it moves in one curl.

Requires a running spike-035 receiver reachable at SPIKE035_BASE_URL (a cloudflared
tunnel to receiver.py). The receiver records received bytes + sha256, so byte-exactness
is asserted SERVER-SIDE — we never trust the agent's self-report.

Run (orchestrated):
  1. SPIKE035_SECRET=... uv run --with fastapi --with uvicorn --with httpx python \
       .planning/spikes/035-live-agent-curl-egress/receiver.py --port 8099   &
  2. cloudflared tunnel --url http://localhost:8099                          &
  3. SPIKE035_SECRET=... SPIKE035_BASE_URL=https://<tunnel> \
       DAIMON_ANTHROPIC__API_KEY=sk-ant-... uv run python \
       scripts/probes/managed_agents/live_agent_curl_egress.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params import (
    BetaManagedAgentsUserToolConfirmationEventParams,
)
from dotenv import load_dotenv

from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, extract_final_response

SECRET = os.environ.get("SPIKE035_SECRET", "spike-035-dev-secret")
BLOB = b"D035" * 262144  # 1,048,576 bytes
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint_token(*, slug: str, op: str, max_bytes: int, name: str | None, now: datetime) -> str:
    payload: dict[str, Any] = {
        "slug": slug, "op": op, "name": name, "max_bytes": max_bytes,
        "exp": int((now + timedelta(seconds=900)).timestamp()),
    }
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


async def main() -> None:
    load_dotenv()
    base = os.environ["SPIKE035_BASE_URL"].rstrip("/")
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    # Learn the fixture's true sha by fetching it ourselves (decoupled from receiver).
    async with httpx.AsyncClient(timeout=30) as hc:
        fx = await hc.get(f"{base}/fixture/notebook.py")
        fx.raise_for_status()
        fixture_sha = hashlib.sha256(fx.content).hexdigest()
        print(f"=== fixture: {len(fx.content)}B sha={fixture_sha[:16]}...  blob: {len(BLOB)}B sha={BLOB_SHA[:16]}... ===")

    now = datetime.now(UTC)
    src_slug, data_slug = f"existing-nb-{uuid.uuid4().hex[:6]}", f"big-data-{uuid.uuid4().hex[:6]}"
    src_url = f"{base}/upload/{mint_token(slug=src_slug, op='source', max_bytes=1_000_000, name=None, now=now)}"
    data_url = f"{base}/upload/{mint_token(slug=data_slug, op='data', max_bytes=2_000_000, name='posterior.nc', now=now)}"

    tag = uuid.uuid4().hex[:8]
    env_id: str | None = None
    agent_id: str | None = None
    verdict = "FAIL"
    try:
        print(f"\n=== create env + agent (probe-egress-live-{tag}) ===")
        environment = await client.beta.environments.create(
            name=f"probe-egress-live-env-{tag}",
            config={"type": "cloud", "networking": {"type": "unrestricted"}},
            metadata={"daimon_probe": "live_agent_curl_egress"},
        )
        env_id = environment.id
        agent = await client.beta.agents.create(
            name=f"probe-egress-live-agent-{tag}",
            model="claude-sonnet-4-6",
            system="You run the exact bash commands given and report each command's raw stdout.",
            tools=[{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
            metadata={"daimon_probe": "live_agent_curl_egress"},
        )
        agent_id = agent.id
        session = await client.beta.sessions.create(agent=agent_id, environment_id=env_id)
        print(f"  env={env_id} agent={agent_id} session={session.id}")

        trigger = (
            "Run these four bash commands in order and report each one's raw stdout on a "
            "labelled line. Do NOT print file contents.\n"
            f"1. FETCH: `curl -sS '{base}/fixture/notebook.py' -o /tmp/nb.py && wc -c < /tmp/nb.py`\n"
            f"2. UP_SRC: `curl -sS -X PUT --data-binary @/tmp/nb.py '{src_url}'`\n"
            "3. GEN: `python3 -c \"open('/tmp/blob.bin','wb').write(b'D035'*262144)\" && wc -c < /tmp/blob.bin`\n"
            f"4. UP_DATA: `curl -sS -X PUT --data-binary @/tmp/blob.bin '{data_url}'`\n"
            "Reply with four lines: FETCH=<o1> UP_SRC=<o2> GEN=<o3> UP_DATA=<o4>"
        )
        user_message: BetaManagedAgentsUserMessageEventParams = {
            "type": "user.message", "content": [{"type": "text", "text": trigger}],
        }
        await client.beta.sessions.events.send(session.id, events=[user_message])

        print("\n=== driving turn (agent fetches + curls; may take 60-120s) ===")
        state = TurnState()
        confirmed: set[str] = set()
        async for event in await client.beta.sessions.events.stream(session_id=session.id):
            state = apply(state, event)
            if isinstance(event, BetaManagedAgentsSessionErrorEvent):
                raise RuntimeError(f"session.error: {getattr(event.error, 'message', event.error)}")
            if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
                if event.stop_reason.type == "requires_action":
                    fresh = [t for t in event.stop_reason.event_ids if t not in confirmed]
                    if fresh:
                        confirmed.update(fresh)
                        decisions: list[BetaManagedAgentsUserToolConfirmationEventParams] = [
                            {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": t}
                            for t in fresh
                        ]
                        print(f"  auto-allowing {len(fresh)} tool call(s)")
                        await client.beta.sessions.events.send(session.id, events=decisions)
                    continue
                break

        print(f"\n  agent final: {extract_final_response(state.content)!r}")

        print("\n=== SERVER-SIDE self-check (GET /received) ===")
        async with httpx.AsyncClient(timeout=30) as hc:
            got = (await hc.get(f"{base}/received")).json()
        src = got.get(src_slug, {})
        data = got.get(data_slug, {})
        print(f"  source slug: {src}")
        print(f"  data slug:   {data}")
        src_ok = src.get("sha256") == fixture_sha
        data_ok = data.get("sha256") == BLOB_SHA
        print(f"  55KB source byte-exact at host: {src_ok}")
        print(f"  1MB data byte-exact at host:    {data_ok}")
        if src_ok and data_ok:
            verdict = "PASS"
            print("  → agent moved BOTH payloads off its sandbox byte-exact via curl")
    finally:
        print("\n=== cleanup ===")
        if agent_id is not None:
            try:
                await client.beta.agents.archive(agent_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  agent archive failed: {exc}")
        if env_id is not None:
            try:
                await client.beta.environments.delete(env_id)
            except Exception as exc:  # noqa: BLE001
                print(f"  env delete failed: {exc}")

    print(f"\n=== VERDICT === {verdict}: live agent curl egress (55KB source + 1MB data)")


if __name__ == "__main__":
    asyncio.run(main())
