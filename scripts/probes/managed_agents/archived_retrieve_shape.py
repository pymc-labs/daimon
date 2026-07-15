"""Probe: how does MA respond to retrieve() on an archived agent / environment?

Phase 38 needs to know the exception/status shape to write the resolver's
404-recovery logic. Specifically:

1. After client.beta.agents.archive(id), what does client.beta.agents.retrieve(id) do?
   - 404? 410? 200 with status="archived"? Other?
2. Same for environments.
3. Does agents.list() include archived items by default? Does it honor
   include_archived=False vs True? Same for environments.

All created resources are tagged `daimon_probe = "phase-38-archived-retrieve"`
and `daimon_name` set to a uuid-suffixed string for traceability and cleanup.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv


PROBE_TAG = "phase-38-archived-retrieve"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    suffix = uuid.uuid4().hex[:8]
    agent_name = f"phase38-agent-{suffix}"
    env_name = f"phase38-env-{suffix}"

    agent_id: str | None = None
    env_id: str | None = None

    try:
        # === 1. Create environment (needed by agent) ===
        print("=== 1. Create environment ===")
        env = await client.beta.environments.create(
            name=env_name,
            config={"type": "cloud"},
            metadata={"daimon_probe": PROBE_TAG, "daimon_name": env_name},
        )
        env_id = env.id
        print(f"  env_id={env_id}")

        # === 2. Create agent ===
        print("\n=== 2. Create agent ===")
        agent = await client.beta.agents.create(
            name=agent_name,
            model="claude-sonnet-4-5",
            metadata={"daimon_probe": PROBE_TAG, "daimon_name": agent_name},
        )
        agent_id = agent.id
        print(f"  agent_id={agent_id}")

        # === 3. Retrieve live (sanity) ===
        print("\n=== 3. Retrieve live agent (sanity) ===")
        live = await client.beta.agents.retrieve(agent_id)
        print(f"  status field present? {hasattr(live, 'status')}")
        if hasattr(live, "status"):
            print(f"  live.status = {live.status!r}")
        print(f"  archived_at present? {hasattr(live, 'archived_at')}")

        # === 4. List includes our agent (include_archived=False default) ===
        print("\n=== 4. agents.list(include_archived=False) before archive ===")
        seen_pre = False
        async for a in client.beta.agents.list(include_archived=False):
            if a.id == agent_id:
                seen_pre = True
                break
        print(f"  live agent appears in list(include_archived=False)? {seen_pre}")

        # === 5. Archive agent ===
        print("\n=== 5. Archive agent ===")
        await client.beta.agents.archive(agent_id)
        print("  archived OK")

        # === 6. Retrieve after archive — THE KEY QUESTION ===
        print("\n=== 6. Retrieve archived agent ===")
        try:
            r = await client.beta.agents.retrieve(agent_id)
            print(f"  RETURNED 200")
            print(f"  status field: {getattr(r, 'status', '<absent>')!r}")
            print(f"  archived_at:  {getattr(r, 'archived_at', '<absent>')!r}")
            print(f"  full dump keys: {sorted(r.model_dump().keys())}")
        except APIStatusError as err:
            print(f"  RAISED APIStatusError")
            print(f"  status_code={err.status_code}")
            print(f"  error.type ={getattr(err, 'type', None)!r}")
            try:
                body = err.response.json()
                print(f"  body={body}")
            except Exception:
                print(f"  body(text)={err.response.text[:300]}")
        except Exception as err:  # noqa: BLE001
            print(f"  RAISED {type(err).__name__}: {err!r}")

        # === 7. List include_archived=False after archive ===
        print("\n=== 7. agents.list(include_archived=False) after archive ===")
        seen_post_excl = False
        async for a in client.beta.agents.list(include_archived=False):
            if a.id == agent_id:
                seen_post_excl = True
                break
        print(f"  archived agent appears in list(include_archived=False)? {seen_post_excl}")

        # === 8. List include_archived=True after archive ===
        print("\n=== 8. agents.list(include_archived=True) after archive ===")
        seen_post_incl = False
        async for a in client.beta.agents.list(include_archived=True):
            if a.id == agent_id:
                seen_post_incl = True
                print(f"  found with status={getattr(a, 'status', '<absent>')!r}")
                break
        print(f"  archived agent appears in list(include_archived=True)? {seen_post_incl}")

        # === 9. Same flow for environment ===
        print("\n=== 9. Archive environment ===")
        await client.beta.environments.archive(env_id)
        print("  archived OK")

        print("\n=== 10. Retrieve archived environment ===")
        try:
            r = await client.beta.environments.retrieve(env_id)
            print(f"  RETURNED 200")
            print(f"  status field: {getattr(r, 'status', '<absent>')!r}")
            print(f"  archived_at:  {getattr(r, 'archived_at', '<absent>')!r}")
            print(f"  full dump keys: {sorted(r.model_dump().keys())}")
        except APIStatusError as err:
            print(f"  RAISED APIStatusError")
            print(f"  status_code={err.status_code}")
            print(f"  error.type ={getattr(err, 'type', None)!r}")
            try:
                body = err.response.json()
                print(f"  body={body}")
            except Exception:
                print(f"  body(text)={err.response.text[:300]}")

        print("\n=== 11. environments.list(include_archived=False) after archive ===")
        seen_env_excl = False
        async for e in client.beta.environments.list(include_archived=False):
            if e.id == env_id:
                seen_env_excl = True
                break
        print(f"  archived env in list(include_archived=False)? {seen_env_excl}")

        print("\n=== 12. environments.list(include_archived=True) after archive ===")
        seen_env_incl = False
        async for e in client.beta.environments.list(include_archived=True):
            if e.id == env_id:
                seen_env_incl = True
                break
        print(f"  archived env in list(include_archived=True)? {seen_env_incl}")

        # === 13. Retrieve a totally unknown id ===
        print("\n=== 13. Retrieve unknown agent id (control) ===")
        try:
            await client.beta.agents.retrieve("ag_01" + "x" * 24)
        except APIStatusError as err:
            print(f"  status_code={err.status_code}, type={getattr(err, 'type', None)!r}")

        print("\n=== VERDICT ===")
        print("  (read sections 6, 7, 8 above for agent; 10, 11, 12 for env)")

    finally:
        # Best-effort cleanup (already archived above; idempotent attempts)
        for label, oid, archive in (
            ("agent", agent_id, lambda i: client.beta.agents.archive(i)),
            ("env", env_id, lambda i: client.beta.environments.archive(i)),
        ):
            if oid:
                try:
                    await archive(oid)
                except APIStatusError as err:
                    if err.status_code not in (404, 409):
                        print(f"  [cleanup] {label} {oid}: {err.status_code} {err}")


if __name__ == "__main__":
    asyncio.run(main())
