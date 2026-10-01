"""Read-path helpers for the /agent-setup panel.

Shell module: performs real I/O (DB reads + MA agent list). Mirrors the
shape of routines_panel/read.py — async functions taking session + anthropic +
keyword tenant_id, returning view-model tuples. No try/except (propagate to
the actions.py/submit.py boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from anthropic import AsyncAnthropic
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, load_agent_details
from daimon.core.answering_map import AnsweringMap, load_answering_map
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.roster import Roster, load_roster
from daimon.core.scope import (
    DeploymentDefault,
)
from daimon.core.stores.identity import get_slack_principal_for_account
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "coding_tools_available",
    "github_facts",
    "load_panel_answering_map",
    "load_panel_details",
    "load_panel_roster",
    "public_mcp_url",
    "resolve_attributions",
]


def github_facts(runtime: SlackRuntime) -> GitHubDeploymentFacts:
    """Read this deployment's two GitHub facts off its settings.

    The core assembler never touches `Settings` — it cannot resolve a token,
    by construction — so the adapter answers "is there an operator fallback
    token" and "is a GitHub App configured" and hands both over as plain
    booleans. The App counts as configured only with both halves of its
    identity present; an app id without a private key mints nothing.
    """
    github = runtime.settings.github
    return GitHubDeploymentFacts(
        has_fallback_pat=github.fallback_pat is not None,
        app_configured=github.app_id is not None and github.app_private_key is not None,
    )


def public_mcp_url(runtime: SlackRuntime) -> str | None:
    """This deployment's own MCP endpoint, or None when it runs without one."""
    url = runtime.settings.mcp.public_url
    return str(url) if url is not None else None


def coding_tools_available(runtime: SlackRuntime) -> bool:
    """Whether a coding-tool token could actually be minted and used.

    Both halves are needed: the URL the person's client connects to, and the
    secret the token is signed with. With either missing the panel says so
    instead of offering a button that cannot produce a working connection.
    """
    return public_mcp_url(runtime) is not None and runtime.settings.mcp.jwt_secret is not None


async def load_panel_roster(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    thread_id: str | None,
    default: DeploymentDefault,
) -> Roster:
    """The tenant's agents, answering-here first, for the Agents view.

    `channel_id` is None only where the caller has no channel — a DM, or a
    payload that carried none. The roster still lists every agent; nothing is
    marked as answering here, because there is no here.
    """
    return await load_roster(
        session,
        anthropic,
        tenant_id=tenant_id,
        platform="slack",
        channel_id=channel_id,
        thread_id=thread_id,
        default=default,
    )


async def load_panel_details(
    session: AsyncSession,
    anthropic: AsyncAnthropic,
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    roster: Roster,
    agent_name: str,
    channel_id: str,
    thread_id: str | None,
    is_admin: bool,
) -> AgentDetails | None:
    """Everything the Details view shows for `agent_name`, or None.

    The roster in hand is the name→MA id map, so a click carrying a name that
    is no longer in this tenant's roster resolves to None here rather than
    reaching MA with an id the caller supplied. `channel_label` is the Slack
    channel mention, so the routing sentence the core builds reads as a link
    in the modal instead of a raw id.
    """
    match = next((row for row in roster.rows if row.name == agent_name), None)
    if match is None:
        return None
    return await load_agent_details(
        session,
        anthropic,
        tenant_id=tenant_id,
        ma_agent_id=match.ma_agent_id,
        platform="slack",
        channel_id=channel_id,
        thread_id=thread_id,
        deployment_default=runtime.deployment_default,
        github=github_facts(runtime),
        public_mcp_url=public_mcp_url(runtime),
        is_admin=is_admin,
        channel_label=f"<#{channel_id}>",
    )


async def load_panel_answering_map(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
) -> AnsweringMap:
    """Every tier of this workspace's routing, for the Who-answers-where view."""
    return await load_answering_map(session, tenant_id=tenant_id, platform="slack", default=default)


async def resolve_attributions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Map each account that has a Slack identity here to its `<@U…>` mention.

    An account with no Slack principal — a CLI-only actor, or someone who
    acted from Discord — is left out rather than rendered as a bare uuid, and
    the workspace's own stamp account is skipped outright: it is how a seeded
    agent is marked as belonging to the install, not a person who made it, so
    "made by" must never be attached to it.
    """
    workspace_stamp = derive_guild_account_uuid(tenant_id)
    resolved: dict[uuid.UUID, str] = {}
    for account_id in dict.fromkeys(account_ids):
        if account_id == workspace_stamp:
            continue
        external_id = await get_slack_principal_for_account(session, account_id=account_id)
        if external_id:
            resolved[account_id] = f"<@{external_id}>"
    return resolved
