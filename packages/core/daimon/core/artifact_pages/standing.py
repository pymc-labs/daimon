"""Who a page counts as a visitor: the one rule every path asks.

A web visitor is one. So is a member the server's audience rules class as an outsider (a
Discord member with no internal role while `audience_hold` is `on`): a page opens to them,
and they send events, as a visitor does, though the workspace lists them as a member.
A workspace with no such classification has only its web visitors.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from daimon.core.stores import web_people as people_store
from daimon.core.turn_hold import RequesterOrg
from daimon.core.web_identity import chat_identity, linked_member_standing
from sqlalchemy.ext.asyncio import AsyncSession


async def outsider_accounts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> frozenset[uuid.UUID]:
    """Of the accounts, those whose chat account is an outsider here; the same verdict as
    the web principal's `org`, which also keeps their chats to a client's."""
    found: set[uuid.UUID] = set()
    for account_id in account_ids:
        chat = await chat_identity(session, tenant_id=tenant_id, account_id=account_id)
        if chat is None or chat[0] != "discord":
            continue
        standing = await linked_member_standing(session, tenant_id=tenant_id, external_id=chat[1])
        if standing is RequesterOrg.OUTSIDER:
            found.add(account_id)
    return frozenset(found)


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
