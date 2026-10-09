"""Scoped skill results obey the grant without changing SDK pagination."""

from collections import deque

import httpx
import pytest
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import PageRequest, Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.walk import ResourceWalk
from mux.errors import ScopeViolation

TENANT = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
)


def skill(skill_id, source="custom"):
    return {
        "id": skill_id,
        "source": source,
        "type": "skill",
        "display_title": skill_id,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "latest_version": None,
    }


def transport(pages):
    return ScriptedTransport(
        deque(ScriptedReply("GET", "/v1/skills", httpx.Response(200, json=page)) for page in pages)
    )


def driver(client):
    return AnthropicManagedAgents(
        client, authorization=ResourceAuthorization(TENANT, frozenset({("skill", "owned")}))
    )


@pytest.mark.parametrize(
    "scope",
    [
        TENANT,
        Scope.platform(reason="test workspace inventory"),
        Scope.legacy_host_authorized(call_site="test legacy host"),
    ],
)
async def test_list_and_page_walk_include_only_owned_custom_and_shared_catalog(scope):
    rows = [skill("owned"), skill("foreign"), skill("catalog", "anthropic")]
    sdk = transport([{"data": rows, "next_page": None}] * 2)
    async with sdk.client() as client:
        backend = driver(client)
        page = await backend.skills.list(scope, page=PageRequest(limit=1000))
        walk = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
        pages = [page async for page in walk.skill_pages(scope, limit=1000)]
        expected = (
            ["owned", "foreign", "catalog"]
            if scope.is_platform or scope.is_legacy_host_authorized
            else ["owned", "catalog"]
        )
        assert [record.id for record in page.data] == expected
        assert [[record.id for record in page.data] for page in pages] == [expected]
        with pytest.raises(ScopeViolation):
            await backend.skills.retrieve(TENANT, "foreign")
    assert len(sdk.requests) == 2
    sdk.assert_consumed()


async def test_unowned_only_page_does_not_stop_the_original_sdk_walk():
    pages = [
        {"data": [skill("foreign")], "next_page": "second"},
        {"data": [skill("owned"), skill("catalog", "anthropic")], "next_page": None},
    ]
    old, new = transport(pages), transport(pages)
    async with old.client() as before, new.client() as after:
        first = await before.beta.skills.list(limit=1000)
        expected_pages = [page async for page in first.iter_pages()]
        walk = driver(after).extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
        actual_pages = [page async for page in walk.skill_pages(TENANT, limit=1000)]
    assert [[record.id for record in page.data] for page in actual_pages] == [
        [],
        ["owned", "catalog"],
    ]
    assert actual_pages[0].next_cursor == expected_pages[0].next_page == "second"
    assert actual_pages[0].has_more
    assert not actual_pages[1].has_more
    assert new.requests == old.requests
    assert len(new.requests) == 2
    old.assert_consumed()
    new.assert_consumed()


async def test_filtered_empty_generic_page_preserves_provider_cursor():
    sdk = transport([{"data": [skill("foreign")], "next_page": "second"}])
    async with sdk.client() as client:
        page = await driver(client).skills.list(TENANT, page=PageRequest(limit=1000))
    assert page.data == ()
    assert page.has_more
    assert page.next_cursor == "second"
    assert len(sdk.requests) == 1
    sdk.assert_consumed()
