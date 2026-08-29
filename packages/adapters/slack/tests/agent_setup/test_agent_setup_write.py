"""Real-Postgres tests for agent_setup/write.py.

Covers:
- do_propagate persists agent_name at the scope (set_fields); second call returns prior name
- do_unpropagate clears the agent_name (unset_fields)
- mask_tail covers the full-length and short-string cases
- fork_agent copies credential + repo binding via core.agent_lifecycle (mirrors Discord)
- delete_agent archives the memory store via core.agent_lifecycle (mirrors Discord)
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import write as write_mod
from daimon.adapters.slack.agent_setup.write import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    load_agent_inline_pat,
    mask_tail,
    owner_repo_from_url,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, get_pat, upsert_credential_encrypted
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.github_credentials import delete_credential_for_principal
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    NotHandled,
    build_fake_anthropic,
    build_stub_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_WRITE_TESTS"
_AGENT_NAME = "my-agent"
_OTHER_AGENT_NAME = "other-agent"
_CHANNEL_ID = "C_WRITE_TESTS"


async def _seed_tenant(session: AsyncSession, team_id: str = _TEAM_ID) -> uuid.UUID:
    """Create a Tenant row and return the derived tenant_id."""
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    return tenant.id


async def _seed_account(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Create an Account row and return its id."""
    tenant_row = await get_tenant(session, tenant_id)
    assert tenant_row is not None, "_seed_account requires a tenant seeded via _seed_tenant"
    account = await make_account(session, tenant=tenant_row)
    return account.id


# ---------------------------------------------------------------------------
# mask_tail (pure, no DB)
# ---------------------------------------------------------------------------


def test_mask_tail_returns_last4_chars_when_secret_is_long_enough() -> None:
    result = mask_tail("ghp_abcd1234")
    assert result == "****1234", "mask_tail should render ****<last4> for secrets >= 4 chars"


def test_mask_tail_returns_four_stars_when_secret_is_shorter_than_four_chars() -> None:
    result = mask_tail("xy")
    assert result == "****", "mask_tail should return **** for secrets shorter than 4 chars"


def test_mask_tail_returns_four_stars_for_empty_string() -> None:
    result = mask_tail("")
    assert result == "****", "mask_tail should return **** for empty string"


# ---------------------------------------------------------------------------
# load_agent_inline_pat / owner_repo_from_url
# ---------------------------------------------------------------------------


def _runtime_for_inline_pat(
    *,
    sessionmaker: Any,
    fernet_key: str | None,
    fallback_pat: str | None = None,
) -> SlackRuntime:
    """Build a SlackRuntime with just enough settings for load_agent_inline_pat."""
    settings = MagicMock()
    settings.crypto.keys = (
        (MagicMock(get_secret_value=lambda: fernet_key),) if fernet_key is not None else ()
    )
    settings.github.oauth_scopes = ("repo", "read:user")
    settings.github.fallback_pat = (
        MagicMock(get_secret_value=lambda: fallback_pat) if fallback_pat is not None else None
    )
    return SlackRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(),
    )


async def test_load_agent_inline_pat_returns_none_when_crypto_unconfigured() -> None:
    """No crypto keys -> no inline PAT could exist; the sessionmaker must never
    be touched (calling _build_runtime_fernet unconditionally would raise)."""
    agent_id = uuid.uuid4()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sessionmaker must not be called when crypto is unconfigured")

    runtime = _runtime_for_inline_pat(sessionmaker=_boom, fernet_key=None)

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result is None, "no crypto keys configured -> no inline PAT can exist"


async def test_load_agent_inline_pat_returns_stored_pat_for_agent_that_has_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trips through store_inline_pat -> load_agent_inline_pat, decrypted."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_LOAD")

    fernet_key = Fernet.generate_key().decode()
    plaintext = "ghp_slack_inline_pat_load_9999"
    runtime = _runtime_for_inline_pat(sessionmaker=db_session_factory, fernet_key=fernet_key)

    agent_id = uuid.uuid4()
    await write_mod.store_inline_pat(
        runtime,
        account_id=uuid.uuid4(),
        agent_id=agent_id,
        plaintext_pat=plaintext,
    )

    result = await load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result == plaintext, "load_agent_inline_pat must decrypt and return the exact stored PAT"


async def test_load_agent_inline_pat_returns_none_for_agent_with_no_token_even_with_fallback_configured(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """allow_service_default=False must be honored: an agent with no stored
    token of its own gets None, never the deployment's shared fallback PAT —
    this is the check that stops the shared public-read token from being
    treated as the agent's own credential."""
    await make_tenant(db_session, platform="slack", workspace_id="T_INLINE_PAT_NO_FALLBACK")

    fernet_key = Fernet.generate_key().decode()
    runtime = _runtime_for_inline_pat(
        sessionmaker=db_session_factory,
        fernet_key=fernet_key,
        fallback_pat="ghp_shared_operator_fallback",
    )

    result = await load_agent_inline_pat(runtime, agent_id=uuid.uuid4())
    assert result is None, (
        "an agent with no stored token must resolve to None, not the shared fallback PAT"
    )


def test_owner_repo_from_url_collapses_scheme_host_and_git_variants() -> None:
    canonical = "acme-org/widgets"
    variants = [
        "https://github.com/acme-org/widgets",
        "http://github.com/acme-org/widgets",
        "github.com/acme-org/widgets",
        "https://github.com/acme-org/widgets.git",
        "https://github.com/acme-org/widgets/",
        "acme-org/widgets",
    ]
    for variant in variants:
        assert owner_repo_from_url(variant) == canonical, (
            f"owner_repo_from_url({variant!r}) must canonicalize to {canonical!r}"
        )


def test_owner_repo_from_url_matches_store_normalization() -> None:
    """Must stay byte-identical to the store's own normalization — a probe run
    against a differently-canonicalized string would verify a different repo
    than the one the binding actually records."""
    from daimon.core.stores.agent_repo_binding import (
        _normalize_owner_repo,  # pyright: ignore[reportPrivateUsage]
    )

    for url in [
        "https://github.com/acme-org/widgets.git",
        "github.com/acme-org/widgets/",
        "acme-org/widgets",
    ]:
        assert owner_repo_from_url(url) == _normalize_owner_repo(url), (
            "owner_repo_from_url must stay byte-identical to the store's normalization"
        )


# ---------------------------------------------------------------------------
# do_propagate — persists scope write and returns prior state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_propagate_persists_agent_name_at_tenant_scope(
    db_session: AsyncSession,
) -> None:
    """do_propagate stamps agent_name at TenantScopeRef; get_scope shows the persisted value."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    assert isinstance(result, PropagateResult), "do_propagate should return PropagateResult"
    assert result.prior_agent_name is None, "clean propagation should have no prior agent name"

    row = await get_scope(db_session, scope=scope)
    from daimon.core.scope import TenantConfigRow

    assert isinstance(row, TenantConfigRow), (
        "get_scope should return a TenantConfigRow after propagate"
    )
    assert row.agent_name == _AGENT_NAME, "propagated agent_name should be persisted at the scope"
    assert row.agent_name_set_by_account_id == account_id, (
        "actor account_id should be recorded for audit"
    )


@pytest.mark.asyncio
async def test_do_propagate_returns_prior_agent_name_on_overwrite(
    db_session: AsyncSession,
) -> None:
    """Second do_propagate returns the prior agent name (last-write-wins audit trail)."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)
    second_account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)

    # First propagation — clean write
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    # Second propagation — overwrite; prior name should surface
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_OTHER_AGENT_NAME,
        actor_account_id=second_account_id,
    )

    assert result.prior_agent_name == _AGENT_NAME, (
        "do_propagate should return the agent_name that was overwritten"
    )
    assert result.prior_actor_account_id == account_id, (
        "do_propagate should return the actor who set the prior value"
    )


# ---------------------------------------------------------------------------
# do_unpropagate — clears agent_name at scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_unpropagate_clears_agent_name_at_scope(
    db_session: AsyncSession,
) -> None:
    """do_unpropagate removes agent_name so the row becomes effectively empty."""
    tenant_id = await _seed_tenant(db_session)
    account_id = await _seed_account(db_session, tenant_id)

    scope = TenantScopeRef(tenant_id=tenant_id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=_AGENT_NAME,
        actor_account_id=account_id,
    )

    await do_unpropagate(db_session, scope=scope, actor_account_id=account_id)

    row = await get_scope(db_session, scope=scope)
    from daimon.core.scope import TenantConfigRow

    # After unpropagate, either the row is gone (None) or agent_name is None
    if isinstance(row, TenantConfigRow):
        assert row.agent_name is None, "do_unpropagate should clear agent_name from the scope row"
    else:
        assert row is None, (
            "do_unpropagate should leave the scope empty (row deleted or agent_name None)"
        )


# ---------------------------------------------------------------------------
# fork_agent / delete_agent — core.agent_lifecycle repoint (mirrors Discord)
# ---------------------------------------------------------------------------


def _agent_dict(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Build a real BetaManagedAgentsAgent and dump to JSON for the MockTransport."""
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": name,
    }
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    return BetaManagedAgentsAgent(
        id=id_,
        type="agent",
        name=name,
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


def _fork_handler(
    *,
    source_payload: dict[str, Any],
    fork_id: str,
    fork_name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    created: list[dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
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


def _runtime_with_db(
    anthropic: Any,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet_key: str,
) -> SlackRuntime:
    """Build a SlackRuntime with a real sessionmaker + crypto keys for fork/delete tests."""
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo", "read:user")
    return SlackRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(),
    )


async def test_fork_agent_rekeys_source_credential_onto_fork(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After fork_agent, get_pat(agent_id=fork) resolves the source's token,
    re-keyed under the fork's OWN principal."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_FORK_CRED")
    tenant_id = tenant.id
    account_id = await _seed_account(db_session, tenant_id)

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
            proof=None,
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

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_name="source",
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

    # Deleting the source credential must not break the fork (no aliasing).
    await delete_credential_for_principal(db_session, principal_id=source_agent_uuid)
    await db_session.commit()
    fork_pat_after_delete = await get_pat(
        principal_id=fork_agent_uuid,
        agent_id=fork_agent_uuid,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert fork_pat_after_delete == plaintext, (
        "fork's credential must survive deletion of the source credential (no aliasing)"
    )


async def test_fork_agent_raises_when_source_credential_unresolvable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fork_agent fails loud when the source's inline-pat binding has no
    resolvable credential (binding row exists, credential row does not)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_FORK_NOCRED")
    tenant_id = tenant.id
    account_id = await _seed_account(db_session, tenant_id)

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
            proof=None,
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

    with pytest.raises(DaimonError, match="github git-proxy"):
        await write_mod.fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_name="source",
            new_name="myfork2",
            account_id=account_id,
        )


async def test_fork_agent_copies_anon_binding_without_error_or_credential_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A public/anon: source binding forks with no error and no credential write;
    the fork's binding carries the same repo with ma_secret_ref copied verbatim."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_FORK_ANON")
    tenant_id = tenant.id
    account_id = await _seed_account(db_session, tenant_id)

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
            proof=None,
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

    await write_mod.fork_agent(
        runtime,
        tenant_id=tenant_id,
        source_name="source",
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


def _make_archive_agent_handler() -> Callable[[httpx.Request], httpx.Response]:
    """`make_fake_ma_handler` doesn't implement POST .../archive — add it here."""

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or not m:
            raise NotHandled
        now = datetime.now(UTC).isoformat()
        return httpx.Response(
            200,
            json={
                "id": m.group("id"),
                "type": "agent",
                "name": "doomed",
                "version": 2,
                "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
                "system": None,
                "metadata": {},
                "mcp_servers": [],
                "tools": [],
                "skills": [],
                "created_at": now,
                "updated_at": now,
                "archived_at": now,
                "description": None,
            },
        )

    return handler


async def test_delete_agent_archives_memory_store(db_session, db_session_factory) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_MEM")
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(
            _make_archive_agent_handler(),
            make_fake_memory_store_handler(mem_state),
            make_fake_ma_handler(),
        )
    )

    agent = await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")

    from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store

    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    assert mem_state.stores[store.id]["archived_at"] is not None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) is None


def _make_failing_store_archive_handler() -> Callable[[httpx.Request], httpx.Response]:
    """500 on memory-store archive — simulates a transient MA outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and re.fullmatch(
            r"/v1/memory_stores/[^/]+/archive", request.url.path
        ):
            return httpx.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
            )
        raise NotHandled

    return handler


async def test_delete_agent_succeeds_when_store_archive_fails(
    db_session, db_session_factory
) -> None:
    """Transient store-archive failure must not fail agent deletion (best-effort degrade)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_FAIL")
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(
            _make_archive_agent_handler(),
            _make_failing_store_archive_handler(),
            make_fake_memory_store_handler(mem_state),
            make_fake_ma_handler(),
        )
    )

    agent = await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")

    from daimon.core.stores.agent_memory_stores import get_memory_store_id, insert_memory_store

    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    # Must not raise despite the 500 from the store archive.
    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    assert mem_state.stores[store.id]["archived_at"] is None
    async with db_session_factory() as s:
        assert await get_memory_store_id(s, tenant_id=tenant.id, agent_id=agent_uuid) == store.id


async def test_delete_agent_clears_tenant_default_naming_the_agent(
    db_session, db_session_factory
) -> None:
    """Deleting the workspace default must not leave a tenant row naming a dead agent.

    A tenant_config row pointing at an archived agent breaks resolution for the
    whole install until an admin re-scopes by hand.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_SCOPE")
    scope = TenantScopeRef(tenant_id=tenant.id)
    await set_fields(db_session, scope=scope, tenant_id=tenant.id, agent_name="doomed")
    await db_session.commit()

    client = build_fake_anthropic(
        combine_handlers(
            _make_archive_agent_handler(),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    await client.beta.agents.create(
        name="doomed",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "doomed"},
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="doomed")

    async with db_session_factory() as s:
        assert await get_scope(s, scope=scope) is None, (
            "the tenant row naming the deleted agent must be gone so resolution "
            "falls through to the deployment default"
        )


# ---------------------------------------------------------------------------
# delete_agent — server-side refusal for defaults-managed ("system") agents
# ---------------------------------------------------------------------------


def _make_recording_archive_handler(
    archived_ids: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Archive handler that records which agent ids MA was actually asked to archive.

    Lets a test assert the guard fired *before* the archive call, not merely
    that `delete_agent` raised.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or not m:
            raise NotHandled
        archived_ids.append(m.group("id"))
        now = datetime.now(UTC)
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=m.group("id"),
                type="agent",
                name="doomed",
                model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                metadata={},
                description=None,
                archived_at=now,
                created_at=now,
                updated_at=now,
                version=2,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    return handler


async def test_delete_agent_refuses_defaults_managed_agent(db_session, db_session_factory) -> None:
    """A workspace admin must not be able to archive the deployment's built-in agent.

    The Slack roster carries no is_system flag, so the panel offers Delete for
    seeded agents — the refusal has to live server-side or the deployment's
    agent (and its memory store) go away in two clicks.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_MANAGED")
    archived_ids: list[str] = []
    client = build_fake_anthropic(
        combine_handlers(
            _make_recording_archive_handler(archived_ids),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "daimon",
            "daimon_managed": "true",
        },
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    with pytest.raises(DaimonError, match="built-in agent"):
        await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="daimon")

    assert archived_ids == [], (
        "delete_agent must refuse a daimon_managed agent before calling agents.archive"
    )


async def test_delete_agent_still_archives_unmanaged_agent(db_session, db_session_factory) -> None:
    """The managed guard must not over-block: user forks still delete normally."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_DELETE_UNMANAGED")
    archived_ids: list[str] = []
    client = build_fake_anthropic(
        combine_handlers(
            _make_recording_archive_handler(archived_ids),
            make_fake_memory_store_handler(FakeMemoryStoreState()),
            make_fake_ma_handler(),
        )
    )
    agent = await client.beta.agents.create(
        name="my-fork",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "my-fork"},
    )

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory

    await write_mod.delete_agent(runtime, tenant_id=tenant.id, name="my-fork")

    assert archived_ids == [agent.id], (
        "an agent without the daimon_managed marker must still be archived"
    )
