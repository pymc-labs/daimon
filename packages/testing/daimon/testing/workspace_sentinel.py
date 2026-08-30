"""Mark an MA workspace as disposable so the test-only workspace nuke may run.

The marker is a single agent carrying `daimon_workspace=disposable` metadata;
`daimon.core.ma.delete_entire_workspace_for_testing` refuses to delete anything
in a workspace that has none.
"""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_WORKSPACE,
    MA_METADATA_VALUE_WORKSPACE_DISPOSABLE,
)
from daimon.core.ma import (
    WORKSPACE_SENTINEL_AGENT_NAME,
    find_workspace_disposable_sentinel,
)


async def mark_workspace_disposable(client: AsyncAnthropic) -> str:
    """Return the id of this workspace's disposable sentinel, creating it if absent.

    Idempotent: an existing sentinel is reused, so repeat calls never accumulate
    marker agents. The agent is never run — it exists only to carry the metadata.
    """
    existing = await find_workspace_disposable_sentinel(client)
    if existing is not None:
        return existing.id
    created = await client.beta.agents.create(
        model="claude-haiku-4-5",
        name=WORKSPACE_SENTINEL_AGENT_NAME,
        description=(
            "Marks this workspace as disposable: daimon's contract-test cleanup "
            "may delete every skill, environment and agent here. Delete this agent "
            "to withdraw that permission."
        ),
        metadata={MA_METADATA_KEY_WORKSPACE: MA_METADATA_VALUE_WORKSPACE_DISPOSABLE},
    )
    return created.id


_NOT_DISPOSABLE_BANNER = """
================================================================================
THIS SUITE DESTROYS THE ENTIRE MANAGED AGENTS WORKSPACE.

Every skill, environment and agent reachable from DAIMON_TEST_ANTHROPIC_API_KEY
is deleted before and after each test module. Only ever point that key at a
disposable dev workspace — never at one any real install depends on.

The workspace behind the current key is NOT marked disposable, so nothing was
touched. If it genuinely is a throwaway workspace, mark it with:

    uv run python -m daimon.testing.mark_disposable --yes
================================================================================
"""


async def require_disposable_workspace(client: AsyncAnthropic) -> None:
    """Fail the run unless this MA workspace is marked disposable.

    Every contract-test conftest whose cleanup calls
    `delete_entire_workspace_for_testing` must call this before its first nuke.
    It deliberately does NOT create the sentinel: auto-marking would make the
    guard wave through exactly the mistake it exists to catch.

    Fails rather than skips, unlike `_require_api_key` — a suite aimed at the
    wrong workspace must be loud, not quietly green.
    """
    if await find_workspace_disposable_sentinel(client) is None:
        pytest.fail(_NOT_DISPOSABLE_BANNER, pytrace=False)
