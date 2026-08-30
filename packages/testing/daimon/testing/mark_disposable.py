"""Mark the MA workspace behind DAIMON_TEST_ANTHROPIC_API_KEY as disposable.

    uv run python -m daimon.testing.mark_disposable          # summary only
    uv run python -m daimon.testing.mark_disposable --yes    # mark it

Prints what is currently in the workspace first, because the only thing this
command guards against is being pointed at the wrong one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from anthropic import AsyncAnthropic
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.testing.workspace_sentinel import mark_workspace_disposable


async def _summarize_and_mark(*, api_key: str, confirmed: bool) -> int:
    client = AsyncAnthropic(api_key=api_key)

    agent_count = 0
    tenant_ids: set[str] = set()
    async for agent in client.beta.agents.list(limit=100):
        agent_count += 1
        tenant_id = agent.metadata.get(MA_METADATA_KEY_TENANT)
        if tenant_id is not None:
            tenant_ids.add(tenant_id)

    print(f"agents in this workspace: {agent_count}")
    print(f"distinct {MA_METADATA_KEY_TENANT} values: {len(tenant_ids)}")
    for tenant_id in sorted(tenant_ids):
        print(f"  {tenant_id}")

    if not confirmed:
        print(
            "\nMarking this workspace disposable lets the contract test suite DELETE "
            "every skill, environment and agent in it. If the summary above looks like "
            "a workspace anyone depends on, stop. Otherwise re-run with --yes."
        )
        return 1

    sentinel_id = await mark_workspace_disposable(client)
    print(f"\nmarked disposable; sentinel agent: {sentinel_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m daimon.testing.mark_disposable",
        description="Mark the MA workspace behind DAIMON_TEST_ANTHROPIC_API_KEY as disposable.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm; without it the command only prints the workspace summary",
    )
    args = parser.parse_args()
    confirmed: bool = args.yes

    api_key = os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "DAIMON_TEST_ANTHROPIC_API_KEY is not set — export the key for the "
            "workspace you want to mark, then re-run.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_summarize_and_mark(api_key=api_key, confirmed=confirmed))


if __name__ == "__main__":
    raise SystemExit(main())
