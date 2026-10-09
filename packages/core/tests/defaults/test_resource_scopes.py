"""Tenant entry points preserve SDK requests without operator capabilities."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core import mux_compat
from daimon.core.defaults import ma_index
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import Action
from daimon.core.mux_backend import managed_agents
from daimon.core.specs import AgentSpec
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic import AnthropicManagedAgents

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
FOREIGN = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def observed_scopes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Scope]]:
    scopes: list[Scope] = []

    def record(
        client: AsyncAnthropic,
        *,
        scope: Scope | None = None,
        resources: frozenset[tuple[str, str]] = frozenset(),
    ) -> AnthropicManagedAgents:
        assert scope is not None
        scopes.append(scope)
        return managed_agents(client, scope=scope, resources=resources)

    monkeypatch.setattr(mux_compat, "managed_agents", record)
    yield scopes


def assert_tenant_scopes(scopes: list[Scope], count: int) -> None:
    assert len(scopes) == count
    for scope in scopes:
        assert scope.tenant_id == str(TENANT)
        assert not scope.is_platform
        assert not scope.is_legacy_host_authorized


@pytest.mark.parametrize(
    "entry_point,resource",
    [
        ("find_agents_by_daimon_tag", "agents"),
        ("find_environments_by_daimon_tag", "environments"),
        ("list_agents_by_tenant", "agents"),
        ("list_environments_by_tenant", "environments"),
    ],
)
async def test_tenant_index_uses_tenant_scope_and_preserves_pagination(
    entry_point: str, resource: str, observed_scopes: list[Scope]
) -> None:
    factory = ma_agent if resource == "agents" else ma_environment
    rows = [
        factory(
            id=f"{resource}-{tenant}",
            name="example",
            metadata={"daimon_tenant": str(tenant), "daimon_name": "example"},
        ).model_dump(mode="json")
        for tenant in (FOREIGN, TENANT)
    ]

    def transport() -> ScriptedTransport:
        result = ScriptedTransport()
        for index, row in enumerate(rows):
            result.queue(
                ScriptedReply(
                    "GET",
                    f"/v1/{resource}",
                    httpx.Response(
                        200,
                        json={"data": [row], "next_page": "second" if index == 0 else None},
                    ),
                )
            )
        return result

    old, new = transport(), transport()
    async with old.client() as before, new.client() as after:
        sdk = before.beta.agents if resource == "agents" else before.beta.environments
        expected = [
            row.model_dump(mode="json")
            async for row in sdk.list(include_archived=False)
            if row.metadata.get("daimon_tenant") == str(TENANT)
        ]
        if entry_point == "find_agents_by_daimon_tag":
            actual = await ma_index.find_agents_by_daimon_tag(
                after, tenant_id=TENANT, name="example"
            )
        elif entry_point == "find_environments_by_daimon_tag":
            actual = await ma_index.find_environments_by_daimon_tag(
                after, tenant_id=TENANT, name="example"
            )
        elif entry_point == "list_agents_by_tenant":
            actual = await ma_index.list_agents_by_tenant(after, tenant_id=TENANT)
        else:
            actual = await ma_index.list_environments_by_tenant(after, tenant_id=TENANT)
    old.assert_consumed()
    new.assert_consumed()
    assert new.requests == old.requests
    assert [row.model_dump(mode="json") for row in actual] == expected
    assert_tenant_scopes(observed_scopes, 1)


@pytest.mark.parametrize("existing", [False, True])
async def test_tenant_reconcile_scopes_create_update_retry_and_duplicate_archive(
    existing: bool, observed_scopes: list[Scope]
) -> None:
    def agent(native_id: str, tenant: uuid.UUID = TENANT, day: int = 2):
        return ma_agent(
            id=native_id,
            name="example",
            metadata={"daimon_tenant": str(tenant), "daimon_name": "example"},
            created_at=datetime(2026, 1, day, tzinfo=UTC),
        ).model_dump(mode="json")

    canonical = agent("canonical")
    duplicate = agent("duplicate", day=1)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [agent("foreign", FOREIGN), duplicate, canonical] if existing else []
        ),
    )
    router.add("GET", r"/v1/agents/canonical", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add("POST", r"/v1/agents/canonical", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add("POST", r"/v1/agents", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add(
        "POST", r"/v1/agents/duplicate/archive", lambda _r, _m: httpx.Response(200, json=duplicate)
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        result = await reconcile_agent(
            client,
            AgentSpec(name="example", model="claude-sonnet-4-6"),
            tenant_id=TENANT,
            dry_run=False,
        )
    transport.assert_consumed()
    assert result.action == (Action.UPDATED if existing else Action.CREATED)
    expected = [("GET", "/v1/agents")]
    if existing:
        expected += [
            ("POST", "/v1/agents/duplicate/archive"),
            ("GET", "/v1/agents/canonical"),
            ("POST", "/v1/agents/canonical"),
        ]
    else:
        expected += [("POST", "/v1/agents")]
    assert [(r.method, r.path) for r in transport.requests] == expected
    assert_tenant_scopes(observed_scopes, len(expected))
