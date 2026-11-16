"""Which agent one person's DM reaches, asked outside chat (web, CLI), with groups read live.

The groups come from the platform as at delivery: a Discord member's roles, Slack user
groups and Teams team rosters, asked only for the groups a grant or rule names. A
platform this process cannot reach leaves them unknown, so a decision that needs them
is `DmUnchecked`, as a real DM would be.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from cryptography.fernet import InvalidToken
from daimon.core.authz import build_subject
from daimon.core.config import Settings
from daimon.core.discord_membership import fetch_discord_member
from daimon.core.dm_access import (
    DmAgent,
    decide_dm,
    group_ids_of,
    groups_by_members,
    load_routing,
    member_roles,
    resolve_groups,
)
from daimon.core.dm_routing import DmDecision, DmRuleFact
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.native_admins import GroupLookupFailed, GroupMembers
from daimon.core.slack_user_groups import user_group_members
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.teams_graph import GraphClient, GraphUnavailable, app_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class DmExplanation:
    decision: DmDecision
    rules: list[DmRuleFact]
    groups: frozenset[str] | None
    """The named groups the person is in; None when the platform could not say."""


async def explain_dm(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    tenant: TenantRow,
    agents: Sequence[DmAgent],
    user_id: str,
) -> DmExplanation:
    """Decide as a DM from `user_id` would, never from a pick (an admin asks for anyone).

    Platform reads happen between two sessions, so a slow platform holds no connection.
    """
    async with sessionmaker() as session:
        routing = await load_routing(session, tenant_id=tenant.id)
        slack = (
            await get_slack_bot_token(session, team_id=tenant.external_id)
            if tenant.platform == "slack"
            else None
        )
    member = Membership()
    match tenant.platform:
        case "discord":
            member = await _discord_member(http, settings, tenant, user_id)
            lookup = (
                groups_by_members(None)
                if member.is_member is None
                else member_roles(member.role_ids)
            )
        case "slack":
            lookup = groups_by_members(
                None
                if slack is None
                else _slack_members(http, settings, slack.encrypted_token, tenant)
            )
        case "teams":
            user_id = user_id.lower()
            lookup = groups_by_members(_teams_members(http, settings, tenant))
        case _:
            lookup = groups_by_members(None)
    groups = await resolve_groups(lookup, user_id, group_ids_of(routing), tenant_id=tenant.id)
    subject = build_subject(
        is_admin=False,
        platform_user_id=user_id,
        role_ids=member.role_ids,
        verified_membership=member.is_member,
    )
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
        decision = await decide_dm(
            session,
            settings,
            policy,
            tenant_id=tenant.id,
            platform=tenant.platform,
            agents=agents,
            subject=subject,
            account_id=None,
            groups=groups,
        )
    return DmExplanation(decision, routing.rules, groups)


async def _discord_member(
    http: httpx.AsyncClient, settings: Settings, tenant: TenantRow, user_id: str
) -> Membership:
    """Read fresh by delivery's own member fetch; unknown without a bot or an answer."""
    if settings.discord is None:
        return Membership()
    token = settings.discord.bot_token.get_secret_value()
    resolver = CachedMembershipResolver(
        lambda _tenant, guild, user: fetch_discord_member(http, token, guild, user)
    )
    return await resolver.resolve(tenant.id, tenant.external_id, user_id, require_fresh=True)


def _slack_members(
    http: httpx.AsyncClient, settings: Settings, encrypted: bytes, tenant: TenantRow
) -> GroupMembers | None:
    """User groups read with the workspace's bot token; None when it cannot be decrypted."""
    keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
    try:
        token = decrypt_token(build_multifernet(keys), encrypted)
    except (InvalidToken, ValueError):
        return None
    return user_group_members(http, token=token, team_id=tenant.external_id)


def _teams_members(
    http: httpx.AsyncClient, settings: Settings, tenant: TenantRow
) -> GroupMembers | None:
    """Team rosters through the deployment's one Entra app; None for another tenant."""
    teams = settings.teams
    if teams is None or teams.tenant_id.lower() != tenant.external_id.lower():
        return None
    graph = GraphClient(
        http,
        app_token(
            http,
            tenant_id=teams.tenant_id,
            client_id=teams.client_id,
            client_secret=teams.client_secret.get_secret_value(),
        ),
    )

    async def members(group_id: str) -> frozenset[str]:
        try:
            return await graph.list_team_member_ids(group_id)
        except GraphUnavailable as exc:
            raise GroupLookupFailed(exc.reason) from exc

    return members
