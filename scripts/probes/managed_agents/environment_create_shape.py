"""Probe: MA environments.retrieve ↔ environments.create shape drift.

Motivation: `daimon environments fork` was observed to fail with

    400 invalid_request_error: config.init_script: Extra inputs are not permitted

because it round-trips the retrieved config straight back into create:

    config=cast(..., source_ma.config.model_dump(mode="json"))

This probe characterizes the exact drift:
  1. Creates a minimal env.
  2. Retrieves it.
  3. Diffs: fields in the retrieved config that the create endpoint rejects.
  4. Attempts the exact failing call (round-trip) to confirm repro.
  5. Attempts a projected call (only `type`) to confirm the fix shape works.
  6. Prints a VERDICT line.

Cleanup: archives both created envs in a finally block.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("DAIMON_ANTHROPIC__API_KEY") or os.environ["ANTHROPIC_API_KEY"]
    client = AsyncAnthropic(api_key=api_key)

    tag = uuid.uuid4().hex[:8]
    src_name = f"probe-envshape-src-{tag}"
    forked_name = f"probe-envshape-fork-{tag}"
    src_id: str | None = None
    fork_id: str | None = None

    try:
        print("=== 1. Create source env (minimal shape) ===")
        created = await client.beta.environments.create(
            name=src_name,
            config={"type": "cloud"},
            metadata={"daimon_probe": "env_create_shape"},
        )
        src_id = created.id
        print(f"  id={src_id}")
        print(f"  create→config keys: {sorted(created.config.model_dump(mode='json').keys())}")

        print("\n=== 2. Retrieve source env ===")
        retrieved = await client.beta.environments.retrieve(src_id)
        retrieved_config = retrieved.config.model_dump(mode="json")
        print(f"  retrieve→config keys: {sorted(retrieved_config.keys())}")
        print(f"  retrieve→config (pretty):\n{json.dumps(retrieved_config, indent=2)}")

        print("\n=== 3. Round-trip create (reproduces `environments fork` bug) ===")
        try:
            _ = await client.beta.environments.create(
                name=forked_name,
                config=retrieved_config,  # type: ignore[arg-type]
                metadata={"daimon_probe": "env_create_shape_roundtrip"},
            )
            print("  UNEXPECTED: round-trip create succeeded — bug may be fixed upstream")
        except APIStatusError as err:
            print(f"  status={err.status_code}")
            print(f"  body={err.response.text}")

        print("\n=== 4. Field-by-field: which retrieved keys does create reject? ===")
        rejected: list[tuple[str, str]] = []
        accepted: list[str] = []
        for key, value in retrieved_config.items():
            if key == "type":
                continue
            trial_name = f"probe-envshape-{tag}-{key[:20]}"
            try:
                made = await client.beta.environments.create(
                    name=trial_name,
                    config={"type": "cloud", key: value},  # type: ignore[misc]
                    metadata={"daimon_probe": f"env_create_shape_field_{key}"},
                )
                accepted.append(key)
                await client.beta.environments.archive(made.id)
            except APIStatusError as err:
                msg = err.response.json().get("error", {}).get("message", "")
                rejected.append((key, msg))

        print(f"  accepted-by-create keys: {accepted}")
        print("  rejected-by-create keys:")
        for key, msg in rejected:
            print(f"    {key!r}: {msg}")

        print("\n=== 5. Projected create (only `type`) — the fix shape ===")
        fork_made = await client.beta.environments.create(
            name=forked_name,
            config={"type": retrieved_config["type"]},  # type: ignore[misc]
            metadata={"daimon_probe": "env_create_shape_projected"},
        )
        fork_id = fork_made.id
        print(f"  id={fork_id} — OK")

        print("\n=== VERDICT ===")
        rejected_keys = [k for k, _ in rejected]
        if rejected_keys:
            print(
                "  BUG CONFIRMED: `environments.retrieve(...).config.model_dump()` "
                "includes fields that `environments.create` rejects: "
                f"{rejected_keys}"
            )
            print(
                "  FIX: in packages/adapters/cli/daimon/adapters/cli/commands/"
                "environments.py::_environments_fork_entry, project the retrieved "
                "config to the create-shape (start with `{'type': ...}`) rather "
                "than forwarding `source_ma.config.model_dump(mode='json')` whole."
            )
        else:
            print("  No rejected keys — either MA accepts everything, or this probe is stale.")

    finally:
        for oid in (src_id, fork_id):
            if oid:
                try:
                    await client.beta.environments.archive(oid)
                except Exception as e:  # noqa: BLE001
                    print(f"  [cleanup] archive {oid} failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
