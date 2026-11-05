"""A person's own data in a workspace: memory, connected accounts and sign-ins.

Every function acts on the account it is given and nothing else: no argument names another
person, so nobody reaches anyone's data through it. The caller reads the account from its own
session and passes the acting `Subject`, which must not be an agent key: a long-lived key never
manages a person's data on their behalf.

Memory is the personal store each agent keeps for the person. A connected account is a saved
Microsoft 365 or Slack access, or an MCP server the person signed in to. Disconnecting deletes
what Daimon holds; it does not ask the other service to revoke the grant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import anthropic
from anthropic import AsyncAnthropic
from daimon.core.authz import Subject
from daimon.core.errors import UserFacingError
from daimon.core.mcp_server_url import same_mcp_url
from daimon.core.mcp_vault import hold_agent_vault_lock
from daimon.core.memory_view import delete_memory_records, list_memory_records
from daimon.core.mux_backend import platform_scope, resource_scope
from daimon.core.mux_compat import delete_credential, list_credentials, list_vaults
from daimon.core.platform_words import words_for
from daimon.core.stores import agent_memory_stores, identity, tenants, web_identities
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import microsoft_user_tokens as microsoft_store
from daimon.core.stores import slack_user_tokens as slack_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type ConnectionKind = Literal["microsoft", "slack", "mcp"]


class ProfileRefused(UserFacingError):
    """The person may not do this; the message is safe to show them."""


def _own_data(subject: Subject) -> None:
    if subject.via_agent_key:
        raise ProfileRefused("An agent key can't manage a person's data.")


@dataclass(frozen=True, slots=True)
class PersonalMemory:
    """One agent's personal memory for the person: its files, by path."""

    agent_id: uuid.UUID
    paths: tuple[str, ...]


async def list_personal_memory(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> list[PersonalMemory]:
    """The person's personal memory with each agent that holds any, files listed."""
    _own_data(subject)
    scope = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    found: list[PersonalMemory] = []
    stores = await agent_memory_stores.list_live_personal_memory_stores(
        session, tenant_id=tenant_id, account_id=account_id
    )
    for agent_id, store_id in stores:
        try:
            records = await list_memory_records(client, store_id, scope=scope)
        except anthropic.NotFoundError:
            continue  # the store is gone, so is the memory
        if records:
            found.append(PersonalMemory(agent_id, tuple(r.path for r in records)))
    return found


async def forget_personal_memory(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> int:
    """Delete the memories listed now from the person's store with one agent; how many went.

    A memory written or changed since the listing stays, as in the `memory` command.
    The store and its binding stay.
    """
    _own_data(subject)
    scope = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    stores = dict(
        await agent_memory_stores.list_live_personal_memory_stores(
            session, tenant_id=tenant_id, account_id=account_id
        )
    )
    store_id = stores.get(agent_id)
    if store_id is None:
        raise ProfileRefused("You have no memory with this agent.")
    try:
        records = await list_memory_records(client, store_id, scope=scope)
    except anthropic.NotFoundError:
        raise ProfileRefused("You have no memory with this agent.") from None
    return await delete_memory_records(client, store_id, records, scope=scope)


@dataclass(frozen=True, slots=True)
class Connection:
    """Something connected to the person's account that they can disconnect.

    `key` names it to `disconnect`: the server URL for an MCP sign-in, else empty.
    `agent_id` is set only for an MCP sign-in.
    """

    kind: ConnectionKind
    label: str
    key: str = ""
    agent_id: uuid.UUID | None = None


_TOKEN_LABELS: dict[ConnectionKind, str] = {
    "microsoft": "Microsoft 365",
    "slack": words_for("slack").label,
}


async def list_connections(
    session: AsyncSession, *, subject: Subject, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[Connection]:
    """Saved Microsoft 365 and Slack access, and MCP sign-ins, in this workspace."""
    _own_data(subject)
    principals = [
        p
        for p in await identity.list_platform_principals_for_account(session, account_id=account_id)
        if p.tenant_id == tenant_id
    ]
    found: list[Connection] = []
    tenant = await tenants.get_tenant(session, tenant_id)
    for p in principals:
        if (
            p.platform == "teams"
            and await microsoft_store.count_microsoft_user_tokens(
                session, tenant_id=tenant_id, user_id=p.external_id
            )
            > 0
        ):
            found.append(Connection("microsoft", _TOKEN_LABELS["microsoft"]))
        if (
            p.platform == "slack"
            and tenant is not None
            and await slack_store.get_slack_user_token(
                session, team_id=tenant.external_id, slack_user_id=p.external_id
            )
            is not None
        ):
            found.append(Connection("slack", _TOKEN_LABELS["slack"]))
    for sign_in in await flows_store.list_account_sign_ins(
        session, tenant_id=tenant_id, account_id=account_id
    ):
        found.append(
            Connection(
                "mcp", sign_in.server_name, key=sign_in.mcp_server_url, agent_id=sign_in.agent_id
            )
        )
    return found


async def disconnect_token(
    session: AsyncSession,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    kind: Literal["microsoft", "slack"],
) -> int:
    """Delete the person's saved Microsoft 365 or Slack access here; how many rows went."""
    _own_data(subject)
    platform = "teams" if kind == "microsoft" else "slack"
    tenant = await tenants.get_tenant(session, tenant_id)
    deleted = 0
    for p in await identity.list_platform_principals_for_account(session, account_id=account_id):
        if p.tenant_id != tenant_id or p.platform != platform:
            continue
        if kind == "microsoft":
            deleted += await microsoft_store.delete_microsoft_user_token(
                session, tenant_id=tenant_id, user_id=p.external_id
            )
        elif tenant is not None:
            token = await slack_store.get_slack_user_token(
                session, team_id=tenant.external_id, slack_user_id=p.external_id
            )
            if token is not None:
                await slack_store.delete_slack_user_token(
                    session, team_id=tenant.external_id, slack_user_id=p.external_id
                )
                deleted += 1
    return deleted


async def disconnect_mcp_sign_in(
    client: AsyncAnthropic,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    subject: Subject,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_url: str,
) -> bool:
    """Delete the person's own OAuth credential for one server on one agent.

    Removes it from each of the person's vaults for the agent (a Slack-turn vault included)
    under the vault lock, then clears the completed mark so the server counts as signed out.
    False when the person had no such sign-in. A token the agent shares is not theirs and stays.
    """
    _own_data(subject)
    async with sessionmaker() as session:
        mine = await flows_store.list_account_sign_ins(
            session, tenant_id=tenant_id, account_id=account_id
        )
    if not any(s.agent_id == agent_id and same_mcp_url(s.mcp_server_url, server_url) for s in mine):
        return False
    prefix = f"daimon-mcp:{account_id}:{agent_id}"
    listing = platform_scope("daimon.core.profile:disconnect_mcp_sign_in")
    vault_ids = [
        vault.id
        async for vault in list_vaults(client, scope=listing)
        if vault.archived_at is None
        and (vault.display_name == prefix or vault.display_name.startswith(prefix + ":"))
    ]
    owner = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    async with hold_agent_vault_lock(sessionmaker, account_id=account_id, agent_id=agent_id):
        for vault_id in vault_ids:
            held = [
                credential.id
                async for credential in list_credentials(client, vault_id, scope=owner)
                if credential.auth.type == "mcp_oauth"
                and same_mcp_url(credential.auth.mcp_server_url, server_url)
            ]
            for credential_id in held:
                try:
                    await delete_credential(client, vault_id, credential_id, scope=owner)
                except anthropic.NotFoundError:
                    continue
    async with sessionmaker() as session, session.begin():
        await flows_store.forget_account_sign_in(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            server_url=server_url,
        )
    return True


async def unlink_web_sign_in(
    session: AsyncSession,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    person_id: uuid.UUID,
    identity_id: uuid.UUID,
) -> bool:
    """Detach one of the person's own web sign-ins; False when it is not theirs.

    The last sign-in stays: without one the person could not come back.
    """
    _own_data(subject)
    mine = await web_identities.list_for_person(session, person_id=person_id)
    target = next((i for i in mine if i.id == identity_id and i.tenant_id == tenant_id), None)
    if target is None:
        return False
    if len(mine) < 2:
        raise ProfileRefused("This is your only sign-in. Add another before removing it.")
    await web_identities.delete_identity(
        session,
        tenant_id=tenant_id,
        provider=target.provider,
        issuer=target.issuer,
        subject=target.subject,
    )
    return True
