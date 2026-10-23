"""Daimon memory tools for provider turns: grant binding, scoping and read-only.

The native memory API is replaced by an in-memory fake at its one seam
(`daimon.core.mux_compat`); grants, policy and store bindings are real rows.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from daimon.core import memory_tools, mux_compat
from daimon.core.memory_tools import MemoryRefused
from daimon.core.stores.turn_memory_grants import (
    TurnMemoryGrantRow,
    get_turn_memory_grant,
    put_turn_memory_grant,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime.now(UTC)


class FakeMemory:
    """Stores keyed by id; each a {path: content} map. Create-only writes."""

    def __init__(self) -> None:
        self.stores: dict[str, dict[str, str]] = {}
        self.created: list[str] = []

    async def create_store(self, _client, payload, *, scope):
        store_id = f"memstore_{len(self.stores)}"
        self.stores[store_id] = {}
        self.created.append(store_id)
        return SimpleNamespace(id=store_id)

    async def delete_store(self, _client, store_id, *, scope):
        self.stores.pop(store_id, None)

    async def entries(self, _client, store_id, *, path_prefix, scope):
        for path, content in sorted(self.stores[store_id].items()):
            yield SimpleNamespace(
                type="memory",
                id=f"{store_id}:{path}",
                path=path,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            )

    async def read(self, _client, store_id, memory_id, *, scope):
        _store, path = memory_id.split(":", 1)
        return SimpleNamespace(content=self.stores[store_id][path])

    async def delete(self, _client, store_id, memory_id, *, expected_content_sha256, scope):
        _store, path = memory_id.split(":", 1)
        content = self.stores[store_id][path]
        assert hashlib.sha256(content.encode()).hexdigest() == expected_content_sha256
        del self.stores[store_id][path]

    async def write(self, _client, store_id, path, content, *, scope):
        assert path not in self.stores[store_id], "create-only"
        self.stores[store_id][path] = content


@pytest.fixture
def fake(monkeypatch) -> FakeMemory:
    from daimon.core import memory_resource, memory_view

    memory = FakeMemory()
    monkeypatch.setattr(memory_resource, "create_memory_store", memory.create_store)
    monkeypatch.setattr(memory_resource, "delete_memory_store", memory.delete_store)
    monkeypatch.setattr(memory_view, "list_memory_entries", memory.entries)
    monkeypatch.setattr(memory_view, "read_memory", memory.read)
    monkeypatch.setattr(memory_view, "delete_memory", memory.delete)
    monkeypatch.setattr(mux_compat, "write_memory", memory.write)
    monkeypatch.setattr(mux_compat, "read_memory", memory.read)
    return memory


@pytest.fixture
async def turn(db_engine: AsyncEngine, db_session: AsyncSession):
    """A tenant, two accounts and a provider-turn credential with its grant."""
    from daimon.core._models import Account, Tenant
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti

    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    guild = str(uuid.uuid4())
    async with sessions.begin() as session:
        tenant = Tenant(
            id=derive_tenant_uuid(platform="discord", workspace_id=guild),
            platform="discord",
            external_id=guild,
        )
        session.add(tenant)
        await session.flush()
        caller, other = Account(tenant_id=tenant.id), Account(tenant_id=tenant.id)
        session.add_all([caller, other])
        await session.flush()
        agent_id = uuid.uuid4()

        async def grant_for(account_id, **changes):
            token = await mint_agent_mcp_token(
                session,
                account_id=account_id,
                tenant_id=tenant.id,
                agent_id=agent_id,
                label="gemini-agent-turn",
                secret=b"a" * 32,
                now=NOW,
                ttl_days=600 / 86400,  # pyright: ignore[reportArgumentType]
                chat_execution=True,
                surface="provider_turn",
            )
            row = TurnMemoryGrantRow(
                jti=token_jti(token),
                tenant_id=tenant.id,
                agent_id=agent_id,
                account_id=account_id,
                agent_name="researcher",
                personal=True,
                read_only=False,
                is_dm=False,
                platform="discord",
                channel_id="channel-1",
                thread_id="thread-1",
                expires_at=NOW + timedelta(seconds=600),
            )
            row = replace(row, **changes)
            await put_turn_memory_grant(session, row)
            return row

        mine = await grant_for(caller.id)
        theirs = await grant_for(other.id)
    return SimpleNamespace(
        sessions=sessions, tenant_id=tenant.id, agent_id=agent_id, mine=mine, theirs=theirs
    )


async def test_the_grant_is_found_only_by_jti_for_its_own_identity(turn):
    grant = turn.mine
    found = await memory_tools.turn_grant(
        turn.sessions,
        jti=grant.jti,
        tenant_id=grant.tenant_id,
        account_id=grant.account_id,
        agent_id=grant.agent_id,
        now=NOW,
    )
    assert found == grant
    for changes in (
        {"jti": uuid.uuid4()},
        {"jti": None},
        {"account_id": turn.theirs.account_id},
        {"agent_id": uuid.uuid4()},
        {"tenant_id": uuid.uuid4()},
        {"now": NOW + timedelta(seconds=601)},
    ):
        args = {
            "jti": grant.jti,
            "tenant_id": grant.tenant_id,
            "account_id": grant.account_id,
            "agent_id": grant.agent_id,
            "now": NOW,
        } | changes
        with pytest.raises(MemoryRefused):
            await memory_tools.turn_grant(turn.sessions, **args)


@pytest.mark.parametrize(
    "path",
    [
        "notes.md",
        "/shared/x",
        "shared/",
        "shared/../x",
        "personal/a//b",
        "shared/a\\b",
        "shared/\x00",
    ],
)
def test_paths_name_only_this_turns_shared_or_personal_memory(path):
    with pytest.raises(MemoryRefused):
        memory_tools.split_path(path)
    assert memory_tools.split_path("shared/notes/a.md") == ("shared", "/notes/a.md")
    assert memory_tools.split_path("personal/me.md") == ("personal", "/me.md")


async def test_shared_memory_is_shared_and_personal_memory_is_the_callers_own(turn, fake):
    client = object()
    mine, theirs = turn.mine, turn.theirs
    await memory_tools.write_memory_at(
        client, turn.sessions, mine, "shared/team.md", "Uses Postgres", expected_content_sha256=None
    )
    await memory_tools.write_memory_at(
        client,
        turn.sessions,
        mine,
        "personal/me.md",
        "Prefers short answers",
        expected_content_sha256=None,
    )
    # Another person's turn on the same agent sees the shared memory, not my personal one.
    assert await memory_tools.read_memory_at(client, turn.sessions, theirs, "shared/team.md") == (
        "Uses Postgres"
    )
    assert (
        await memory_tools.read_memory_at(client, turn.sessions, theirs, "personal/me.md") is None
    )
    paths = {
        entry.path for entry in await memory_tools.list_memories(client, turn.sessions, theirs)
    }
    assert paths == {"shared/team.md"}
    assert len(fake.stores) == 2, "one shared and one personal store"


async def test_a_shared_conversation_has_no_personal_memory(turn, fake):
    shared_thread = replace(turn.mine, personal=False)
    with pytest.raises(MemoryRefused, match="shared conversation"):
        await memory_tools.write_memory_at(
            object(),
            turn.sessions,
            shared_thread,
            "personal/me.md",
            "x",
            expected_content_sha256=None,
        )
    assert not fake.stores


async def test_read_only_is_enforced_on_every_write_from_the_grant_and_live_policy(turn, fake):
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    client = object()
    with pytest.raises(MemoryRefused, match="read-only"):
        await memory_tools.write_memory_at(
            client,
            turn.sessions,
            replace(turn.mine, read_only=True),
            "shared/a.md",
            "x",
            expected_content_sha256=None,
        )
    # A seal landing mid-turn makes the next write refuse.
    async with turn.sessions.begin() as session:
        await set_access_policy(
            session,
            tenant_id=turn.tenant_id,
            policy=TenantAccessPolicy.model_validate({"sealed_channel_ids": ["channel-1"]}),
        )
    with pytest.raises(MemoryRefused, match="read-only"):
        await memory_tools.write_memory_at(
            client, turn.sessions, turn.mine, "shared/a.md", "x", expected_content_sha256=None
        )
    # The DM policy, read live.
    async with turn.sessions.begin() as session:
        await set_access_policy(
            session,
            tenant_id=turn.tenant_id,
            policy=TenantAccessPolicy.model_validate({"dm_memory_read_only": True}),
        )
    dm = replace(turn.mine, is_dm=True)
    with pytest.raises(MemoryRefused, match="read-only"):
        await memory_tools.write_memory_at(
            client, turn.sessions, dm, "shared/a.md", "x", expected_content_sha256=None
        )
    assert await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", "x", expected_content_sha256=None
    ), "a non-DM turn in an unsealed channel still writes"


async def test_an_overwrite_needs_the_content_it_read(turn, fake):
    client = object()
    first = await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", "one", expected_content_sha256=None
    )
    with pytest.raises(MemoryRefused, match="already exists"):
        await memory_tools.write_memory_at(
            client, turn.sessions, turn.mine, "shared/a.md", "two", expected_content_sha256=None
        )
    with pytest.raises(MemoryRefused, match="changed"):
        await memory_tools.write_memory_at(
            client,
            turn.sessions,
            turn.mine,
            "shared/a.md",
            "two",
            expected_content_sha256="0" * 64,
        )
    await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", "two", expected_content_sha256=first
    )
    assert (
        await memory_tools.read_memory_at(client, turn.sessions, turn.mine, "shared/a.md") == "two"
    )
    with pytest.raises(MemoryRefused):
        await memory_tools.delete_memory_at(
            client, turn.sessions, turn.mine, "shared/a.md", expected_content_sha256=first
        )
    two = hashlib.sha256(b"two").hexdigest()
    assert await memory_tools.delete_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", expected_content_sha256=two
    )


async def test_a_grant_goes_with_its_token(turn):
    from daimon.core._models import McpToken
    from sqlalchemy import delete

    async with turn.sessions.begin() as session:
        await session.execute(delete(McpToken).where(McpToken.jti == turn.mine.jti))
    async with turn.sessions() as session:
        assert await get_turn_memory_grant(session, jti=turn.mine.jti) is None


@pytest.mark.parametrize(
    ("shared_owner", "read_only", "is_dm"),
    [(None, False, False), (uuid.uuid4(), False, False), (None, True, True)],
)
def test_the_grant_follows_the_turns_own_admission(shared_owner, read_only, is_dm):
    admission = SimpleNamespace(
        grant=SimpleNamespace(is_dm=is_dm, channel_id="parent", thread_id="thread"),
        shared_owner=shared_owner,
        hold=None,
        memory_read_only=read_only,
        origin_channel_id="not-this",
        config=SimpleNamespace(agent_name="researcher"),
        agent=SimpleNamespace(name="ma-name"),
    )
    args = {
        "jti": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "platform": "discord",
        "worker": False,
        "expires_at": NOW,
    }
    grant = memory_tools.turn_memory_grant(admission, **args)  # pyright: ignore[reportArgumentType]
    assert grant is not None
    # A shared thread never offers anyone's personal store.
    assert grant.personal is (shared_owner is None)
    assert (grant.read_only, grant.is_dm) == (read_only, is_dm)
    # The place is the admission grant's own, never the origin field.
    assert (grant.channel_id, grant.thread_id, grant.agent_name) == (
        "parent",
        "thread",
        "researcher",
    )
    for no_memory in (
        SimpleNamespace(**{**vars(admission), "grant": None}),
        # Held turns mount only their channel's store natively; these tools never serve it.
        SimpleNamespace(**{**vars(admission), "hold": object()}),
    ):
        assert memory_tools.turn_memory_grant(no_memory, **args) is None  # pyright: ignore[reportArgumentType]
    # Workers never get memory.
    assert memory_tools.turn_memory_grant(admission, **{**args, "worker": True}) is None  # pyright: ignore[reportArgumentType]


def test_only_a_provider_target_running_every_memory_tool_keeps_memory():
    from daimon.core.tool_safety import DAIMON_SERVER_NAME
    from daimon.core.turn.runtime_manifest import keeps_memory_through_tools
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, MCPConnection

    def target(provider, allowed, server=DAIMON_SERVER_NAME):
        return AgentSpec(
            name="researcher",
            model=ModelRef(provider=provider, id="m"),
            system="s",
            mcp_servers=(
                MCPConnection(
                    name=server,
                    url="https://mcp.invalid/mcp",
                    tool_policy={"allowed_tools": list(allowed)},
                ),
            ),
        )

    everything = (*memory_tools.MEMORY_TOOLS, "search_tools")
    assert keeps_memory_through_tools(target("gemini", everything))
    assert not keeps_memory_through_tools(target("gemini", memory_tools.MEMORY_TOOLS[:-1]))
    assert not keeps_memory_through_tools(target("gemini", everything, server="other"))
    # Anthropic keeps memory natively; OpenAI turns mint no memory grant (yet).
    assert not keeps_memory_through_tools(target("anthropic", everything))
    assert not keeps_memory_through_tools(target("openai", everything))


async def test_the_mcp_memory_tools_serve_only_a_granted_provider_turn(turn, fake, monkeypatch):
    """Through the real verifier and identity middleware at the HTTP edge."""
    from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
    from daimon.adapters.mcp.middleware.mcp_identity import (
        IdentityMiddleware,
        production_agent_id_resolver,
        production_internal_resolver,
        production_is_admin_resolver,
        production_role_resolver,
        production_subject_resolver,
        production_tenant_resolver,
    )
    from daimon.adapters.mcp.tools.memory import register_memory_tools
    from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
    from daimon.testing.asgi import mcp_session
    from fastmcp import FastMCP

    secret = b"a" * 32
    app = FastMCP("memory", auth=DaimonJWTVerifier(secret=secret, sessionmaker=turn.sessions))
    identity = IdentityMiddleware(
        subject_resolver=production_subject_resolver,
        tenant_resolver=production_tenant_resolver,
        role_resolver=production_role_resolver,
        agent_id_resolver=production_agent_id_resolver,
        is_admin_resolver=production_is_admin_resolver,
        internal_resolver=production_internal_resolver,
        sessionmaker=turn.sessions,
    )
    app.add_middleware(identity)
    register_memory_tools(app, SimpleNamespace(session_factory=turn.sessions, client=object()))  # pyright: ignore[reportArgumentType]
    http = app.http_app()
    now = datetime.now(UTC)

    async def mint(surface, *, grant):
        async with turn.sessions.begin() as session:
            token = await mint_agent_mcp_token(
                session,
                account_id=turn.mine.account_id,
                tenant_id=turn.tenant_id,
                agent_id=turn.agent_id,
                label="gemini-agent-turn",
                secret=secret,
                now=now,
                ttl_days=600 / 86400,  # pyright: ignore[reportArgumentType]
                chat_execution=True,
                surface=surface,
            )
            if grant:
                await put_turn_memory_grant(
                    session,
                    replace(
                        turn.mine, jti=token_jti(token), expires_at=now + timedelta(minutes=10)
                    ),
                )
        return token

    async def call(token, name, **arguments):
        return await mcp_session(
            http, token=token, method="tools/call", params={"name": name, "arguments": arguments}
        )

    try:
        granted = await mint("provider_turn", grant=True)
        wrote = await call(granted, "memory_write", path="shared/team.md", content="Uses Postgres")
        assert "Saved shared/team.md" in json_text(wrote) and '"isError": true' not in json_text(
            wrote
        )
        read = await call(granted, "memory_read", path="shared/team.md")
        assert "Uses Postgres" in json_text(read)
        # An ordinary chat credential and a provider turn without a grant reach nothing.
        for token in (await mint(None, grant=True), await mint("provider_turn", grant=False)):
            refused = await call(token, "memory_read", path="shared/team.md")
            assert '"isError": true' in json_text(refused)
            assert "Uses Postgres" not in json_text(refused)
    finally:
        await identity.drain_audit()


def json_text(value) -> str:
    import json

    return json.dumps(value)


async def test_a_failed_replacement_restores_the_memory_it_replaces(turn, fake, monkeypatch):
    client = object()
    first = await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", "keep me", expected_content_sha256=None
    )
    real = fake.write
    calls = []

    async def failing_once(*args, **kwargs):
        calls.append(args[3])
        if len(calls) == 1:
            raise RuntimeError("provider down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(mux_compat, "write_memory", failing_once)
    with pytest.raises(RuntimeError, match="provider down"):
        await memory_tools.write_memory_at(
            client, turn.sessions, turn.mine, "shared/a.md", "new", expected_content_sha256=first
        )
    assert calls == ["new", "keep me"]
    assert await memory_tools.read_memory_at(client, turn.sessions, turn.mine, "shared/a.md") == (
        "keep me"
    )


async def test_memory_size_and_count_are_capped(turn, fake, monkeypatch):
    client = object()
    monkeypatch.setattr(memory_tools, "_MAX_MEMORIES", 2)
    monkeypatch.setattr(memory_tools, "_MAX_CONTENT_BYTES", 8)
    # Bytes, not characters: four two-byte characters fill eight bytes, a fifth exceeds them.
    await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/a.md", "éééé", expected_content_sha256=None
    )
    with pytest.raises(MemoryRefused, match="too large"):
        await memory_tools.write_memory_at(
            client, turn.sessions, turn.mine, "shared/b.md", "ééééé", expected_content_sha256=None
        )
    await memory_tools.write_memory_at(
        client, turn.sessions, turn.mine, "shared/b.md", "b", expected_content_sha256=None
    )
    with pytest.raises(MemoryRefused, match="full"):
        await memory_tools.write_memory_at(
            client, turn.sessions, turn.mine, "shared/c.md", "c", expected_content_sha256=None
        )
    fake.stores[next(iter(fake.stores))]["/d.md"] = "written natively"
    listed = await memory_tools.list_memories(client, turn.sessions, turn.mine)
    assert len(listed) == 2, "a listing never exceeds the cap"
