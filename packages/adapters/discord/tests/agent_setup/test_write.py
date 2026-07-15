"""Tests for write.py helpers: tenant roster, reconcile call propagation, mask."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import write as write_mod
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.agent_setup.write import (
    call_reconcile_for_panel,
    create_blank_agent,
    load_tenant_roster,
    mask_tail,
    replace_agent_resources_for_panel,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core._models import Tenant
from daimon.core.defaults.report import Action
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, get_pat, upsert_credential_encrypted
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.github_credentials import delete_credential_for_principal
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _agent_dict(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    managed: bool = False,
) -> dict[str, Any]:
    """Build a real BetaManagedAgentsAgent and dump to JSON for the MockTransport.

    Inlined (no factory) per testing skill — every call site shows what it
    constructs, and SDK drift breaks the test loudly.
    """
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": name,
    }
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    if managed:
        metadata["daimon_managed"] = "true"
    return BetaManagedAgentsAgent(
        id=id_,
        type="agent",
        name=name,
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]  # SDK model uses TypedDict + Pydantic forgives dict
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]  # SDK accepts ISO string into datetime
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")


def test_mask_tail_short_input_returns_stars_only() -> None:
    assert mask_tail("abc") == "****", "inputs shorter than 4 must not leak any trailing chars"
    assert mask_tail("") == "****", "empty input must mask to plain stars"


def test_mask_tail_long_input_returns_last_four() -> None:
    assert mask_tail("ghp_1234567890") == "****7890", "last 4 chars are the display mask"
    assert mask_tail("abcd") == "****abcd", "exactly-4-char input may show all four"


async def test_load_tenant_roster_includes_all_tenant_agents(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """SC-1: every non-archived agent in the tenant is visible — no per-user filter."""
    other_account = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
    agents_payload = [
        _agent_dict(id_="ag_mine", name="mine", tenant_id=tenant_id, account_id=account_id),
        _agent_dict(id_="ag_theirs", name="theirs", tenant_id=tenant_id, account_id=other_account),
        _agent_dict(
            id_="ag_default",
            name="daimon",
            tenant_id=tenant_id,
            account_id=None,
            managed=True,
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    names = {entry.name for entry in roster}
    assert "mine" in names, "caller's own agent must appear (per-user filter removed)"
    assert "daimon" in names, "unstamped system agent must be visible to everyone"
    assert "theirs" in names, (
        "another user's stamped agent must now appear in the tenant roster (SC-1)"
    )

    by_name = {entry.name: entry for entry in roster}
    assert by_name["daimon"].is_system is True, (
        "defaults-managed agent (daimon_managed=true) must carry is_system=True"
    )
    assert by_name["mine"].is_system is False, (
        "unmanaged agent (no daimon_managed stamp) must carry is_system=False"
    )
    assert by_name["theirs"].is_system is False, (
        "another user's unmanaged agent must also carry is_system=False"
    )


async def test_load_tenant_roster_seeded_agent_with_account_and_managed_is_system(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """#160: a real seeded agent carries BOTH daimon_account (guild seed stamps
    it, defaults/_reconcile.py:110,176) AND daimon_managed=true (reconciler
    provenance). is_system must key off the managed marker, not the account
    stamp — otherwise every seeded agent on a real tenant renders editable."""
    agents_payload = [
        _agent_dict(
            id_="ag_seeded",
            name="daimon",
            tenant_id=tenant_id,
            account_id=account_id,
            managed=True,
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    by_name = {entry.name: entry for entry in roster}
    assert by_name["daimon"].is_system is True, (
        "seeded agent stamped daimon_account AND daimon_managed=true must be "
        "is_system=True (real seeded-agent shape, #160)"
    )


async def test_load_tenant_roster_fork_agent_stays_editable(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """A panel fork carries daimon_account but NO daimon_managed marker
    (write.py:245-249 stamps managed=False) — it must stay editable."""
    agents_payload = [
        _agent_dict(
            id_="ag_fork",
            name="my-fork",
            tenant_id=tenant_id,
            account_id=account_id,
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    by_name = {entry.name: entry for entry in roster}
    assert by_name["my-fork"].is_system is False, (
        "fork-shaped agent (account stamp, no managed marker) must stay editable"
    )


async def test_load_tenant_roster_explicit_managed_false_stays_editable(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """An agent explicitly stamped daimon_managed=false must not be treated as
    system — only the exact string "true" trips the discriminator."""
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "explicit-false",
        "daimon_account": str(account_id),
        "daimon_managed": "false",
    }
    agent_payload = BetaManagedAgentsAgent(
        id="ag_explicit_false",
        type="agent",
        name="explicit-false",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    by_name = {entry.name: entry for entry in roster}
    assert by_name["explicit-false"].is_system is False, (
        "daimon_managed='false' must not trip the is_system discriminator"
    )


async def test_load_tenant_roster_is_identical_for_two_distinct_accounts(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """SC-1: load_tenant_roster takes no account_id — two distinct callers get the
    same roster (no per-user filtering)."""
    other_account = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
    agents_payload = [
        _agent_dict(id_="ag_a", name="agent-a", tenant_id=tenant_id, account_id=account_id),
        _agent_dict(id_="ag_b", name="agent-b", tenant_id=tenant_id, account_id=other_account),
        _agent_dict(id_="ag_sys", name="daimon", tenant_id=tenant_id, account_id=None),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)

    # Call load_tenant_roster twice (it takes no account_id; both calls must be identical).
    roster_first = await load_tenant_roster(client, tenant_id=tenant_id)
    roster_second = await load_tenant_roster(client, tenant_id=tenant_id)

    names_first = [(e.name, e.ma_agent_id, e.is_system) for e in roster_first]
    names_second = [(e.name, e.ma_agent_id, e.is_system) for e in roster_second]
    assert names_first == names_second, (
        "load_tenant_roster has no account_id parameter — two successive calls must produce "
        "byte-identical rosters (SC-1: independent of caller account)"
    )


def _runtime_with_settings(
    anthropic: Any, *, tenant_id: uuid.UUID, public_url: HttpUrl | None
) -> DiscordRuntime:
    """Build a DiscordRuntime carrying just the bits write.py touches."""
    _ = tenant_id  # runtime no longer carries tenant_id (D-06); threaded into helpers
    settings = MagicMock()
    settings.mcp.public_url = public_url
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


async def test_fork_agent_adds_base_toolset_when_source_lacks_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Forking a legacy agent created before the base-toolset guarantee must not
    propagate the hole — the fork gains the base toolset so skills stay usable."""
    source_payload = _agent_dict(
        id_="ag_src", name="source", tenant_id=tenant_id, account_id=account_id
    )
    created: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [source_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents/ag_src":
            return httpx.Response(200, json=source_payload)
        if request.method == "POST" and request.url.path == "/v1/agents":
            created.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=_agent_dict(
                    id_="ag_fork", name="myfork", tenant_id=tenant_id, account_id=account_id
                ),
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime_with_db(
        build_stub_anthropic(handler),
        sessionmaker=db_session_factory,
        fernet_key=Fernet.generate_key().decode(),
        public_url=None,
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_spec=source_spec,
        new_name="myfork",
        account_id=account_id,
    )

    assert len(created) == 1, "fork must call MA create exactly once"
    tool_types = [t.get("type") for t in created[0].get("tools", [])]
    assert "agent_toolset_20260401" in tool_types, (
        "fork of a toolless source must gain the base toolset; skills require read"
    )


def _runtime_with_db(
    anthropic: Any,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet_key: str,
    public_url: HttpUrl | None = None,
) -> DiscordRuntime:
    """Build a DiscordRuntime with a real sessionmaker + crypto keys for fork credential tests."""
    settings = MagicMock()
    settings.mcp.public_url = public_url
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo", "read:user")
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _fork_handler(
    *,
    source_payload: dict[str, Any],
    fork_id: str,
    fork_name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    created: list[dict[str, Any]],
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [source_payload], "next_page": None})
        if request.method == "GET" and request.url.path == f"/v1/agents/{source_payload['id']}":
            return httpx.Response(200, json=source_payload)
        if request.method == "POST" and request.url.path == "/v1/agents":
            created.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=_agent_dict(
                    id_=fork_id, name=fork_name, tenant_id=tenant_id, account_id=account_id
                ),
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    return handler


async def test_fork_agent_rekeys_source_credential_onto_fork(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """MPP-02/D-02: after fork_agent, get_pat(agent_id=fork) resolves the source's token,
    re-keyed under the fork's OWN principal."""
    tenant = Tenant(id=tenant_id, platform="discord", external_id="test-guild-fork-cred")
    db_session.add(tenant)
    await db_session.flush()

    source_payload = _agent_dict(
        id_="ag_src_cred", name="source", tenant_id=tenant_id, account_id=account_id
    )
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src_cred")
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_fork_cred")

    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext = "ghp_source_token_xxxx1234"
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=source_agent_uuid,
        github_login="(inline-pat)",
        plaintext_token=plaintext,
        scopes=("repo", "read:user"),
    )
    async with db_session_factory() as s, s.begin():
        await set_agent_github_binding(
            s, agent_id=source_agent_uuid, principal_id=source_agent_uuid
        )
        await set_binding(
            s,
            tenant_id=tenant_id,
            agent_id=source_agent_uuid,
            repo_url="github.com/acme/repo",
            default_branch="main",
            ma_secret_ref=f"inline-pat:{source_agent_uuid}",
        )

    created: list[dict[str, Any]] = []
    handler = _fork_handler(
        source_payload=source_payload,
        fork_id="ag_fork_cred",
        fork_name="myfork",
        tenant_id=tenant_id,
        account_id=account_id,
        created=created,
    )
    runtime = _runtime_with_db(
        build_stub_anthropic(handler), sessionmaker=db_session_factory, fernet_key=fernet_key
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_spec=source_spec,
        new_name="myfork",
        account_id=account_id,
    )

    fork_pat = await get_pat(
        principal_id=fork_agent_uuid,
        agent_id=fork_agent_uuid,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert fork_pat == plaintext, "fork's credential must resolve the source's token"

    # D-02: deleting the source credential must not break the fork (no aliasing).
    await delete_credential_for_principal(db_session, principal_id=source_agent_uuid)
    await db_session.commit()
    fork_pat_after_delete = await get_pat(
        principal_id=fork_agent_uuid,
        agent_id=fork_agent_uuid,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert fork_pat_after_delete == plaintext, (
        "fork's credential must survive deletion of the source credential (D-02: no aliasing)"
    )


async def test_fork_agent_raises_when_source_credential_unresolvable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """D-04: fork_agent fails loud when the source's inline-pat binding has no
    resolvable credential (binding row exists, credential row does not)."""
    tenant = Tenant(id=tenant_id, platform="discord", external_id="test-guild-fork-nocred")
    db_session.add(tenant)
    await db_session.flush()

    source_payload = _agent_dict(
        id_="ag_src_nocred", name="source", tenant_id=tenant_id, account_id=account_id
    )
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src_nocred")

    # Binding exists (inline-pat:) but no github_credentials row backs it —
    # the undecryptable/missing-credential case.
    async with db_session_factory() as s, s.begin():
        await set_binding(
            s,
            tenant_id=tenant_id,
            agent_id=source_agent_uuid,
            repo_url="github.com/acme/repo",
            default_branch="main",
            ma_secret_ref=f"inline-pat:{source_agent_uuid}",
        )

    created: list[dict[str, Any]] = []
    handler = _fork_handler(
        source_payload=source_payload,
        fork_id="ag_fork_nocred",
        fork_name="myfork2",
        tenant_id=tenant_id,
        account_id=account_id,
        created=created,
    )
    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_with_db(
        build_stub_anthropic(handler), sessionmaker=db_session_factory, fernet_key=fernet_key
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    with pytest.raises(DaimonError, match="github git-proxy"):
        await write_mod.fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_spec=source_spec,
            new_name="myfork2",
            account_id=account_id,
        )


async def test_fork_agent_copies_anon_binding_without_error_or_credential_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """A public/anon: source binding forks with no error and no credential write;
    the fork's binding carries the same repo with ma_secret_ref copied verbatim."""
    tenant = Tenant(id=tenant_id, platform="discord", external_id="test-guild-fork-anon")
    db_session.add(tenant)
    await db_session.flush()

    source_payload = _agent_dict(
        id_="ag_src_anon", name="source", tenant_id=tenant_id, account_id=account_id
    )
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src_anon")
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_fork_anon")

    async with db_session_factory() as s, s.begin():
        await set_binding(
            s,
            tenant_id=tenant_id,
            agent_id=source_agent_uuid,
            repo_url="github.com/acme/public-repo",
            default_branch="main",
            ma_secret_ref="anon:",
        )

    created: list[dict[str, Any]] = []
    handler = _fork_handler(
        source_payload=source_payload,
        fork_id="ag_fork_anon",
        fork_name="myfork3",
        tenant_id=tenant_id,
        account_id=account_id,
        created=created,
    )
    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_with_db(
        build_stub_anthropic(handler), sessionmaker=db_session_factory, fernet_key=fernet_key
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_spec=source_spec,
        new_name="myfork3",
        account_id=account_id,
    )

    fork_binding = await get_binding(db_session, tenant_id=tenant_id, agent_id=fork_agent_uuid)
    assert fork_binding is not None, "anon: source binding must still be copied to the fork"
    assert fork_binding.repo_url == "acme/public-repo", "fork points at the same repo"
    assert fork_binding.ma_secret_ref == "anon:", "anon: ref is copied verbatim, not rewritten"

    fernet = build_multifernet((fernet_key,))
    fork_pat = await get_pat(
        principal_id=fork_agent_uuid,
        agent_id=fork_agent_uuid,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert fork_pat is None, "no credential write must happen for an anon: source"


async def test_fork_agent_copies_repo_binding_with_rewritten_secret_ref(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """MPP-02/D-03: fork's repo binding matches the source's repo_url/default_branch,
    with ma_secret_ref rewritten to the fork's own inline-pat ref."""
    tenant = Tenant(id=tenant_id, platform="discord", external_id="test-guild-fork-binding")
    db_session.add(tenant)
    await db_session.flush()

    source_payload = _agent_dict(
        id_="ag_src_bind", name="source", tenant_id=tenant_id, account_id=account_id
    )
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src_bind")
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_fork_bind")

    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext = "ghp_bind_token_xxxx5678"
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=source_agent_uuid,
        github_login="(inline-pat)",
        plaintext_token=plaintext,
        scopes=("repo", "read:user"),
    )
    async with db_session_factory() as s, s.begin():
        await set_agent_github_binding(
            s, agent_id=source_agent_uuid, principal_id=source_agent_uuid
        )
        await set_binding(
            s,
            tenant_id=tenant_id,
            agent_id=source_agent_uuid,
            repo_url="https://github.com/acme/private-repo",
            default_branch="develop",
            ma_secret_ref=f"inline-pat:{source_agent_uuid}",
        )

    created: list[dict[str, Any]] = []
    handler = _fork_handler(
        source_payload=source_payload,
        fork_id="ag_fork_bind",
        fork_name="myfork4",
        tenant_id=tenant_id,
        account_id=account_id,
        created=created,
    )
    runtime = _runtime_with_db(
        build_stub_anthropic(handler), sessionmaker=db_session_factory, fernet_key=fernet_key
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_spec=source_spec,
        new_name="myfork4",
        account_id=account_id,
    )

    fork_binding = await get_binding(db_session, tenant_id=tenant_id, agent_id=fork_agent_uuid)
    assert fork_binding is not None, "fork must have a repo binding"
    assert fork_binding.repo_url == "acme/private-repo", "fork points at the same repo"
    assert fork_binding.default_branch == "develop", "default_branch copied from source"
    assert fork_binding.ma_secret_ref == f"inline-pat:{fork_agent_uuid}", (
        "ma_secret_ref rewritten to the fork's own inline-pat ref (D-03)"
    )


async def test_fork_agent_unbound_source_produces_unbound_fork(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """A source with no repo binding forks with no binding and no error."""
    tenant = Tenant(id=tenant_id, platform="discord", external_id="test-guild-fork-unbound")
    db_session.add(tenant)
    await db_session.flush()

    source_payload = _agent_dict(
        id_="ag_src_unbound", name="source", tenant_id=tenant_id, account_id=account_id
    )
    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_fork_unbound")

    created: list[dict[str, Any]] = []
    handler = _fork_handler(
        source_payload=source_payload,
        fork_id="ag_fork_unbound",
        fork_name="myfork5",
        tenant_id=tenant_id,
        account_id=account_id,
        created=created,
    )
    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_with_db(
        build_stub_anthropic(handler), sessionmaker=db_session_factory, fernet_key=fernet_key
    )
    source_spec = AgentSpec(name="source", model="claude-sonnet-4-6")

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_spec=source_spec,
        new_name="myfork5",
        account_id=account_id,
    )

    fork_binding = await get_binding(db_session, tenant_id=tenant_id, agent_id=fork_agent_uuid)
    assert fork_binding is None, "unbound source must produce an unbound fork, no error"


async def test_call_reconcile_for_panel_propagates_public_url_and_account_id(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """SC-2: reconcile must stamp the GUILD account (guild_account_id), not the personal account."""
    captured: dict[str, Any] = {}

    # Use a DISTINCT guild account so a regression (stamping the personal account) fails loudly.
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000001111")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    async def spy_reconcile(
        client: Any,
        spec: AgentSpec,
        *,
        tenant_id: uuid.UUID,
        dry_run: bool,
        account_id: uuid.UUID | None = None,
        public_url: str | None = None,
        managed: bool = True,
    ) -> Any:
        captured["client"] = client
        captured["spec"] = spec
        captured["tenant_id"] = tenant_id
        captured["dry_run"] = dry_run
        captured["account_id"] = account_id
        captured["public_url"] = public_url
        captured["managed"] = managed
        return MagicMock()

    monkeypatch.setattr(write_mod, "reconcile_agent", spy_reconcile)

    selected = RosterEntry(
        name="mine",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="mine", model="claude-sonnet-4-6", system="be helpful"),
    )
    state = PanelState(
        roster=[selected],
        selected=selected,
        account_id=account_id,
        guild_account_id=guild_account,
    )
    runtime = _runtime_with_settings(
        build_stub_anthropic(),
        tenant_id=tenant_id,
        public_url=HttpUrl("https://example.com/mcp"),
    )

    await call_reconcile_for_panel(runtime, state, tenant_id=tenant_id)

    assert captured["account_id"] == guild_account, (
        "SC-2: panel reconcile must stamp the guild account (guild_account_id), "
        "not the personal account_id — regressions fail loudly because guild_account != account_id"
    )
    assert captured["account_id"] != account_id, (
        "SC-2: personal account must NOT be used as the ownership stamp"
    )
    assert captured["public_url"] == "https://example.com/mcp", (
        "panel write must forward public_url so Phase 34 default-MCP merge runs"
    )
    assert captured["tenant_id"] == tenant_id
    assert captured["dry_run"] is False, "panel writes are never dry-run"
    assert captured["managed"] is False, (
        "panel writes target user forks, NOT seeded resources — "
        "managed=True would mark them sweep-eligible on next defaults apply"
    )


async def test_call_reconcile_for_panel_omits_public_url_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    captured: dict[str, Any] = {}

    async def spy_reconcile(
        client: Any,
        spec: AgentSpec,
        *,
        tenant_id: uuid.UUID,
        dry_run: bool,
        account_id: uuid.UUID | None = None,
        public_url: str | None = None,
        managed: bool = True,
    ) -> Any:
        captured["public_url"] = public_url
        return MagicMock()

    monkeypatch.setattr(write_mod, "reconcile_agent", spy_reconcile)

    selected = RosterEntry(
        name="mine",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="mine", model="claude-sonnet-4-6"),
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    await call_reconcile_for_panel(runtime, state, tenant_id=tenant_id)
    assert captured["public_url"] is None, (
        "no public_url configured → no default-MCP merge; panel must pass None"
    )


async def test_create_blank_agent_is_not_marked_managed(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """A panel-created blank agent must NOT be stamped daimon_managed=true.

    SC-2: the caller (panel.py NewAgentModal) passes the guild account; this
    test verifies create_blank_agent forwards whatever account_id is given and
    never flips managed=True (which would make it sweep-eligible, archiving it
    on the next defaults apply).
    """
    captured: dict[str, Any] = {}

    # Use a distinct guild account to verify the stamp is forwarded as-is.
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000002222")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    async def spy_reconcile(
        client: Any,
        spec: AgentSpec,
        *,
        tenant_id: uuid.UUID,
        dry_run: bool,
        account_id: uuid.UUID | None = None,
        public_url: str | None = None,
        managed: bool = True,
    ) -> Any:
        captured["managed"] = managed
        captured["account_id"] = account_id
        return MagicMock()

    monkeypatch.setattr(write_mod, "reconcile_agent", spy_reconcile)

    # Stub find_agents_by_daimon_tag to return empty (no collision).
    async def _no_collision(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _no_collision)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    await create_blank_agent(
        runtime,
        tenant_id=tenant_id,
        name="data scientist",
        system="be helpful",
        model="claude-sonnet-4-6",
        account_id=guild_account,  # SC-2: caller (panel.py) supplies the guild account
    )

    assert captured["managed"] is False, (
        "create_blank_agent makes a guild-owned agent — managed=True would mark it "
        "sweep-eligible, so the next deploy's defaults apply archives it"
    )
    assert captured["account_id"] == guild_account, (
        "SC-2: create_blank_agent must forward the guild account stamp it receives; "
        "passing the personal account would be a regression"
    )
    assert captured["account_id"] != account_id, (
        "SC-2: the personal account must not be the stamp — regression guard"
    )


async def test_create_blank_agent_rejects_duplicate_tenant_name(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Plan-01 create-path guard: if an agent with the same name already exists
    under the guild account, create_blank_agent raises DaimonError (SC-2 + collision
    decision option (a))."""
    from daimon.core.errors import DaimonError

    guild_account = uuid.UUID("00000000-0000-0000-0000-000000003333")

    # Simulate a pre-existing agent owned by the guild account with the same name.
    existing_meta = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "existing-agent",
        "daimon_account": str(guild_account),
    }
    existing_agent = BetaManagedAgentsAgent(
        id="ag_existing",
        type="agent",
        name="existing-agent",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata=existing_meta,
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )

    async def _collision_found(*args: Any, **kwargs: Any) -> list[Any]:
        return [existing_agent]

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _collision_found)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    with pytest.raises(DaimonError, match="existing-agent"):
        await create_blank_agent(
            runtime,
            tenant_id=tenant_id,
            name="existing-agent",
            system="be helpful",
            model="claude-sonnet-4-6",
            account_id=guild_account,
        )


# ----- D-72-01: tenant-scoped name guards -----


async def test_create_blank_agent_rejects_name_held_by_other_owner(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """D-72-01: create_blank_agent rejects a name that exists under a DIFFERENT owner.

    Any non-archived same-name agent in the tenant blocks creation regardless of
    who owns it. Zero MA write calls must fire (collision is detected before reconcile).
    """
    from daimon.core.errors import DaimonError

    other_account = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    # Agent stamped with a different account — NOT the caller's account_id.
    existing_agent = BetaManagedAgentsAgent(
        id="ag_other_owner",
        type="agent",
        name="taken-name",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "taken-name",
            "daimon_account": str(other_account),  # different owner
        },
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )

    async def _collision_found(*args: Any, **kwargs: Any) -> list[Any]:
        return [existing_agent]

    reconcile_calls: list[Any] = []

    async def _spy_reconcile(*args: Any, **kwargs: Any) -> Any:
        reconcile_calls.append(args)
        return MagicMock()

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _collision_found)
    monkeypatch.setattr(write_mod, "reconcile_agent", _spy_reconcile)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    with pytest.raises(DaimonError, match="taken-name"):
        await create_blank_agent(
            runtime,
            tenant_id=tenant_id,
            name="taken-name",
            system=None,
            model="claude-sonnet-4-6",
            account_id=account_id,  # caller's own account; other_account owns the collision
        )

    assert reconcile_calls == [], (
        "D-72-01: create must raise before reconcile when another owner holds the name"
    )


async def test_fork_agent_rejects_new_name_held_by_other_owner(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """D-72-01: fork_agent rejects new_name that exists under a DIFFERENT owner.

    The old docstring promised cross-owner name reuse was allowed; D-72-01 inverts
    that: any non-archived same-name agent in the tenant blocks the fork. Zero MA
    create calls must fire.
    """
    from daimon.core.errors import DaimonError

    other_account = uuid.UUID("00000000-0000-0000-0000-0000000000ee")

    existing_agent = BetaManagedAgentsAgent(
        id="ag_other_fork",
        type="agent",
        name="fork-target",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "fork-target",
            "daimon_account": str(other_account),  # different owner
        },
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )

    create_calls: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agents":
            create_calls.append(request)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    # find_agents_by_daimon_tag is called FIRST (for new_name collision check)
    # and returns the existing agent; the handler for the actual source lookup
    # must never be reached.
    call_count = 0

    async def _collision_for_new_name(*args: Any, **kwargs: Any) -> list[Any]:
        nonlocal call_count
        call_count += 1
        # Only block on the new_name check (first call); source lookup is never reached
        # because the DaimonError is raised first.
        return [existing_agent]

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _collision_for_new_name)

    runtime = _runtime_with_settings(
        build_stub_anthropic(handler), tenant_id=tenant_id, public_url=None
    )
    source_spec = AgentSpec(name="source-agent", model="claude-sonnet-4-6")

    with pytest.raises(DaimonError, match="fork-target"):
        await write_mod.fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_spec=source_spec,
            new_name="fork-target",
            account_id=account_id,  # caller's own account; other_account owns the collision
        )

    assert create_calls == [], (
        "D-72-01: fork must raise before agents.create when another owner holds new_name"
    )


# ----- Plan 04: kick_off_skill_sync -----


@pytest.mark.asyncio
async def test_kick_off_skill_sync_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """The kickoff helper must invoke sync_agent_skills with the right shape."""
    import asyncio

    from daimon.adapters.discord.agent_setup.write import kick_off_skill_sync
    from daimon.core.specs import SkillRepo

    captured: dict[str, Any] = {}

    async def spy_sync(**kwargs: Any) -> Any:
        captured.update(kwargs)
        # Simulate a slow sync — caller must NOT block on it inline.
        await asyncio.sleep(0.05)
        return MagicMock()

    monkeypatch.setattr(write_mod, "sync_agent_skills", spy_sync)

    fernet_key = "GbBz3RJBzTKtPmU0eVqDXn3ssNoyL8N-NCmGUVUWcCQ="  # valid Fernet key for tests

    settings = MagicMock()
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo",)
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    await kick_off_skill_sync(
        runtime,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="bot",
        repo_url="https://github.com/me/skills",
    )
    assert captured["principal_id"] == account_id, (
        "kick_off_skill_sync must pass the account_id as principal_id"
    )
    assert captured["tenant_id"] == tenant_id, "kick_off_skill_sync must propagate tenant_id"
    assert captured["agent_name"] == "bot", "agent_name must be forwarded"
    repos = captured["repos"]
    assert len(repos) == 1 and isinstance(repos[0], SkillRepo), (
        "repos must be a single SkillRepo (real Pydantic constructor)"
    )
    assert repos[0].url == "https://github.com/me/skills", "repo URL must be forwarded"
    assert repos[0].split is True, (
        "panel path must use split=True so per-SKILL.md discovery scopes the bundle "
        "to individual skill subdirs (matches chat path); split=False bundles the "
        "whole repo and 400s on > 200 files for many real repos"
    )


@pytest.mark.asyncio
async def test_kick_off_skill_sync_uses_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Skill-sync uses Phase 18 crypto config (build_multifernet from settings.crypto.keys)."""
    from cryptography.fernet import MultiFernet
    from daimon.adapters.discord.agent_setup.write import kick_off_skill_sync

    captured: dict[str, Any] = {}

    async def spy_sync(**kwargs: Any) -> Any:
        captured["fernet"] = kwargs["fernet"]
        return MagicMock()

    monkeypatch.setattr(write_mod, "sync_agent_skills", spy_sync)

    fernet_key = "GbBz3RJBzTKtPmU0eVqDXn3ssNoyL8N-NCmGUVUWcCQ="

    settings = MagicMock()
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo",)
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    await kick_off_skill_sync(
        runtime,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_name="bot",
        repo_url="https://github.com/me/skills",
    )
    assert isinstance(captured["fernet"], MultiFernet), (
        "kick_off_skill_sync must build a MultiFernet from settings.crypto.keys"
    )


@pytest.mark.asyncio
async def test_apply_repo_modal_persists_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """LD-04-01: RepoAuthModal must persist via agent_repo_binding.set_binding."""
    from unittest.mock import AsyncMock

    from daimon.adapters.discord.agent_setup import modals as modals_mod
    from daimon.adapters.discord.agent_setup.modals import RepoAuthModal
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores import agent_repo_binding as binding_store

    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild-write")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild-write")
    db_session.add(tenant)
    await db_session.flush()

    ma_agent_id = "agent_017abc"  # MA returns prefixed strings, not UUIDs (BUG-25-01)
    expected_agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        mock_agent = MagicMock()
        mock_agent.id = ma_agent_id
        return mock_agent

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    # Resolve the per-interaction tenant to the seeded tenant (overrides the
    # conftest autouse stub, which returns a different fixture tenant_id).
    monkeypatch.setattr(modals_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant.id))
    # The no-PAT path verifies repo visibility before writing an anon: binding;
    # stub it True so the success path runs without a live api.github.com call.
    monkeypatch.setattr(modals_mod, "is_public_repo", AsyncMock(return_value=True))

    selected = RosterEntry(
        name="bot",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="bot", model="claude-sonnet-4-6"),
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)

    settings = MagicMock()
    settings.crypto.keys = ()
    settings.github.oauth_scopes = ("repo",)
    # No App creds -> is_app_installed_for_repo returns False with zero HTTP
    # calls (D-06); a MagicMock here would be truthy and crash build_app_jwt.
    settings.github.app_id = None
    settings.github.app_private_key = None
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "develop"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]  # no inline PAT — OAuth path

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    row = await binding_store.get_binding(
        db_session, tenant_id=tenant.id, agent_id=expected_agent_uuid
    )
    assert row is not None, "RepoAuthModal must persist a row via agent_repo_binding.set_binding"
    assert row.repo_url == "me/repo", "set_binding normalizes repo_url to canonical owner/repo"
    assert row.default_branch == "develop", "binding default_branch must round-trip"
    assert row.ma_secret_ref, "binding ma_secret_ref must be non-empty (LD-04-01)"


async def test_load_tenant_roster_hydrates_mcp_servers_skills_and_tools(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Regression for BUG-25-UAT-01: MA agent's mcp_servers/skills/tools must
    survive into the RosterEntry's AgentSpec so that Fork deep-copies them."""
    agent_payload = BetaManagedAgentsAgent(
        id="ag_with_mcp",
        type="agent",
        name="research-bot",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]  # SDK forgives dict
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "research-bot",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-05-14T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-14T00:00:00Z",  # type: ignore[arg-type]
        version=2,
        mcp_servers=[
            {"name": "test-mcp", "type": "url", "url": "https://example.com/mcp"},  # type: ignore[list-item]  # SDK accepts dict for response submodels
        ],
        skills=[{"type": "custom", "skill_id": "skill_my", "version": "1"}],  # type: ignore[list-item]
        tools=[
            {  # type: ignore[list-item]
                "type": "mcp_toolset",
                "mcp_server_name": "test-mcp",
                "configs": [],
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            }
        ],
        system="be helpful",
    ).model_dump(mode="json")

    from anthropic.types.beta import SkillListResponse
    from daimon.core.defaults.metadata import tenant_scoped_display_title

    # Canonical title: this tenant's prefix + bare name "mySkill"
    canonical_title = tenant_scoped_display_title(tenant_id=tenant_id, name="mySkill")
    skills_list_payload = [
        SkillListResponse(
            id="skill_my",
            type="skill",
            display_title=canonical_title,
            latest_version="1",
            source="custom",
            created_at="2026-05-14T00:00:00Z",
            updated_at="2026-05-14T00:00:00Z",
        ).model_dump(mode="json")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": skills_list_payload, "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    assert len(roster) == 1, "exactly one agent expected"
    spec = roster[0].spec
    assert spec.mcp_servers and len(spec.mcp_servers) == 1, (
        "mcp_servers must survive roster load — Fork relies on this to deep-copy MCPs"
    )
    assert spec.mcp_servers[0].get("name") == "test-mcp", "MCP name must round-trip"
    assert spec.mcp_servers[0].get("url") == "https://example.com/mcp", "MCP URL must round-trip"
    assert len(spec.skills) == 1, "skills must survive roster load"
    assert spec.skills[0].skill_id == "mySkill", (
        "custom skill_id must round-trip as BARE authoring name (prefix stripped), "
        "not the canonical title or MA skill id — the save path re-prefixes via resolve_refs"
    )
    assert spec.tools and len(spec.tools) == 1, (
        "tools must survive roster load — needed for the mcp_toolset back-reference MA validates"
    )
    assert spec.tools[0].get("mcp_server_name") == "test-mcp", "toolset reference must round-trip"


async def test_replace_agent_resources_for_panel_drops_removed_mcp_not_unioned_back(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Panel remove must REPLACE mcp_servers, not union the removed entry back.

    Regression: panel removes routed through reconcile_agent, whose update path
    unconditionally unions the spec with MA's current state
    (merge_mcp_servers_with_ma). An MCP the user ✕-removed — absent from the
    reduced spec but still on MA — got re-added, so removal silently no-op'd.
    The replace path must send the reduced set verbatim; MA's partial update
    then replaces the array and the entry is actually gone.
    """
    daimon_mcp = {"name": "daimon-mcp", "type": "url", "url": "https://mcp.example.test/mcp"}
    context7 = {"name": "context7", "type": "url", "url": "https://mcp.context7.com/mcp"}
    # MA still carries BOTH — the user's removal has not been persisted yet.
    ma_agent_json = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="myfork",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "myfork",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=2,
        mcp_servers=[daimon_mcp, context7],  # type: ignore[list-item]
        skills=[],
        tools=[],
    ).model_dump(mode="json")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [ma_agent_json], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents/ag_1":
            return httpx.Response(200, json=ma_agent_json)
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents/ag_1":
            captured.update(json.loads(request.content))
            persisted = dict(ma_agent_json)
            persisted["mcp_servers"] = [daimon_mcp]
            persisted["version"] = 3
            return httpx.Response(200, json=persisted)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    runtime = _runtime_with_settings(
        client, tenant_id=tenant_id, public_url=HttpUrl("https://mcp.example.test/mcp")
    )

    # The reduced spec the remove reducer produces: context7 gone, daimon-mcp kept.
    reduced = AgentSpec.model_validate(
        {
            "name": "myfork",
            "model": "claude-sonnet-4-6",
            "system": None,
            "mcp_servers": [daimon_mcp],
            "skills": [],
            # daimon-mcp's toolset stays; context7's was dropped by remove_mcp_at.
            "tools": [{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
        }
    )
    entry = RosterEntry(name="myfork", model="claude-sonnet-4-6", spec=reduced)
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    outcome = await replace_agent_resources_for_panel(runtime, state, tenant_id=tenant_id)

    assert outcome.action is Action.UPDATED, "replace path must report an UPDATED outcome"
    sent = captured.get("mcp_servers")
    assert sent is not None, "the agents.update body must carry mcp_servers"
    names = [m["name"] for m in sent]
    assert names == ["daimon-mcp"], (
        f"removed context7 must NOT be unioned back into the update body; got {names}"
    )


async def test_replace_agent_resources_for_panel_sends_empty_mcp_servers_when_last_removed(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Removing the LAST MCP must send mcp_servers=[] explicitly, not omit it.

    Regression: remove_mcp_at sets mcp_servers=None when the list empties;
    dump_agent_spec(exclude_none=True) then dropped the key entirely. MA's
    partial update PRESERVES an absent field — so the removed MCP stayed in
    mcp_servers while tools (sent, replaced) lost its toolset → MA 400
    "mcp_servers <name> declared but no mcp_toolset references them". The
    removal must send mcp_servers as an explicit empty list so MA replaces it.
    """
    context7 = {"name": "context7", "type": "url", "url": "https://mcp.context7.com/mcp"}
    # MA agent's only MCP is context7 (valid: referenced by its toolset).
    ma_agent_json = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="myfork",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "myfork",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=2,
        mcp_servers=[context7],  # type: ignore[list-item]
        skills=[],
        tools=[],
    ).model_dump(mode="json")

    captured: dict[str, Any] = {}
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [ma_agent_json], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents/ag_1":
            return httpx.Response(200, json=ma_agent_json)
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents/ag_1":
            body = json.loads(request.content)
            captured.update(body)
            seen_keys.extend(body.keys())
            persisted = dict(ma_agent_json)
            persisted["mcp_servers"] = []
            persisted["version"] = 3
            return httpx.Response(200, json=persisted)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    runtime = _runtime_with_settings(
        client, tenant_id=tenant_id, public_url=HttpUrl("https://mcp.example.test/mcp")
    )

    # The reduced spec remove_mcp_at produces when context7 was the only MCP:
    # mcp_servers empties to None; the orphaned mcp_toolset is dropped, leaving
    # just the agent_toolset.
    reduced = AgentSpec.model_validate(
        {
            "name": "myfork",
            "model": "claude-sonnet-4-6",
            "system": None,
            "mcp_servers": None,
            "skills": [],
            "tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
        }
    )
    entry = RosterEntry(name="myfork", model="claude-sonnet-4-6", spec=reduced)
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    outcome = await replace_agent_resources_for_panel(runtime, state, tenant_id=tenant_id)

    assert outcome.action is Action.UPDATED, "replace path must report an UPDATED outcome"
    assert "mcp_servers" in seen_keys, (
        "mcp_servers MUST be present in the update body even when emptied — "
        "an absent field is preserved by MA's partial update, re-adding the removed MCP"
    )
    assert captured["mcp_servers"] == [], (
        f"removing the last MCP must send mcp_servers=[]; got {captured.get('mcp_servers')!r}"
    )


# ---------------------------------------------------------------------------
# D-02 bare-name round-trip: strip on read, re-prefix on save
# ---------------------------------------------------------------------------


async def test_build_custom_skill_title_map_strips_to_bare_names_and_excludes_foreign_skills(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Map built via load_tenant_roster holds BARE names for caller's tenant only.

    Tenant A's seeded skill (cli-auth), tenant A's synced skill (daimon/x),
    tenant B's skill, and an anthropic built-in → map contains exactly the
    two bare names from tenant A; tenant B and anthropic built-in are absent.
    """
    from anthropic.types.beta import SkillListResponse
    from daimon.core.defaults.metadata import tenant_scoped_display_title

    tenant_b_id = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000000")

    # Canonical titles — the real display_title stored in MA
    title_a_seeded = tenant_scoped_display_title(tenant_id=tenant_id, name="cli-auth")
    title_a_synced = tenant_scoped_display_title(tenant_id=tenant_id, name="daimon/x")
    title_b = tenant_scoped_display_title(tenant_id=tenant_b_id, name="cli-auth")

    skills_payload = [
        SkillListResponse(
            id="sk_a_seed",
            type="skill",
            display_title=title_a_seeded,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
        SkillListResponse(
            id="sk_a_sync",
            type="skill",
            display_title=title_a_synced,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
        SkillListResponse(
            id="sk_b",
            type="skill",
            display_title=title_b,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
        SkillListResponse(
            id="sk_anthropic",
            type="skill",
            display_title="anthropic-builtin",
            latest_version="1",
            source="anthropic",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
    ]
    # Agent pinning all four skills; only A's two will survive into the roster
    agent_payload = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="myagent",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "myagent",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[
            {"type": "custom", "skill_id": "sk_a_seed", "version": "1"},  # type: ignore[list-item]
            {"type": "custom", "skill_id": "sk_a_sync", "version": "1"},  # type: ignore[list-item]
            {"type": "custom", "skill_id": "sk_b", "version": "1"},  # type: ignore[list-item]
            {"type": "anthropic", "skill_id": "sk_anthropic", "version": "1"},  # type: ignore[list-item]
        ],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": skills_payload, "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    roster = await load_tenant_roster(client, tenant_id=tenant_id)

    assert len(roster) == 1, "one agent expected"
    skill_ids = {s.skill_id for s in roster[0].spec.skills}

    assert "cli-auth" in skill_ids, "tenant A's seeded bare name must appear in skill refs"
    assert "daimon/x" in skill_ids, "tenant A's synced bare name must appear in skill refs"
    # B's skill_id "sk_b" is not in the map → dangling-ref skip → absent
    assert not any(sid.startswith(f"{str(tenant_b_id)[:8]}-") for sid in skill_ids), (
        "tenant B's skill must be absent from roster — strip_tenant_prefix returns None"
    )
    # anthropic type goes through directly (not filtered by the custom map)
    assert "sk_anthropic" in skill_ids, (
        "anthropic-type skill pin passes through unchanged (type='anthropic' branch)"
    )


async def test_build_roster_entry_drops_foreign_tenant_skill_pin_with_warning(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Foreign-tenant pin is dropped via the dangling-ref skip branch with a warning.

    An agent pinning A's skill and B's skill → SkillRef list contains the bare
    name for A's skill; B's pin is absent from the roster entry and emits a
    panel.skill_ref_dropped warning (capture_logs).
    """
    from anthropic.types.beta import SkillListResponse
    from daimon.core.defaults.metadata import tenant_scoped_display_title

    tenant_b_id = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000000")

    title_a = tenant_scoped_display_title(tenant_id=tenant_id, name="cli-auth")
    title_b = tenant_scoped_display_title(tenant_id=tenant_b_id, name="cli-auth")

    skills_payload = [
        SkillListResponse(
            id="sk_a",
            type="skill",
            display_title=title_a,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
        SkillListResponse(
            id="sk_b",
            type="skill",
            display_title=title_b,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
    ]
    # Agent pins both skills: A's and B's
    agent_payload = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="myagent",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "myagent",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[
            {"type": "custom", "skill_id": "sk_a", "version": "1"},  # type: ignore[list-item]
            {"type": "custom", "skill_id": "sk_b", "version": "1"},  # type: ignore[list-item]
        ],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": skills_payload, "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)

    with structlog.testing.capture_logs() as captured:
        roster = await load_tenant_roster(client, tenant_id=tenant_id)

    assert len(roster) == 1, "one agent expected"
    skills = roster[0].spec.skills
    assert len(skills) == 1, (
        "only tenant A's skill pin must survive — B's pin is dropped by the dangling-ref branch"
    )
    assert skills[0].skill_id == "cli-auth", (
        "retained skill must carry the BARE authoring name, not the canonical title"
    )

    # B's pin must have triggered the panel.skill_ref_dropped warning
    dropped_warnings = [r for r in captured if r.get("event") == "panel.skill_ref_dropped"]
    assert len(dropped_warnings) == 1, (
        "exactly one panel.skill_ref_dropped warning expected (B's foreign-tenant pin)"
    )
    assert dropped_warnings[0]["skill_id"] == "sk_b", (
        "the dropped warning must identify B's skill_id"
    )


async def test_replace_agent_resources_for_panel_round_trips_bare_name_to_canonical_title(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Save round-trip: bare-named skill ref in panel state → canonical title in MA request.

    Panel state holds bare name 'cli-auth' (stripped on read). The save path
    calls resolve_refs(tenant_id=...) which prefixes internally to produce the
    canonical title. The outbound agents.update body must carry the MA skill_id
    resolved from the canonical title (via the skills.list lookup), not the bare name.
    """
    from anthropic.types.beta import SkillListResponse
    from daimon.core.defaults.metadata import tenant_scoped_display_title
    from daimon.core.specs import SkillRef

    canonical_title = tenant_scoped_display_title(tenant_id=tenant_id, name="cli-auth")
    ma_skill_id = "skill_abc123"

    # MA agent shape (what find_agent_by_daimon_tag returns)
    ma_agent_json = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="myfork",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "myfork",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[{"type": "custom", "skill_id": ma_skill_id, "version": "1"}],  # type: ignore[list-item]
        tools=[],
        system=None,
    ).model_dump(mode="json")

    # skills.list returns the skill with canonical title
    skills_payload = [
        SkillListResponse(
            id=ma_skill_id,
            type="skill",
            display_title=canonical_title,
            latest_version="1",
            source="custom",
            created_at="2026-06-01T00:00:00Z",
            updated_at="2026-06-01T00:00:00Z",
        ).model_dump(mode="json"),
    ]

    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [ma_agent_json], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents/ag_1":
            return httpx.Response(200, json=ma_agent_json)
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": skills_payload, "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents/ag_1":
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json=ma_agent_json)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = build_stub_anthropic(handler)
    runtime = _runtime_with_settings(client, tenant_id=tenant_id, public_url=None)

    # Panel state holds the BARE name (as produced by load_tenant_roster after our change)
    spec = AgentSpec.model_validate(
        {
            "name": "myfork",
            "model": "claude-sonnet-4-6",
            "skills": [SkillRef(type="custom", skill_id="cli-auth")],
        }
    )
    entry = RosterEntry(name="myfork", model="claude-sonnet-4-6", spec=spec, ma_agent_id="ag_1")
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    outcome = await replace_agent_resources_for_panel(runtime, state, tenant_id=tenant_id)

    assert outcome.action is Action.UPDATED, "save must report UPDATED"
    sent_skills = captured_body.get("skills", [])
    assert len(sent_skills) == 1, "exactly one skill must be in the outbound request"
    # resolve_refs resolved the bare name 'cli-auth' → canonical title → MA skill_id
    assert sent_skills[0].get("skill_id") == ma_skill_id, (
        "the outbound MA request must carry the MA skill_id resolved from the canonical title, "
        "proving bare-name-in → canonical-lookup → MA-id-out (lossless round-trip)"
    )


# ---------------------------------------------------------------------------
# #144-2: replace_agent_resources_for_panel retry on version conflict
# ---------------------------------------------------------------------------


async def test_replace_agent_resources_retries_once_on_version_conflict(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """#144-2: replace_agent_resources_for_panel retries once on a 409 version conflict.

    Router: list returns the agent; first update returns 409; second retrieve
    returns the agent again; second update returns 200.
    Assert: outcome is UPDATED, exactly two update attempts, and a second retrieve fired.
    """
    ma_agent_json = BetaManagedAgentsAgent(
        id="ag_retry",
        type="agent",
        name="retry-bot",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "retry-bot",
            "daimon_account": str(account_id),
        },
        description=None,
        archived_at=None,
        created_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-06-01T00:00:00Z",  # type: ignore[arg-type]
        version=3,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")
    updated_json = {**ma_agent_json, "version": 4}

    retrieve_count = 0
    update_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal retrieve_count, update_count
        # list (find_agent_by_daimon_tag)
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [ma_agent_json], "next_page": None})
        # retrieve (update_agent_with_version_retry internal)
        if request.method == "GET" and request.url.path == "/v1/agents/ag_retry":
            retrieve_count += 1
            return httpx.Response(200, json=ma_agent_json)
        # skills list (resolve_refs)
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        # update — conflict once, then succeed
        if request.method == "POST" and request.url.path == "/v1/agents/ag_retry":
            update_count += 1
            if update_count == 1:
                return httpx.Response(
                    409,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": (
                                "Concurrent modification detected. "
                                "Please fetch the latest version and retry."
                            ),
                        },
                    },
                )
            return httpx.Response(200, json=updated_json)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    # max_retries=0: SDK auto-retries 409 by default; disable so our helper logic fires.
    import httpx as httpx_mod

    transport = httpx_mod.MockTransport(handler)
    http_client_inner = httpx_mod.AsyncClient(
        transport=transport, base_url="https://api.anthropic.com"
    )
    from anthropic import AsyncAnthropic

    no_retry_client = AsyncAnthropic(api_key="test", http_client=http_client_inner, max_retries=0)
    runtime = _runtime_with_settings(no_retry_client, tenant_id=tenant_id, public_url=None)

    spec = AgentSpec.model_validate({"name": "retry-bot", "model": "claude-sonnet-4-6"})
    entry = RosterEntry(name="retry-bot", model="claude-sonnet-4-6", spec=spec)
    state = PanelState(roster=[entry], selected=entry, account_id=account_id)

    outcome = await replace_agent_resources_for_panel(runtime, state, tenant_id=tenant_id)

    assert outcome.action is Action.UPDATED, (
        "#144-2: replace_agent_resources_for_panel must report UPDATED after successful retry"
    )
    assert update_count == 2, (
        f"#144-2: exactly two update attempts expected (conflict + retry); got {update_count}"
    )
    assert retrieve_count == 2, (
        f"#144-2: second retrieve must fire after conflict so the closure gets the fresh agent; "
        f"got {retrieve_count}"
    )
