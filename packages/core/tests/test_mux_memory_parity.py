"""Old SDK and memory port requests stay identical, including pagination/beta headers."""

import hashlib
from collections import deque
from typing import Any, Literal

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import PageRequest, ResourceRef, Revision, Scope
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.memory_stores import AnthropicMemoryStores, StoreCreate
from mux.errors import ScopeViolation, UnsupportedCapability

scope = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="authorized"
)
ref = ResourceRef(
    id="store",
    kind="memory_store",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id="tenant",
    account_id="account",
)
stamp = "2026-09-15T12:00:00Z"
store = {
    "id": "store",
    "type": "memory_store",
    "name": "test",
    "description": "desc",
    "metadata": {"daimon_tenant": "tenant"},
    "created_at": stamp,
    "updated_at": stamp,
}
memory = {
    "id": "memory",
    "type": "memory",
    "path": "/notes.md",
    "memory_store_id": "store",
    "content": "notes",
    "content_size_bytes": 5,
    "content_sha256": hashlib.sha256(b"notes").hexdigest(),
    "memory_version_id": "version",
    "created_at": stamp,
    "updated_at": stamp,
}


def script(method: str, path: str, body: dict[str, Any]) -> ScriptedTransport:
    return ScriptedTransport(deque([ScriptedReply(method, path, httpx.Response(200, json=body))]))


def port(client: AsyncAnthropic) -> AnthropicMemoryStores:
    return AnthropicMemoryStores(
        client, "workspace", ResourceAuthorization(scope, frozenset({("memory_store", "store")}))
    )


def same(old: ScriptedTransport, new: ScriptedTransport) -> None:
    old.assert_consumed()
    new.assert_consumed()
    assert [r.to_dict() for r in old.requests] == [r.to_dict() for r in new.requests]
    assert dict(new.requests[0].protocol_headers)["anthropic-beta"] == "agent-memory-2026-07-22"
    assert ("beta", "true") in new.requests[0].query


@pytest.mark.parametrize(
    "name,method,path,body",
    [
        ("create", "POST", "/v1/memory_stores", store),
        ("retrieve", "GET", "/v1/memory_stores/store", store),
        ("archive", "POST", "/v1/memory_stores/store/archive", store),
        ("delete", "DELETE", "/v1/memory_stores/store", {}),
        ("read", "GET", "/v1/memory_stores/store/memories/memory", memory),
        ("write", "POST", "/v1/memory_stores/store/memories", memory),
        ("list", "GET", "/v1/memory_stores", {"data": [store], "next_page": None}),
    ],
)
async def test_memory_operations_preserve_sdk_requests(
    name: Literal["create", "retrieve", "archive", "delete", "read", "write", "list"],
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    old, new = script(method, path, body), script(method, path, body)
    async with old.client() as before, new.client() as after:
        native = port(after)
        if name == "create":
            await before.beta.memory_stores.create(
                name="test", description="desc", metadata={"daimon_tenant": "tenant"}
            )
            actual = await native.create_native(
                scope,
                StoreCreate(name="test", description="desc", metadata={"daimon_tenant": "tenant"}),
                key="create",
            )
            assert actual.ref.tenant_id == "tenant"
        elif name == "retrieve":
            expected = await before.beta.memory_stores.retrieve("store")
            actual = await native.retrieve(scope, ref)
            assert actual.created_at == expected.created_at
        elif name in ("archive", "delete"):
            await getattr(before.beta.memory_stores, name)("store")
            await getattr(native, name)(scope, ref, key=name)
        elif name == "read":
            await before.beta.memory_stores.memories.retrieve(
                "memory", memory_store_id="store", view="full"
            )
            assert (await native.read(scope, ref, "memory")).content == "notes"
        elif name == "write":
            await before.beta.memory_stores.memories.create(
                "store", path="/notes.md", content="notes", view="full"
            )
            assert (
                await native.write(scope, ref, "/notes.md", "notes", expected=None, key="write")
            ).content == "notes"
        elif name == "list":
            await before.beta.memory_stores.list()
            actual = await native.list(scope, page=PageRequest())
            assert len(actual.data) == 1
    same(old, new)


@pytest.mark.parametrize(
    "rows,cursor",
    [([], "unused"), ([memory], ""), ([{"type": "memory_prefix", "path": "/folder/"}], None)],
)
async def test_memory_walk_preserves_sdk_stop_rules(
    rows: list[dict[str, Any]], cursor: str | None
) -> None:
    body = {"data": rows, "next_page": cursor}
    old, new = (
        script("GET", "/v1/memory_stores/store/memories", body),
        script("GET", "/v1/memory_stores/store/memories", body),
    )
    async with old.client() as before, new.client() as after:
        page = await before.beta.memory_stores.memories.list("store", path_prefix="/")
        expected = [item.path async for item in page]
        actual = [item.path async for item in port(after).walk(scope, ref, path_prefix="/")]
        assert actual == expected
    same(old, new)


async def test_foreign_and_conditional_requests_fail_before_io() -> None:
    sdk = ScriptedTransport()
    async with sdk.client() as client:
        native = port(client)
        foreign = ref.model_copy(update={"tenant_id": "foreign"})
        with pytest.raises(ScopeViolation):
            await native.read(scope, foreign, "memory")
        with pytest.raises(UnsupportedCapability):
            await native.write(
                scope, ref, "/notes.md", "notes", expected=Revision(local=1), key="write"
            )
    assert not sdk.requests


async def test_memory_store_list_filters_foreign_tags_and_ungranted_ids_without_more_io() -> None:
    foreign = {**store, "id": "foreign", "metadata": {"daimon_tenant": "foreign"}}
    ungranted = {**store, "id": "ungranted"}
    sdk = script(
        "GET", "/v1/memory_stores", {"data": [store, foreign, ungranted], "next_page": None}
    )
    async with sdk.client() as client:
        records = await port(client).list(scope, page=PageRequest())
        assert [item.ref.id for item in records.data] == ["store"]
    sdk.assert_consumed()
    assert len(sdk.requests) == 1


async def test_memory_creation_same_key_sends_twice_and_reconstructs_native_dates() -> None:
    sdk = ScriptedTransport(
        deque(
            ScriptedReply("POST", "/v1/memory_stores", httpx.Response(200, json=store))
            for _ in range(2)
        )
    )
    async with sdk.client() as client:
        native = port(client)
        for _ in range(2):
            created = await native.create_native(scope, StoreCreate(name="test"), key="same")
            assert created.created_at.isoformat() == "2026-09-15T12:00:00+00:00"
            assert created.ref.tenant_id == scope.tenant_id
    sdk.assert_consumed()
    assert len(sdk.requests) == 2
