"""ISO-03 / SC-4: cross-tenant data-boundary proof in the derived-identity world.

Two guild-tenants are provisioned via `provision_tenant`, so each carries the
DERIVED `derive_tenant_uuid(platform, workspace_id)` tenant id (not an arbitrary
earliest row). An MA fake serves only tenant A's agent/environment (tagged
`daimon_tenant == str(tenant_a)`). The assertions prove the `daimon_tenant`
metadata filter is the isolation boundary: tenant A's resolvers return its own
resources; tenant B's lookups return nothing and `resolve_agent` for tenant B
raises `MAResolverMissError` — no agent, environment, vault, or session (all
keyed by the same metadata) crosses the boundary.
"""

from __future__ import annotations

import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_environment_by_daimon_tag,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError, new_resolver_cache, resolve_agent
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_cross_tenant_find_agent_returns_own_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result_a = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-aaa"
    )
    result_b = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-bbb"
    )
    assert result_a.tenant_id != result_b.tenant_id, (
        "two distinct guilds must derive distinct tenant ids"
    )
    assert result_a.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="guild-aaa"), (
        "tenant A id must be the derived identity, not an arbitrary row"
    )
    assert result_b.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="guild-bbb"), (
        "tenant B id must be the derived identity, not an arbitrary row"
    )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                BetaManagedAgentsAgent(
                    id="ag_a",
                    type="agent",
                    name="daimon",
                    model={"id": "claude-opus-4-7"},  # pyright: ignore[reportArgumentType]
                    metadata={
                        "daimon_tenant": str(result_a.tenant_id),
                        "daimon_name": "daimon",
                    },
                    description=None,
                    created_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                    updated_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    own = await find_agent_by_daimon_tag(client, tenant_id=result_a.tenant_id, name="daimon")
    assert own is not None and own.id == "ag_a", "tenant A must see its own agent"

    cross = await find_agent_by_daimon_tag(client, tenant_id=result_b.tenant_id, name="daimon")
    assert cross is None, "tenant B must not see tenant A's agent (daimon_tenant filter)"


async def test_cross_tenant_find_environment_returns_own_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result_a = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-aaa"
    )
    result_b = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-bbb"
    )
    assert result_a.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="guild-aaa"), (
        "tenant A id must be the derived identity"
    )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response(
            [
                BetaEnvironment(
                    id="env_a",
                    type="environment",
                    name="default",
                    config=EMPTY_CLOUD_CONFIG,
                    metadata={
                        "daimon_tenant": str(result_a.tenant_id),
                        "daimon_name": "default",
                    },
                    description="",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    own = await find_environment_by_daimon_tag(client, tenant_id=result_a.tenant_id, name="default")
    assert own is not None and own.id == "env_a", "tenant A must see its own environment"

    cross = await find_environment_by_daimon_tag(
        client, tenant_id=result_b.tenant_id, name="default"
    )
    assert cross is None, (
        "tenant B must not see tenant A's environment — the same daimon_tenant "
        "filter that isolates vault/session resources"
    )


async def test_cross_tenant_resolve_agent_misses_for_other_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result_a = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-aaa"
    )
    result_b = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-bbb"
    )
    assert result_a.tenant_id == derive_tenant_uuid(platform="discord", workspace_id="guild-aaa"), (
        "tenant A id must be the derived identity"
    )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                BetaManagedAgentsAgent(
                    id="ag_a",
                    type="agent",
                    name="daimon",
                    model={"id": "claude-opus-4-7"},  # pyright: ignore[reportArgumentType]
                    metadata={
                        "daimon_tenant": str(result_a.tenant_id),
                        "daimon_name": "daimon",
                    },
                    description=None,
                    created_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                    updated_at="2026-04-21T00:00:00Z",  # pyright: ignore[reportArgumentType]
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    # No-op apply_callable so the resolver's self-heal step is a clean no-op
    # (does not reconcile a new agent into tenant B); the post-apply retry then
    # misses again and the resolver raises. This isolates the daimon_tenant
    # filter as the property under test.
    async def _noop_apply() -> None:
        return None

    with pytest.raises(MAResolverMissError):
        await resolve_agent(
            client,
            tenant_id=result_b.tenant_id,
            daimon_tag="daimon",
            cached_id=None,
            apply_callable=_noop_apply,
            cache=new_resolver_cache(),
        )
