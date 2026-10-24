"""Write-path helpers for the /agent-setup panel (Slack adapter).

Ports the Discord agent_setup write + scope_default logic, swapping the
platform-specific runtime type and audit-display helper for their Slack
equivalents. All core saga/store calls are reused UNCHANGED. No cross-adapter
imports (import-linter contract).

GitHub OAuth platform-keying: the state row is keyed to
``platform="slack"`` with a string Slack user ID (e.g. ``"U123456"``). The
callback resolver routes via the ``platform`` column — this prevents
cross-platform state reuse.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core import agent_lifecycle
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_agent

if TYPE_CHECKING:
    pass

_log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def owner_repo_from_url(url: str) -> str:
    """Extract canonical ``owner/repo`` from a GitHub URL or short path.

    Must stay byte-identical to
    ``daimon.core.stores.agent_repo_binding._normalize_owner_repo`` — a probe
    run against a differently-canonicalized string would verify a different
    repo than the one the binding actually records.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


# ---------------------------------------------------------------------------
# Agent mutation wrappers (port of Discord write.py)
# ---------------------------------------------------------------------------


async def delete_agent(runtime: SlackRuntime, *, tenant_id: uuid.UUID, name: str) -> None:
    """Archive the MA agent matching ``name`` under the given tenant.

    Channel and workspace scope rows naming the agent are cleared as part of the
    delete, so turn resolution falls through the cascade instead of resolving to
    a deleted agent, and its agent rule is dropped with them.
    """
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise DaimonError(f"No agent named *{name}* found.")
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        # Server-side refusal, and on Slack the ONLY one: RosterEntry carries no
        # managed flag, so the panel offers Delete on a seeded agent exactly as
        # it does on a user agent, and the click branch checks only admin.
        # Archiving here would take the deployment's built-in agent and its
        # memory store with it.
        raise DaimonError(
            f"*{name}* is a built-in agent and cannot be deleted. "
            "Make a copy first, then delete the copy."
        )
    await archive_agent(runtime.anthropic, agent.id, scope=resource_scope(tenant_id=str(tenant_id)))
    await agent_lifecycle.archive_memory_store_best_effort(
        anthropic=runtime.anthropic,
        sessionmaker=runtime.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        log_context={"tenant_id": str(tenant_id), "agent_name": name, "ma_agent_id": agent.id},
    )
    # After the MA archive, never before: a failure here leaves an archived
    # agent with a stale rule and scope rows rather than a live agent with cleared ones.
    # Deliberately unguarded — a failure must reach the action's error boundary
    # rather than degrade silently.
    await agent_lifecycle.forget_archived_agent(
        runtime.sessionmaker, tenant_id=tenant_id, name=name, agent=agent
    )


async def load_agent_inline_pat(runtime: SlackRuntime, *, agent_id: uuid.UUID) -> str | None:
    return None


async def store_inline_pat(
    runtime: SlackRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    raise ValueError("GitHub repository access requires a GitHub App grant.")
