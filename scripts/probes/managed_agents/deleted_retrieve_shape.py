"""Probe: hard-DELETE on environment, then retrieve.

Agents have no DELETE endpoint (archive-only). Environments support DELETE.
This probe answers: after `environments.delete(id)`, does `retrieve(id)`
return 404, 410, or something else? Used to size resolver error handling.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv


PROBE_TAG = "phase-38-deleted-retrieve"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    suffix = uuid.uuid4().hex[:8]
    env_name = f"phase38-del-env-{suffix}"

    env_id: str | None = None
    try:
        print("=== 1. Create environment ===")
        env = await client.beta.environments.create(
            name=env_name,
            config={"type": "cloud"},
            metadata={"daimon_probe": PROBE_TAG, "daimon_name": env_name},
        )
        env_id = env.id
        print(f"  env_id={env_id}")

        print("\n=== 2. DELETE environment ===")
        try:
            await client.beta.environments.delete(env_id)
            print("  deleted OK")
        except APIStatusError as err:
            print(f"  delete returned {err.status_code} {getattr(err, 'type', None)!r}")

        print("\n=== 3. Retrieve deleted environment ===")
        try:
            r = await client.beta.environments.retrieve(env_id)
            print(f"  RETURNED 200 -- id={r.id}, archived_at={getattr(r, 'archived_at', '<absent>')!r}")
        except APIStatusError as err:
            print(f"  status_code={err.status_code} type={getattr(err, 'type', None)!r}")
            try:
                print(f"  body={err.response.json()}")
            except Exception:
                print(f"  body(text)={err.response.text[:300]}")

        print("\n=== 4. environments.list(include_archived=True) — does deleted env appear? ===")
        seen = False
        async for e in client.beta.environments.list(include_archived=True):
            if e.id == env_id:
                seen = True
                break
        print(f"  deleted env in list(include_archived=True)? {seen}")

        env_id = None  # already gone, skip cleanup

    finally:
        if env_id:
            try:
                await client.beta.environments.archive(env_id)
            except APIStatusError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
