"""Mark an MA workspace as disposable so the test-only workspace nuke may run.

The marker is a single agent carrying `daimon_workspace=disposable` metadata;
`daimon.core.ma.delete_entire_workspace_for_testing` refuses to delete anything
in a workspace that has none.
"""

from __future__ import annotations

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
