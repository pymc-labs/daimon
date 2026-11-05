"""Connect codes: a prompt's single-use code trades for one coding key."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from daimon.core._models import CodingKeyClaim, McpToken, WebAudit
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.authz import Subject, Surface
from daimon.core.coding_key_connect import (
    CLAIM_AUDIT_ACTION,
    ConnectBatch,
    claim_coding_key,
    connect_status,
    hash_code,
    mint_connect_batch,
)
from daimon.core.coding_key_prompts import CODING_CLIENTS
from daimon.core.coding_keys import list_coding_keys
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_owners
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
SECRET = b"k" * 32
CLAIM_URL = "https://daimon.example.com/coding-keys/claim"
MA_AGENT = "ag_x"


@dataclass
class _World:
    sessions: async_sessionmaker[AsyncSession]
    deps: AgentEditDeps
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    owner: AgentActor
    member: AgentActor

    async def mint(self, *, replaces: uuid.UUID | None = None, actor: AgentActor | None = None):
        return await mint_connect_batch(
            self.deps,
            actor=actor or self.owner,
            ma_agent_id=MA_AGENT,
            claim_url=CLAIM_URL,
            now=NOW,
            replaces=replaces,
        )

    async def claim(self, code: str, *, at: dt.datetime = NOW) -> str | None:
        return await claim_coding_key(self.sessions, code=code, secret=SECRET, now=at)


def code_of(batch: ConnectBatch, client: str) -> str:
    """The code a prompt carries: its `code=` argument."""
    prompt = next(p.prompt for p in batch.prompts if p.id == client)
    return prompt.split("code=")[1].split()[0].strip("'\"`")


@pytest.fixture
async def world(
    db_preflight: DatabasePreflight, db_clean: None, db_committing_engine: AsyncEngine
) -> AsyncIterator[_World]:
    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        owner = await make_account(session, tenant=tenant)
        member = await make_account(session, tenant=tenant)
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=MA_AGENT)
        await agent_owners.record_creator(
            session, tenant_id=tenant.id, agent_id=agent_id, account_id=owner.id
        )
    router = MARouter()
    router.add_agent(
        ma_agent(
            id=MA_AGENT,
            name="helper",
            tenant_id=tenant.id,
            metadata={"daimon_account": str(owner.id)},
        )
    )
    deps = AgentEditDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=sessions,
        public_url="https://daimon.example.com/mcp",
    )

    def actor(account_id: uuid.UUID) -> AgentActor:
        return AgentActor(
            tenant_id=tenant.id,
            account_id=account_id,
            subject=Subject(is_admin=False, platform_user_id=str(account_id)),
            surface=Surface.WEB,
        )

    yield _World(sessions, deps, tenant.id, agent_id, actor(owner.id), actor(member.id))


async def _tokens(world: _World) -> list[McpToken]:
    async with world.sessions() as session:
        return list((await session.scalars(select(McpToken))).all())


async def test_a_batch_has_one_prompt_per_tool_each_with_its_own_code(world: _World) -> None:
    batch = await world.mint()

    assert [p.id for p in batch.prompts] == list(CODING_CLIENTS)
    codes = {code_of(batch, client) for client in CODING_CLIENTS}
    assert len(codes) == len(CODING_CLIENTS), "every tool gets its own code"
    assert batch.expires_at == NOW + dt.timedelta(minutes=10)
    assert all("daimon-helper" in p.prompt for p in batch.prompts)
    assert not await _tokens(world), "a prompt carries a code, never a key"


async def test_a_code_trades_once_for_a_coding_key_that_records_the_tool(world: _World) -> None:
    batch = await world.mint()
    code = code_of(batch, "codex")

    token = await world.claim(code)

    assert token, "the code trades for a key"
    assert await world.claim(code) is None, "a code works once"
    (row,) = await _tokens(world)
    assert (row.purpose, row.client, uuid.UUID(row.agent_id)) == ("coding", "codex", world.agent_id)
    async with world.sessions() as session:
        listed = await list_coding_keys(
            session,
            tenant_id=world.tenant_id,
            agent_id=world.agent_id,
            access="owner",
            viewer_account_id=world.owner.account_id,
            platform="discord",
            now=NOW,
        )
        audits = (
            await session.scalars(select(WebAudit).where(WebAudit.action == CLAIM_AUDIT_ACTION))
        ).all()
    assert [entry.client for entry in listed] == ["codex"]
    assert len(audits) == 1, "the claim is audited like a key made on screen"


async def test_the_first_code_used_kills_the_rest_of_its_batch(world: _World) -> None:
    batch = await world.mint()

    assert await world.claim(code_of(batch, "cursor"))

    for client in CODING_CLIENTS:
        assert await world.claim(code_of(batch, client)) is None, client
    assert len(await _tokens(world)) == 1


async def test_a_code_is_dead_after_ten_minutes(world: _World) -> None:
    batch = await world.mint()
    code = code_of(batch, "pi")

    late = NOW + dt.timedelta(minutes=10)
    assert await world.claim(code, at=late) is None
    assert await world.claim(code, at=NOW + dt.timedelta(minutes=9)), "still good a minute before"


async def test_an_unknown_code_is_refused(world: _World) -> None:
    await world.mint()
    assert await world.claim("not-a-code") is None


async def test_codes_are_hashed_at_rest(world: _World) -> None:
    batch = await world.mint()
    code = code_of(batch, "mcp")

    async with world.sessions() as session:
        hashes = set((await session.scalars(select(CodingKeyClaim.code_hash))).all())
    assert code not in hashes
    assert hash_code(code) in hashes


async def test_two_claims_racing_in_one_batch_mint_one_key(world: _World) -> None:
    batch = await world.mint()
    codes = [code_of(batch, client) for client in CODING_CLIENTS]

    results = await asyncio.gather(*(world.claim(code) for code in codes))

    assert len([r for r in results if r]) == 1, "only one of the racing claims mints"
    assert len(await _tokens(world)) == 1


async def test_the_same_code_claimed_twice_at_once_mints_once(world: _World) -> None:
    code = code_of(await world.mint(), "claude-code")

    results = await asyncio.gather(world.claim(code), world.claim(code))

    assert len([r for r in results if r]) == 1
    assert len(await _tokens(world)) == 1


async def test_a_claim_re_checks_standing_and_an_owner_removed_is_refused(world: _World) -> None:
    code = code_of(await world.mint(), "claude-code")
    async with world.sessions.begin() as session:
        assert await agent_owners.clear_owner(
            session, tenant_id=world.tenant_id, agent_id=world.agent_id
        )

    assert await world.claim(code) is None
    assert not await _tokens(world)


async def test_a_new_batch_replaces_the_previous_unused_one(world: _World) -> None:
    first = await world.mint()
    second = await world.mint(replaces=first.batch)

    assert await world.claim(code_of(first, "codex")) is None, "the replaced batch is dead"
    assert await world.claim(code_of(second, "codex")), "the new batch lives"


async def test_without_replaces_both_batches_live(world: _World) -> None:
    first = await world.mint()
    await world.mint()
    assert await world.claim(code_of(first, "codex"))


async def test_replaces_never_kills_another_persons_batch(world: _World) -> None:
    async with world.sessions.begin() as session:
        await agent_owners.set_owner(
            session,
            tenant_id=world.tenant_id,
            agent_id=world.agent_id,
            owner_account_id=world.owner.account_id,
            source="admin_assigned",
        )
        await agent_owners.add_co_owner(
            session,
            tenant_id=world.tenant_id,
            agent_id=world.agent_id,
            account_id=world.member.account_id,
            added_by_account_id=world.owner.account_id,
        )
    theirs = await world.mint(actor=world.member)

    await world.mint(replaces=theirs.batch)  # the owner names the member's batch

    assert await world.claim(code_of(theirs, "codex")), "someone else's batch is not theirs to kill"


async def test_a_member_who_is_not_an_owner_cannot_mint(world: _World) -> None:
    with pytest.raises(AgentEditError) as raised:
        await world.mint(actor=world.member)
    assert raised.value.kind == "forbidden"


async def test_the_batch_status_follows_the_claim_for_its_maker_only(world: _World) -> None:
    batch = await world.mint()
    status = connect_status(world.deps, actor=world.owner, ma_agent_id=MA_AGENT, batch=batch.batch)
    assert (await status).connected is False

    await world.claim(code_of(batch, "cursor"))

    done = await connect_status(
        world.deps, actor=world.owner, ma_agent_id=MA_AGENT, batch=batch.batch
    )
    assert (done.connected, done.client) == (True, "cursor")
    with pytest.raises(AgentEditError) as raised:
        await connect_status(
            world.deps, actor=world.member, ma_agent_id=MA_AGENT, batch=batch.batch
        )
    assert raised.value.kind in {"forbidden", "not_found"}
    with pytest.raises(AgentEditError):
        await connect_status(
            world.deps, actor=world.owner, ma_agent_id=MA_AGENT, batch=uuid.uuid4()
        )


async def test_claims_are_counted_and_deleted_with_the_account(world: _World) -> None:
    from daimon.core.stores import coding_key_claims as store

    await world.mint()
    async with world.sessions.begin() as session:
        assert await store.count_claims_for_account(
            session, account_id=world.owner.account_id
        ) == len(CODING_CLIENTS)
        await store.delete_claims_for_account(session, account_id=world.owner.account_id)
        assert await session.scalar(select(func.count()).select_from(CodingKeyClaim)) == 0
