"""Who a page counts as a visitor: the one rule every path asks.

A web visitor is one. So is a member the server's audience rules class as an outsider (a
Discord member with no internal role while `audience_hold` is `on`): a page opens to them,
and they send events, as a visitor does, though the workspace lists them as a member.
A workspace with no such classification has only its web visitors.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from daimon.core.channel_audience import audience_mode
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores import web_people as people_store
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.workspace_members import list_member_role_ids
from daimon.core.web_identity import chat_identities
from sqlalchemy.ext.asyncio import AsyncSession


async def outsider_accounts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> frozenset[uuid.UUID]:
    """Of the accounts, those whose chat account is an outsider here; the same verdict as
    the web principal's `org` (`linked_member_standing`), which also keeps their chats to a
    client's. One read each of the mode, the policy, the chat accounts and their roles."""
    if not account_ids or await audience_mode(session, FeaturePlace(tenant_id, "discord")) != "on":
        return frozenset()
    chats = await chat_identities(session, tenant_id=tenant_id, account_ids=account_ids)
    discord = {a: user_id for a, (platform, user_id) in chats.items() if platform == "discord"}
    if not discord:
        return frozenset()
    # No internal role configured, or none mirrored for the member: an outsider (fail closed).
    internal = set((await load_access_policy(session, tenant_id=tenant_id)).internal_role_ids)
    roles = await list_member_role_ids(session, tenant_id=tenant_id, user_ids=discord.values())
    return frozenset(
        a for a, user_id in discord.items() if not internal & set(roles.get(user_id, ()))
    )


async def visitor_accounts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> frozenset[uuid.UUID]:
    """Of the accounts, those a page counts as visitors: web visitors and outsiders."""
    people = await people_store.get_people_for_accounts(
        session, tenant_id=tenant_id, account_ids=account_ids
    )
    visitors = {i for i, person in people.items() if person.kind == "visitor"}
    rest = set(account_ids) - visitors
    return frozenset(
        visitors | await outsider_accounts(session, tenant_id=tenant_id, account_ids=rest)
    )
