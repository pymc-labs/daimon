"""A person's own data in a workspace: only theirs, never through an agent key."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.authz import Subject
from daimon.core.coding_keys import list_my_coding_keys, revoke_my_coding_key
from daimon.core.profile import (
    ProfileRefused,
    disconnect_token,
    forget_personal_memory,
    list_connections,
    list_personal_memory,
)
from daimon.core.stores.agent_memory_stores import insert_personal_memory_store
from daimon.core.stores.mcp_tokens import create_mcp_token_row
from daimon.core.stores.microsoft_user_tokens import (
    count_microsoft_user_tokens,
    upsert_microsoft_user_token,
)
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.UTC)
ME = Subject()


async def test_memory_lists_per_agent_and_forget_clears_only_that_agent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    me = await make_account(db_session, tenant=tenant)
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    agents = (uuid.uuid4(), uuid.uuid4())
    stores: list[str] = []
    for agent in agents:
        store = await client.beta.memory_stores.create(name="p", description="d")
        await client.beta.memory_stores.memories.create(store.id, path="/a.md", content="x")
        await insert_personal_memory_store(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent,
            account_id=me.id,
            memory_store_id=store.id,
        )
        stores.append(store.id)

    listed = await list_personal_memory(
        db_session, client, subject=ME, tenant_id=tenant.id, account_id=me.id
    )
    assert {m.agent_id for m in listed} == set(agents), "each agent with memory lists"

    removed = await forget_personal_memory(
        db_session, client, subject=ME, tenant_id=tenant.id, account_id=me.id, agent_id=agents[0]
    )
    assert removed == 1, "one file went"
    assert state.memories[stores[0]] == [], "that agent's memory is empty"
    assert len(state.memories[stores[1]]) == 1, "the other agent's memory is untouched"


async def test_forgetting_with_an_agent_the_person_has_no_memory_with_refuses(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    me = await make_account(db_session, tenant=tenant)
    client = build_fake_anthropic(make_fake_memory_store_handler(FakeMemoryStoreState()))
    with pytest.raises(ProfileRefused):
        await forget_personal_memory(
            db_session,
            client,
            subject=ME,
            tenant_id=tenant.id,
            account_id=me.id,
            agent_id=uuid.uuid4(),
        )


async def test_an_agent_key_manages_no_personal_data(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    me = await make_account(db_session, tenant=tenant)
    key = Subject(via_agent_key=True)
    with pytest.raises(ProfileRefused):
        await list_connections(db_session, subject=key, tenant_id=tenant.id, account_id=me.id)
    with pytest.raises(ProfileRefused):
        await disconnect_token(
            db_session, subject=key, tenant_id=tenant.id, account_id=me.id, kind="microsoft"
        )


async def test_disconnecting_microsoft_deletes_only_the_persons_own_token(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="teams", workspace_id="org-1")
    me = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    for account, user in ((me, "u-me"), (other, "u-other")):
        await make_platform_principal(
            db_session, platform="teams", external_id=user, tenant=tenant, account=account
        )
        await upsert_microsoft_user_token(
            db_session,
            tenant_id=tenant.id,
            user_id=user,
            encrypted_refresh_token=b"enc",
            scopes="mail",
            now=NOW,
        )

    shown = await list_connections(db_session, subject=ME, tenant_id=tenant.id, account_id=me.id)
    assert [c.kind for c in shown] == ["microsoft"], "the person sees their saved account"

    gone = await disconnect_token(
        db_session, subject=ME, tenant_id=tenant.id, account_id=me.id, kind="microsoft"
    )
    assert gone == 1, "one token went"
    assert await count_microsoft_user_tokens(db_session, tenant_id=tenant.id, user_id="u-me") == 0
    assert (
        await count_microsoft_user_tokens(db_session, tenant_id=tenant.id, user_id="u-other") == 1
    ), "another person's token stays"


async def test_my_coding_keys_list_and_revoke_only_my_own(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    me = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    jtis: dict[str, uuid.UUID] = {}
    for name, account in (("mine", me), ("theirs", other)):
        jtis[name] = uuid.uuid4()
        await create_mcp_token_row(
            db_session,
            jti=jtis[name],
            account_id=account.id,
            tenant_id=tenant.id,
            agent_id=str(uuid.uuid4()),
            label=None,
            created_at=NOW,
            purpose="coding",
        )
    listed = await list_my_coding_keys(db_session, tenant_id=tenant.id, account_id=me.id, now=NOW)
    assert [k.jti for k in listed] == [jtis["mine"]], "only my key lists"

    for name, expected in (("theirs", False), ("mine", True)):
        done = await revoke_my_coding_key(
            db_session, jti=jtis[name], tenant_id=tenant.id, account_id=me.id, now=NOW
        )
        assert done is expected, f"revoking {name} returns {expected}"
    assert (
        await list_my_coding_keys(db_session, tenant_id=tenant.id, account_id=me.id, now=NOW) == []
    )
