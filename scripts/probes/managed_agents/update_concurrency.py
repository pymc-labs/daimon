"""Probe: how does MA handle concurrent updates on agents and environments?

Agent.update has a required `version: int` parameter — suggests optimistic
concurrency. Env.update has no version parameter — suggests last-write-wins.

This probe verifies both:
  1. Agent: create -> note version -> update with stale version, expect failure.
     Also: two concurrent updates with the same version, expect exactly one
     winner.
  2. Environment: two concurrent updates with different configs, expect both
     to succeed (last-write-wins), record which won.

Cleanup: archive agent, delete env.

Observed conflict error shape (run 2026-06-11):
  - HTTP status code: 409
  - SDK exception class: anthropic.ConflictError (subclass of APIStatusError)
  - Error body type: "invalid_request_error"
  - Error message: "Concurrent modification detected. Please fetch the latest
    version and retry."

This shape is pinned by self-check assertions below. If MA changes the error
shape (status code or exception class), re-run this probe and update both the
docstring and the assertions.

Run:
    uv run python scripts/probes/managed_agents/update_concurrency.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from anthropic import AsyncAnthropic, ConflictError
from dotenv import load_dotenv


async def probe_agent_version(client: AsyncAnthropic) -> None:
    print("\n== Agent optimistic concurrency ==")
    suffix = uuid.uuid4().hex[:8]
    agent = await client.beta.agents.create(
        name=f"probe-upd-{suffix}",
        model={"id": "claude-haiku-4-5", "speed": "standard"},
        system="probe",
        skills=[],
        tools=[],
        mcp_servers=[],
    )
    print(f"  created agent={agent.id} version={agent.version}")

    stale_conflict: ConflictError | None = None

    try:
        # 1. Update with correct version — should succeed and bump version.
        updated = await client.beta.agents.update(
            agent.id, version=agent.version, system="probe v2"
        )
        print(f"  update(version={agent.version}) -> OK version={updated.version}")

        # 2. Update with stale (original) version — expect 409 ConflictError.
        try:
            r = await client.beta.agents.update(
                agent.id, version=agent.version, system="probe v3-stale"
            )
            print(f"  update(stale version={agent.version}) -> UNEXPECTED OK version={r.version}")
        except Exception as e:
            print(f"  update(stale version={agent.version}) -> {type(e).__name__}: {e}")
            if isinstance(e, ConflictError):
                stale_conflict = e

        # 3. Two concurrent updates with current version — expect one winner.
        current = updated.version
        results = await asyncio.gather(
            client.beta.agents.update(agent.id, version=current, system="race-A"),
            client.beta.agents.update(agent.id, version=current, system="race-B"),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            label = ("A", "B")[i]
            if isinstance(r, Exception):
                print(f"  race[{label}] -> {type(r).__name__}: {r}")
            else:
                print(f"  race[{label}] -> OK version={r.version} system={r.system!r}")
    finally:
        await client.beta.agents.archive(agent.id)
        print(f"  archived {agent.id}")

    # Self-check: pin the observed conflict error shape.
    # If these assertions fail, MA has changed the error contract — update the docstring above.
    assert stale_conflict is not None, (
        "stale-version update must produce a ConflictError (409); got no exception"
    )
    assert isinstance(stale_conflict, ConflictError), (
        f"expected anthropic.ConflictError, got {type(stale_conflict).__name__}"
    )
    assert stale_conflict.status_code == 409, (
        f"expected HTTP 409, got {stale_conflict.status_code}"
    )
    assert "Concurrent modification" in str(stale_conflict), (
        f"expected 'Concurrent modification' in error message, got: {stale_conflict}"
    )
    print("\n  [SELF-CHECK PASSED] conflict shape: ConflictError (HTTP 409)")


async def probe_env_update(client: AsyncAnthropic) -> None:
    print("\n== Environment update concurrency ==")
    suffix = uuid.uuid4().hex[:8]
    env = await client.beta.environments.create(
        name=f"probe-envupd-{suffix}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"  created env={env.id}")

    try:
        # Two concurrent updates with different descriptions — no version kwarg available.
        results = await asyncio.gather(
            client.beta.environments.update(env.id, description="race-A"),
            client.beta.environments.update(env.id, description="race-B"),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            label = ("A", "B")[i]
            if isinstance(r, Exception):
                print(f"  race[{label}] -> {type(r).__name__}: {r}")
            else:
                print(f"  race[{label}] -> OK description={r.description!r}")

        final = await client.beta.environments.retrieve(env.id)
        print(f"  final description={final.description!r}")
    finally:
        await client.beta.environments.delete(env.id)
        print(f"  deleted {env.id}")


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DAIMON_ANTHROPIC__API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)
    await probe_agent_version(client)
    await probe_env_update(client)


if __name__ == "__main__":
    asyncio.run(main())
