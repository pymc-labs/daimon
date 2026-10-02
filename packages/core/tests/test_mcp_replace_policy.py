"""H2 follow-ups: who counts as sharing an agent, one URL form, and the token race."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.mcp_attach import (
    McpServerReplaceRefusedError,
    decide_mcp_connect,
    decide_mcp_replacement,
)
from daimon.core.mcp_server_url import canonical_mcp_url, same_mcp_url
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, UserScopeRef
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_URL = "https://mcp.linear.app/sse"


def _agent(tenant_id: uuid.UUID, *, servers: list[dict[str, str]] | None = None):
    return ma_agent(
        id="ag_private",
        name="private-bot",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "private-bot"},
        mcp_servers=servers or [],
    )


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://MCP.Example.com:443/mcp/", "https://mcp.example.com/mcp", True),
        ("http://mcp.example.com:80/mcp#frag", "http://mcp.example.com/mcp", True),
        ("https://mcp.example.com:8443/mcp", "https://mcp.example.com/mcp", False),
        ("https://mcp.example.com/MCP", "https://mcp.example.com/mcp", False),
    ],
)
def test_one_canonical_url_form(left: str, right: str, same: bool) -> None:
    assert same_mcp_url(left, right) is same
    assert (canonical_mcp_url(left) == canonical_mcp_url(right)) is same


@pytest.mark.parametrize("sharing", ["handoff-thread", "personal-default"])
async def test_an_agent_answering_in_a_bound_thread_or_as_a_personal_default_is_shared(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    sharing: str,
) -> None:
    """Not a channel/tenant/deployment default, but it still answers other people."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    if sharing == "handoff-thread":
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="1",
            thread_id="2",
            responder_ma_agent_id="ag_private",
            responder_name="private-bot",
            kind="handoff",
        )
    else:
        await set_fields(
            db_session,
            scope=UserScopeRef(account_id=account.id),
            tenant_id=tenant.id,
            agent_name="private-bot",
        )
    await db_session.commit()
    agent = _agent(tenant.id, servers=[{"name": "linear", "type": "url", "url": _URL}])

    decision = await decide_mcp_connect(
        db_session_factory,
        tenant_id=tenant.id,
        agent=agent,
        agent_id=uuid.uuid4(),
        server_name="linear",
        url="https://attacker.example/mcp",
        platform="discord",
        caller=ChannelAdminCaller(platform_user_id="u1"),
        default=DeploymentDefault(agent_name="other"),
        shares_token=False,
    )
    assert decision.refused


async def test_a_channel_admin_replaces_a_server_only_on_an_agent_local_to_them(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Repointing a server goes through channel admin locality, as key changes do."""
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="private-bot",
        mode="agent",
    )
    for channel_id, user_id in (("c1", "u1"), ("c9", "u9")):
        await set_channel_admins(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=channel_id,
            role_ids=[],
            user_ids=[user_id],
            actor_account_id=None,
        )
    await db_session.commit()

    async def outcome(user_id: str, *, managed: bool = False, platform: str = "discord") -> str:
        agent = _agent(tenant.id)
        if managed:
            agent = agent.model_copy(
                update={"metadata": {**agent.metadata, "daimon_managed": "true"}}
            )
        return await decide_mcp_replacement(
            db_session_factory,
            tenant_id=tenant.id,
            platform=platform,
            agent=agent,
            caller=ChannelAdminCaller(platform_user_id=user_id),
            default=DeploymentDefault(agent_name="other"),
        )

    assert await outcome("u1") == "needs_admin", (
        "an agent someone else made and a member bound to c1 is not c1's admin's"
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="private-bot",
        mode="agent",
        set_by_admin=True,
    )
    await db_session.commit()
    assert await outcome("u1") == "allow", "c1's admin repoints the agent a server admin gave c1"
    assert await outcome("u9") == "needs_admin", "an admin of another channel may not"
    assert await outcome("u5") == "needs_admin", "a plain member may not"
    assert await outcome("u1", managed=True) == "managed_agent", "managed stays a server admin's"
    assert await outcome("u1", platform="teams") == "needs_admin", (
        "grants are per platform: c1's Discord admin holds nothing as a Teams caller"
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="teams",
        channel_id="c1",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    await db_session.commit()
    assert await outcome("u1", platform="teams") == "allow", "a Teams grant counts like Discord's"


async def test_the_first_shared_token_for_an_already_connected_url_is_a_replacement(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No token row yet, but the agent already uses the URL: a member's token would
    become everyone's credential for it."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent = _agent(tenant.id, servers=[{"name": "linear", "type": "url", "url": _URL}])

    decision = await decide_mcp_connect(
        db_session_factory,
        tenant_id=tenant.id,
        agent=agent,
        agent_id=uuid.uuid4(),
        server_name="linear-2",
        url=_URL + "/",
        platform="discord",
        caller=ChannelAdminCaller(platform_user_id="u1"),
        default=DeploymentDefault(agent_name="private-bot"),
        shares_token=True,
    )
    assert decision.replaces and decision.refused


async def test_the_agent_wide_token_is_never_overwritten_when_replace_is_not_allowed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The write re-checks under a lock, closing the decide-then-upsert race."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    agent_id = uuid.uuid4()
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_URL,
        plaintext_token="first",
        replace_allowed=False,
    )
    with pytest.raises(McpServerReplaceRefusedError):
        await save_agent_mcp_credential(
            sessionmaker=db_session_factory,
            fernet=fernet,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url="https://MCP.linear.app/sse/",
            plaintext_token="second",
            replace_allowed=False,
        )
    from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials

    stored = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory, fernet=fernet, tenant_id=tenant.id, agent_id=agent_id
    )
    assert [c.token for c in stored] == ["first"]


async def test_two_concurrent_member_first_writes_exactly_one_wins(
    db_nullpool_engine, db_clean
) -> None:
    import asyncio

    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await make_tenant(session)
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    agent_id = uuid.uuid4()

    async def write(value: str) -> str:
        await save_agent_mcp_credential(
            sessionmaker=sm,
            fernet=fernet,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url=_URL,
            plaintext_token=value,
            replace_allowed=False,
        )
        return value

    results = await asyncio.gather(write("a"), write("b"), return_exceptions=True)
    winners = [r for r in results if isinstance(r, str)]
    losers = [r for r in results if isinstance(r, McpServerReplaceRefusedError)]
    assert len(winners) == 1 and len(losers) == 1
    from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials

    stored = await resolve_agent_mcp_credentials(
        sessionmaker=sm, fernet=fernet, tenant_id=tenant.id, agent_id=agent_id
    )
    assert [c.token for c in stored] == winners


async def test_attach_backstop_refuses_a_shared_token_for_a_url_attached_meanwhile() -> None:
    """Same name, same URL, already attached by someone else after the decision."""
    import re

    import httpx
    from daimon.core.mcp_attach import attach_mcp_server_to_agent
    from daimon.testing.ma import MARouter, build_fake_anthropic

    agent = _agent(uuid.uuid4(), servers=[{"name": "linear", "type": "url", "url": _URL}])
    updates: list[bytes] = []

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updates.append(req.content)
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    router = MARouter()
    router.add_agent(agent)
    router.add("POST", rf"/v1/agents/{agent.id}", on_update)
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(McpServerReplaceRefusedError):
        await attach_mcp_server_to_agent(
            client,
            agent.id,
            server_name="linear",
            url=_URL,
            replace_allowed=False,
            shares_token=True,
        )
    assert updates == []
    # An OAuth grant (personal) re-attaching the same server is a no-op, not a refusal.
    await attach_mcp_server_to_agent(
        client, agent.id, server_name="linear", url=_URL, replace_allowed=False
    )


async def test_concurrent_member_and_admin_first_writes_always_leave_the_admin_token(
    db_nullpool_engine, db_clean
) -> None:
    """Both writers take the same lock: member-then-admin is an allowed overwrite,
    admin-then-member refuses the member. Either way the admin's token stands."""
    import asyncio

    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sm() as session, session.begin():
        tenant = await make_tenant(session)
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    agent_id = uuid.uuid4()

    async def write(value: str, *, allowed: bool) -> None:
        await save_agent_mcp_credential(
            sessionmaker=sm,
            fernet=fernet,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url=_URL,
            plaintext_token=value,
            replace_allowed=allowed,
        )

    for _ in range(5):
        async with sm() as session, session.begin():
            from sqlalchemy import text

            await session.execute(text("DELETE FROM agent_mcp_credentials"))
        results = await asyncio.gather(
            write("member", allowed=False), write("admin", allowed=True), return_exceptions=True
        )
        assert all(r is None or isinstance(r, McpServerReplaceRefusedError) for r in results)
        from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials

        stored = await resolve_agent_mcp_credentials(
            sessionmaker=sm, fernet=fernet, tenant_id=tenant.id, agent_id=agent_id
        )
        assert [c.token for c in stored] == ["admin"]


async def test_a_url_variant_rotates_the_one_row_instead_of_adding_a_duplicate(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials

    tenant = await make_tenant(db_session)
    await db_session.commit()
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    args = dict(
        sessionmaker=db_session_factory, fernet=fernet, tenant_id=tenant.id, agent_id=uuid.uuid4()
    )
    await save_agent_mcp_credential(**args, mcp_server_url=_URL, plaintext_token="one")
    await save_agent_mcp_credential(
        **args, mcp_server_url="https://MCP.linear.app:443/sse/", plaintext_token="two"
    )
    stored = await resolve_agent_mcp_credentials(**args)
    assert [(c.mcp_server_url, c.token) for c in stored] == [(_URL, "two")]


def _stateful_ma(tenant_id: uuid.UUID):
    """An MA fake whose agent keeps its attached servers, for connect tests."""
    import json
    import re

    import httpx
    from daimon.testing.ma import MARouter, build_fake_anthropic, list_response

    state = {
        "agent": ma_agent(
            id="ag_private",
            name="private-bot",
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": "private-bot"},
            mcp_servers=[],
        ).model_dump(mode="json")
    }
    vault_posts: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json.loads(req.content)
        state["agent"] = {**state["agent"], "mcp_servers": body.get("mcp_servers") or []}
        return httpx.Response(200, json=state["agent"])

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([state["agent"]]))
    router.add(
        "GET", r"/v1/agents/ag_private", lambda _r, _m: httpx.Response(200, json=state["agent"])
    )
    router.add("POST", r"/v1/agents/ag_private", on_update)

    def on_vault_post(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        vault_posts.append(json.loads(req.content))
        return httpx.Response(
            500, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
        )

    router.add("POST", r"/v1/vaults.*", on_vault_post)
    router.add("GET", r"/v1/vaults.*", lambda _r, _m: list_response([]))
    return build_fake_anthropic(router.dispatch), state, vault_posts


@pytest.mark.parametrize("interleave", ["admin-attaches-first", "admin-publishes-first"])
async def test_no_reader_ever_sees_a_refused_members_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    interleave: str,
) -> None:
    """Astra's race: another caller's attach or publish lands inside the member's
    connect while a third session keeps resolving (mirroring) the agent-wide
    tokens. The member is refused and the member's token is never visible."""
    import datetime as dt

    from daimon.core import mcp_token_connect
    from daimon.core.agent_mcp_credentials import resolve_agent_mcp_credentials

    tenant = await make_tenant(db_session)
    await db_session.commit()
    from daimon.core.ma_identity import derive_agent_uuid

    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_private")
    client, state, vault_posts = _stateful_ma(tenant.id)
    seen: set[str] = set()

    async def mirror() -> None:
        for cred in await resolve_agent_mcp_credentials(
            sessionmaker=db_session_factory, fernet=fernet, tenant_id=tenant.id, agent_id=agent_uuid
        ):
            seen.add(cred.token)

    real_attach = mcp_token_connect.attach_mcp_server_to_agent
    real_store = mcp_token_connect.store_agent_mcp_token

    async def attach(*args, **kwargs):
        await mirror()
        if interleave == "admin-attaches-first":
            state["agent"] = {
                **state["agent"],
                "mcp_servers": [{"name": "linear-admin", "type": "url", "url": _URL}],
            }
        result = await real_attach(*args, **kwargs)
        await mirror()
        return result

    async def store(session, **kwargs):
        await mirror()
        if interleave == "admin-publishes-first":
            # In production this admin write waits on the lock the member holds;
            # here it shares the test connection, so it lands first.
            await save_agent_mcp_credential(
                sessionmaker=db_session_factory,
                fernet=fernet,
                tenant_id=tenant.id,
                agent_id=agent_uuid,
                mcp_server_url=_URL,
                plaintext_token="admin-token",
            )
        try:
            return await real_store(session, **kwargs)
        finally:
            await mirror()

    monkeypatch.setattr(mcp_token_connect, "attach_mcp_server_to_agent", attach)
    monkeypatch.setattr(mcp_token_connect, "store_agent_mcp_token", store)

    with pytest.raises(McpServerReplaceRefusedError):
        await mcp_token_connect.connect_mcp_server_with_token(
            client,
            sessionmaker=db_session_factory,
            fernet=fernet,
            tenant_id=tenant.id,
            agent_id=agent_uuid,
            account_id=uuid.uuid4(),
            server_name="linear",
            mcp_server_url=_URL,
            token="member-token",
            replace_allowed=False,
            jwt_secret=b"x" * 32,
            public_url="https://daimon.example/mcp",
            now=dt.datetime.now(dt.UTC),
        )
    await mirror()
    assert "member-token" not in seen, "no other session may ever mirror the refused token"
    assert vault_posts == [], "the submitter's own vault copy is written only after success"
