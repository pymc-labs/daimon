"""Tests for the chat-initiated repo bind's click-time authorization gate.

`_admin_interaction` / `_member_interaction` are copied verbatim from
`tests/agent_setup/test_authz.py` rather than imported: that module's package
(`agent_setup/`, which carries an `__init__.py`) sits one level below
`tests/`, which carries none, so pytest's `--import-mode=importlib` only
resolves `agent_setup.test_authz` as an importable dotted name once
`agent_setup/test_authz.py` has itself already been collected in the same
session. Running this file alone (as the plan's own verify command does)
raises `ModuleNotFoundError` for that import, so a copy — not an import — is
what actually works standalone. Both builders build a `MagicMock(spec=discord.Member)`
so `isinstance(user, discord.Member)` holds and the admin/member split is real,
never a bare `MagicMock()`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import authz as panel_authz
from daimon.adapters.discord.agent_setup.state import RosterEntry
from daimon.adapters.discord.agent_setup.write import load_agent_inline_pat, store_inline_pat
from daimon.adapters.discord.credential_repo_bind import (
    _AGENT_GONE_MESSAGE,
    _SHARED_AGENT_MESSAGE,
    _WRONG_GUILD_MESSAGE,
    refuse_if_shared_and_not_admin_for_request,
    resolve_repo_binding_credential,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.specs import AgentSpec
from daimon.core.stores import scoped_config_write
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, build_stub_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

# `make_tenant` derives the tenant id from `workspace_id` the exact same way
# `derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))`
# does — see `daimon.testing.factories.make_tenant` and
# `daimon.core.ma_identity.derive_tenant_uuid`. Every tenant seeded below and
# every interaction built below must agree on this one id, or a refusal test
# lands on the wrong-guild branch and passes for the wrong reason. The one
# deliberate exception is the wrong-guild test itself, which seeds a distinct
# workspace_id on purpose.
_GUILD_ID = 111


def _admin_interaction(*, guild_id: int = _GUILD_ID, acked: bool = False) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 1
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = acked
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _member_interaction(*, guild_id: int = _GUILD_ID, acked: bool = False) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 2
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = 999
    interaction.response.is_done.return_value = acked
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _runtime(
    *,
    sessionmaker: object,
    anthropic: object,
    deployment_default: DeploymentDefault | None = None,
    crypto_keys: tuple[str, ...] = (),
    oauth_scopes: tuple[str, ...] = ("repo", "read:user"),
) -> DiscordRuntime:
    settings = MagicMock()
    # Defaults to no crypto configured, matching a bare MagicMock's prior
    # behaviour of never being exercised by the gate-only tests above; the
    # resolution tests below pass real crypto_keys so store_inline_pat /
    # load_agent_inline_pat can round-trip through a real MultiFernet.
    settings.crypto.keys = tuple(MagicMock(get_secret_value=lambda k=k: k) for k in crypto_keys)
    settings.github.oauth_scopes = oauth_scopes
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,  # type: ignore[arg-type]  # a real fake AsyncAnthropic, never a bare mock
        sessionmaker=sessionmaker,  # type: ignore[arg-type]  # a real async_sessionmaker or a spy MagicMock, never invoked as a client
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default or DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _make_agent(
    *, ma_agent_id: str, tenant_id: uuid.UUID, name: str, managed: bool
) -> BetaManagedAgentsAgent:
    metadata = {"daimon_tenant": str(tenant_id)}
    if managed:
        metadata["daimon_managed"] = "true"
    return BetaManagedAgentsAgent(
        id=ma_agent_id,
        type="agent",
        name=name,
        model={"id": "claude-sonnet-4-6"},
        metadata=metadata,
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )


def _list_agents_handler(
    agents: list[BetaManagedAgentsAgent],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return list_response([agent.model_dump(mode="json") for agent in agents])

    return handler


def _counting_handler(
    agents: list[BetaManagedAgentsAgent], *, calls: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return list_response([agent.model_dump(mode="json") for agent in agents])

    return handler


def _repo_probe_transport(
    *,
    calls: list[httpx.Request],
    accepted_pats: frozenset[str] = frozenset(),
    public: bool = False,
) -> httpx.MockTransport:
    """Fake `GET https://api.github.com/repos/{owner_repo}` for both
    `pat_can_access_repo` (Authorization header present) and `is_public_repo`
    (no header). Every request lands in `calls`, so a test can assert "zero
    unauthenticated calls" to pin the skip-the-public-probe branches."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        auth = request.headers.get("authorization")
        if auth is not None:
            pat = auth.removeprefix("Bearer ")
            if pat in accepted_pats:
                return httpx.Response(200, json={"private": True})
            return httpx.Response(404)
        return httpx.Response(200, json={"private": False}) if public else httpx.Response(404)

    return httpx.MockTransport(handler)


def _sent_message(interaction: MagicMock) -> Any:  # noqa: ANN401 -- MagicMock call_args positional arg is untyped by construction
    """Return the ephemeral message text an interaction was sent, on whichever half fired."""
    if interaction.response.send_message.called:
        return interaction.response.send_message.call_args.args[0]
    return interaction.followup.send.call_args.args[0]


async def test_defaults_managed_target_member_refuses_with_shared_agent_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_seeded_1"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant.id, name="daimon", managed=True)
    client = build_fake_anthropic(_list_agents_handler([agent]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _member_interaction()

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    assert refused is True, "a member must not bind a repo to a defaults-managed agent"
    assert _sent_message(interaction) == _SHARED_AGENT_MESSAGE
    assert "working repo" in _sent_message(interaction)
    assert "Manage Server" in _sent_message(interaction)
    assert "`/agent-setup`" in _sent_message(interaction)
    assert "keys" not in _sent_message(interaction)
    assert "fork" not in _sent_message(interaction)


async def test_reachable_non_managed_target_flips_with_the_scope_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Branches (d) and (e) share one refusal message, so text alone cannot tell
    them apart. Running the same fixture with and without the scope row that
    makes the agent reachable, and asserting the result flips, is the only way
    to pin branch (e) specifically — deleting it would leave this test green
    with the scope row removed unless the flip is checked both ways."""
    ma_agent_id = "agent_reachable_1"
    agent_name = "bot"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=tenant.id, name=agent_name, managed=False
    )
    client = build_fake_anthropic(_list_agents_handler([agent]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)

    async with db_session_factory() as session, session.begin():
        await scoped_config_write.set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant.id),
            tenant_id=tenant.id,
            agent_name=agent_name,
        )
    scoped_interaction = _member_interaction()
    refused_when_reachable = await refuse_if_shared_and_not_admin_for_request(
        scoped_interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )
    assert refused_when_reachable is True, "the tenant-scoped default agent must refuse a member"
    assert _sent_message(scoped_interaction) == _SHARED_AGENT_MESSAGE

    async with db_session_factory() as session, session.begin():
        await scoped_config_write.unset_fields(
            session, scope=TenantScopeRef(tenant_id=tenant.id), fields=["agent_name"]
        )
    unscoped_interaction = _member_interaction()
    refused_when_unreachable = await refuse_if_shared_and_not_admin_for_request(
        unscoped_interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )
    assert refused_when_unreachable is False, (
        "removing the only scope row naming this agent must free it for its own member"
    )
    assert refused_when_reachable != refused_when_unreachable, (
        "only branch (e), reachability, can produce this flip — a deleted branch (e) "
        "would leave both calls returning True"
    )


async def test_non_managed_non_reachable_target_member_allowed_no_ephemeral(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_private_1"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant.id, name="bot", managed=False)
    client = build_fake_anthropic(_list_agents_handler([agent]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _member_interaction()

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    assert refused is False, "a private, unreachable agent is the member's own to bind a repo to"
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_admin_passes_on_defaults_managed_target_before_any_ma_request(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ordering pin: reordering the admin branch after the defaults-managed
    branch must turn this red. Verified empirically by temporarily swapping
    them (see summary)."""
    ma_agent_id = "agent_seeded_2"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant.id, name="daimon", managed=True)
    calls: list[httpx.Request] = []
    client = build_fake_anthropic(_counting_handler([agent], calls=calls))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _admin_interaction()

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    assert refused is False, "a live admin must be able to bind a repo to the seeded agent"
    assert len(calls) == 0, "the admin branch must precede any MA request"
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


async def test_unknown_agent_uuid_refuses_with_agent_gone_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    client = build_fake_anthropic(_list_agents_handler([]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _member_interaction()

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=uuid.uuid4()
    )

    assert refused is True, "a derived agent uuid matching no MA agent must fail closed"
    assert _sent_message(interaction) == _AGENT_GONE_MESSAGE


async def test_wrong_guild_refuses_before_the_admin_check(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Using an admin interaction here is the point: it proves the tenant
    re-derivation precedes even the admin short-circuit."""
    seeded_workspace_id = "555001"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=seeded_workspace_id)
    calls: list[httpx.Request] = []
    client = build_fake_anthropic(_counting_handler([], calls=calls))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _admin_interaction(guild_id=_GUILD_ID)

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=uuid.uuid4()
    )

    assert refused is True, "a request row minted for a different guild's tenant must refuse"
    assert _sent_message(interaction) == _WRONG_GUILD_MESSAGE
    assert len(calls) == 0, "the tenant check must precede every MA request, admin or not"


async def test_unacked_refusal_uses_response_send_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_seeded_3"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant.id, name="daimon", managed=True)
    client = build_fake_anthropic(_list_agents_handler([agent]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _member_interaction(acked=False)

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    assert refused is True
    interaction.response.send_message.assert_called_once()
    interaction.followup.send.assert_not_called()


async def test_acked_refusal_uses_followup_send(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ma_agent_id = "agent_seeded_4"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(ma_agent_id=ma_agent_id, tenant_id=tenant.id, name="daimon", managed=True)
    client = build_fake_anthropic(_list_agents_handler([agent]))
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)
    interaction = _member_interaction(acked=True)

    refused = await refuse_if_shared_and_not_admin_for_request(
        interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    assert refused is True
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_called_once()


@pytest.mark.parametrize(
    ("is_admin", "is_system", "reachable"),
    [
        (is_admin, is_system, reachable)
        for is_admin in (True, False)
        for is_system in (True, False)
        for reachable in (True, False)
    ],
)
async def test_decision_table_parity_with_the_panel_gate(
    db_session_factory: async_sessionmaker[AsyncSession],
    is_admin: bool,
    is_system: bool,
    reachable: bool,
) -> None:
    """Parity is between the two DECISIONS, not the two call sites: the chat
    path additionally runs this gate as a pre-filter before a modal opens
    (added by the next plan), and the panel's own repo-bind path gates at
    open and at submit too — so even that shape matches — but this test's
    scope is only the shared boolean.

    Each side's own fail-closed guards (a missing roster entry on the panel
    side; a wrong guild or an unresolvable agent on the chat side) are inputs
    the other side does not have, and are held out of the comparison by
    construction: the panel side always gets a real entry, and the chat side
    always gets a matching tenant and a resolvable agent.
    """
    ma_agent_id = "agent_parity"
    agent_name = "bot"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    agent = _make_agent(
        ma_agent_id=ma_agent_id, tenant_id=tenant.id, name=agent_name, managed=is_system
    )
    client = build_fake_anthropic(_list_agents_handler([agent]))
    if reachable:
        async with db_session_factory() as session, session.begin():
            await scoped_config_write.set_fields(
                session,
                scope=TenantScopeRef(tenant_id=tenant.id),
                tenant_id=tenant.id,
                agent_name=agent_name,
            )
    runtime = _runtime(sessionmaker=db_session_factory, anthropic=client)

    chat_interaction = _admin_interaction() if is_admin else _member_interaction()
    chat_refused = await refuse_if_shared_and_not_admin_for_request(
        chat_interaction, runtime=runtime, tenant_id=tenant.id, agent_id=agent_id
    )

    panel_interaction = _admin_interaction() if is_admin else _member_interaction()
    entry = RosterEntry(
        name=agent_name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=agent_name, model="claude-sonnet-4-6"),
        is_system=is_system,
    )
    panel_refused = await panel_authz.refuse_if_shared_and_not_admin(
        panel_interaction, runtime=runtime, entry=entry
    )

    assert chat_refused == panel_refused, (
        f"decision mismatch for admin={is_admin} system={is_system} reachable={reachable}: "
        f"chat gate returned {chat_refused}, panel gate returned {panel_refused}"
    )


# --- resolve_repo_binding_credential -----------------------------------------
#
# These drive the helper directly with an httpx.AsyncClient over
# httpx.MockTransport -- no Discord interaction is needed at all.


async def test_pasted_pat_that_github_accepts_returns_inline_pat_ref_and_is_readable_back(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    calls: list[httpx.Request] = []
    transport = _repo_probe_transport(calls=calls, accepted_pats=frozenset({"ghp_good"}))
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        ref, proof = await resolve_repo_binding_credential(
            runtime,
            http_client,
            agent_id=agent_id,
            account_id=account_id,
            repo_url="github.com/o/r",
            pasted_pat="ghp_good",
            now=now,
        )

    assert ref == f"inline-pat:{agent_id}", (
        "a pasted, GitHub-accepted PAT must resolve to the inline-pat ref"
    )
    assert proof.kind == "pat"
    assert proof.at == now
    assert proof.account_id == account_id
    stored = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert stored == "ghp_good", (
        "the accepted PAT must be readable back through load_agent_inline_pat"
    )


async def test_pasted_pat_that_github_refuses_raises_and_writes_no_credential(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    calls: list[httpx.Request] = []
    transport = _repo_probe_transport(calls=calls, accepted_pats=frozenset())
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(DaimonError, match="can't access this repo"):
            await resolve_repo_binding_credential(
                runtime,
                http_client,
                agent_id=agent_id,
                account_id=account_id,
                repo_url="github.com/o/r",
                pasted_pat="ghp_bad",
                now=datetime.now(UTC),
            )

    stored = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert stored is None, "a GitHub-refused PAT must never be written"


async def test_blank_pat_no_stored_pat_public_repo_returns_anon_ref_with_public_proof(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    calls: list[httpx.Request] = []
    transport = _repo_probe_transport(calls=calls, public=True)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        ref, proof = await resolve_repo_binding_credential(
            runtime,
            http_client,
            agent_id=agent_id,
            account_id=account_id,
            repo_url="github.com/o/r",
            pasted_pat=None,
            now=now,
        )

    assert ref == "anon:", "a verifiably public repo with a blank token must resolve to anon:"
    assert proof.kind == "public"
    assert proof.at == now
    assert proof.account_id == account_id


async def test_blank_pat_no_stored_pat_private_repo_raises_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    calls: list[httpx.Request] = []
    transport = _repo_probe_transport(calls=calls, public=False)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(DaimonError, match="isn't publicly readable"):
            await resolve_repo_binding_credential(
                runtime,
                http_client,
                agent_id=agent_id,
                account_id=account_id,
                repo_url="github.com/o/r",
                pasted_pat=None,
                now=datetime.now(UTC),
            )

    stored = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert stored is None, "a private repo with no token must never write a credential"


async def test_blank_pat_stored_pat_covers_repo_returns_inline_pat_ref_skipping_public_probe(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    await store_inline_pat(
        runtime, account_id=account_id, agent_id=agent_id, plaintext_pat="ghp_stored_ok"
    )
    calls: list[httpx.Request] = []
    transport = _repo_probe_transport(calls=calls, accepted_pats=frozenset({"ghp_stored_ok"}))

    async with httpx.AsyncClient(transport=transport) as http_client:
        ref, proof = await resolve_repo_binding_credential(
            runtime,
            http_client,
            agent_id=agent_id,
            account_id=account_id,
            repo_url="github.com/o/r",
            pasted_pat=None,
            now=now,
        )

    assert ref == f"inline-pat:{agent_id}", (
        "an existing stored PAT that covers the repo must win over any public probe"
    )
    assert proof.kind == "pat"
    assert len(calls) >= 1
    assert all(call.headers.get("authorization") is not None for call in calls), (
        "a stored PAT that covers the repo must skip the unauthenticated public probe entirely"
    )


async def test_blank_pat_stored_pat_does_not_cover_repo_raises_without_falling_through(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid.uuid4()
    account_id = uuid.uuid4()
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_stub_anthropic(),
        crypto_keys=(Fernet.generate_key().decode(),),
    )
    await store_inline_pat(
        runtime, account_id=account_id, agent_id=agent_id, plaintext_pat="ghp_stored_stale"
    )
    calls: list[httpx.Request] = []
    # accepted_pats is empty AND public=True: if the code wrongly fell
    # through to the public probe on a mismatch, this would return anon:
    # instead of raising -- exactly what this test pins against.
    transport = _repo_probe_transport(calls=calls, accepted_pats=frozenset(), public=True)

    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(DaimonError, match="already has a stored GitHub token"):
            await resolve_repo_binding_credential(
                runtime,
                http_client,
                agent_id=agent_id,
                account_id=account_id,
                repo_url="github.com/o/r",
                pasted_pat=None,
                now=datetime.now(UTC),
            )

    assert len(calls) >= 1
    assert all(call.headers.get("authorization") is not None for call in calls), (
        "a stored PAT that fails re-verification must never fall through to the public probe"
    )
