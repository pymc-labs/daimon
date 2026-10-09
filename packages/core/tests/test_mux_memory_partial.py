"""M0 reads retain the SDK's partial-record semantics and exact native requests."""

import uuid
import warnings
from collections import deque
from typing import Any, Literal

import httpx
import pytest
from daimon.core import ma_resolver, memory_view
from daimon.core.mux_backend import resource_scope
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

TENANT = uuid.UUID(int=1)
SCOPE = resource_scope(tenant_id=str(TENANT))


def script(replies: list[tuple[str, str, dict[str, Any]]]) -> ScriptedTransport:
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(200, json=body))
            for method, path, body in replies
        )
    )


def same(old: ScriptedTransport, new: ScriptedTransport) -> None:
    old.assert_consumed()
    new.assert_consumed()
    assert [request.to_dict() for request in old.requests] == [
        request.to_dict() for request in new.requests
    ]


@pytest.mark.parametrize("body", [{"content": "hello"}, {"content": None}, {}])
async def test_public_memory_content_accepts_unused_missing_fields(body: dict[str, Any]) -> None:
    replies: list[tuple[str, str, dict[str, Any]]] = [
        (
            "GET",
            "/v1/memory_stores/store/memories",
            {"data": [{"type": "memory", "id": "m", "path": "/notes"}], "next_page": None},
        ),
        ("GET", "/v1/memory_stores/store/memories/m", body),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        page = await before.beta.memory_stores.memories.list("store", path_prefix="/")
        expected = None
        async for item in page:
            if item.type == "memory" and item.path == "/notes":
                full = await before.beta.memory_stores.memories.retrieve(
                    item.id, memory_store_id="store", view="full"
                )
                expected = full.content or ""
                break
        assert await memory_view.get_memory_content(after, "store", "/notes", scope=SCOPE) == (
            expected
        )
    same(old, new)


@pytest.mark.parametrize("operation", ["paths", "content", "missing"])
@pytest.mark.parametrize("stop", ["last-page", "empty-page-cursor", "empty-next-page"])
async def test_public_memory_reads_ignore_partial_prefix_and_unknown_rows(
    operation: Literal["paths", "content", "missing"],
    stop: Literal["last-page", "empty-page-cursor", "empty-next-page"],
) -> None:
    ignored = [{"type": "memory_prefix"}, {"type": "future_entry"}, {}]
    cursor = "next" if stop != "empty-next-page" else ""
    replies: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", "/v1/memory_stores/store/memories", {"data": ignored, "next_page": cursor})
    ]
    if stop != "empty-next-page":
        rows = [{"type": "memory", "id": "m", "path": "/notes"}] if stop == "last-page" else []
        replies.append(
            (
                "GET",
                "/v1/memory_stores/store/memories",
                {"data": rows, "next_page": "unused" if not rows else None},
            )
        )
    if operation == "content" and stop == "last-page":
        replies.append(("GET", "/v1/memory_stores/store/memories/m", {"content": "hello"}))
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        page = await before.beta.memory_stores.memories.list("store", path_prefix="/")
        if operation == "paths":
            expected = sorted([item.path async for item in page if item.type == "memory"])
            actual = await memory_view.list_memory_paths(after, "store", scope=SCOPE)
        else:
            selected = "/missing" if operation == "missing" else "/notes"
            expected = None
            async for item in page:
                if item.type == "memory" and item.path == selected:
                    full = await before.beta.memory_stores.memories.retrieve(
                        item.id, memory_store_id="store", view="full"
                    )
                    expected = full.content or ""
                    break
            actual = await memory_view.get_memory_content(after, "store", selected, scope=SCOPE)
        assert actual == expected
    same(old, new)


async def test_memory_path_listing_does_not_require_unused_memory_id() -> None:
    replies: list[tuple[str, str, dict[str, Any]]] = [
        (
            "GET",
            "/v1/memory_stores/store/memories",
            {"data": [{"type": "memory", "path": "/notes"}], "next_page": None},
        )
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        page = await before.beta.memory_stores.memories.list("store", path_prefix="/")
        expected = sorted([item.path async for item in page if item.type == "memory"])
        assert await memory_view.list_memory_paths(after, "store", scope=SCOPE) == expected
    same(old, new)


@pytest.mark.parametrize("operation", ["cached", "live"])
@pytest.mark.parametrize("unused", [{}, {"created_at": None}, {"created_at": "not-a-date"}])
async def test_public_environment_resolver_accepts_unused_missing_timestamp(
    operation: Literal["cached", "live"], unused: dict[str, Any]
) -> None:
    body = {
        "id": "env",
        "type": "environment",
        "name": "test",
        "metadata": {"daimon_tenant": str(TENANT)},
        "archived_at": None,
        **unused,
    }
    replies: list[tuple[str, str, dict[str, Any]]] = [("GET", "/v1/environments/env", body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.environments.retrieve("env")
        cache = ma_resolver.new_resolver_cache()
        if operation == "cached":
            actual = await ma_resolver.retrieve_environment_cached(after, cache, TENANT, "env")
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
            assert actual.model_dump(mode="json", exclude_unset=True) == expected.model_dump(
                mode="json", exclude_unset=True
            )
            assert actual.model_fields_set == expected.model_fields_set
            assert await ma_resolver.retrieve_environment_cached(after, cache, TENANT, "env") is (
                actual
            )
        else:

            async def no_apply() -> None:
                pytest.fail("the existing identity is live; defaults must not run")

            assert (
                await ma_resolver.resolve_environment(
                    after,
                    tenant_id=TENANT,
                    daimon_tag="test",
                    apply_callable=no_apply,
                    cache=cache,
                    cached_id="env",
                )
                == "env"
            )
            assert expected.archived_at is None
            assert expected.metadata["daimon_tenant"] == str(TENANT)
    same(old, new)


@pytest.mark.parametrize("operation", ["content", "entries", "environment"])
async def test_native_read_paths_keep_scope_denial_before_io(operation: str) -> None:
    from mux.contracts.ids import ResourceRef
    from mux.drivers.anthropic import AnthropicManagedAgents
    from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
    from mux.drivers.anthropic.resources.environments import EnvironmentReads
    from mux.drivers.anthropic.resources.memory_stores import MemoryStores
    from mux.errors import ScopeViolation

    kind = "environment" if operation == "environment" else "memory_store"
    sdk = ScriptedTransport()
    async with sdk.client() as client:
        backend = AnthropicManagedAgents(
            client,
            account_scope_id="workspace",
            authorization=ResourceAuthorization(SCOPE, frozenset({(kind, "resource")})),
        )
        ref = ResourceRef(
            id="resource",
            kind=kind,
            provider="anthropic",
            account_scope_id="workspace",
            tenant_id="foreign",
            account_id=SCOPE.account_id,
        )
        assert not sdk.requests  # Factory/extension construction makes no request.
        with pytest.raises(ScopeViolation):
            if operation == "environment":
                environments = backend.extension(
                    EnvironmentReads, namespace="anthropic.environment_reads", version=1
                )
                await environments.retrieve_native(SCOPE, ref)
            else:
                memories = backend.extension(
                    MemoryStores, namespace="anthropic.memory_stores", version=1
                )
                if operation == "content":
                    await memories.read_native(SCOPE, ref, "m")
                else:
                    _ = [row async for row in memories.walk_native(SCOPE, ref, path_prefix="/")]
    assert not sdk.requests


async def test_native_environment_read_keeps_post_fetch_tenant_check() -> None:
    from mux.errors import ScopeViolation

    replies: list[tuple[str, str, dict[str, Any]]] = [
        (
            "GET",
            "/v1/environments/env",
            {"id": "env", "metadata": {"daimon_tenant": "foreign"}},
        )
    ]
    sdk = script(replies)
    async with sdk.client() as client:
        with pytest.raises(ScopeViolation):
            await ma_resolver.retrieve_environment_cached(
                client, ma_resolver.new_resolver_cache(), TENANT, "env"
            )
    sdk.assert_consumed()
    assert len(sdk.requests) == 1


@pytest.mark.parametrize("body", [{"content": "hello"}, {"content": None}, {}])
async def test_compat_memory_read_retains_exact_partial_sdk_snapshot(body: dict[str, Any]) -> None:
    from daimon.core.mux_compat import read_memory

    replies: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", "/v1/memory_stores/store/memories/m", body)
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.memory_stores.memories.retrieve(
            "m", memory_store_id="store", view="full"
        )
        actual = await read_memory(after, "store", "m", scope=SCOPE)
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        assert actual.model_dump(mode="json", exclude_unset=True) == expected.model_dump(
            mode="json", exclude_unset=True
        )
        assert actual.model_fields_set == expected.model_fields_set
    same(old, new)


async def test_live_environment_read_does_not_warn_about_unused_invalid_timestamp() -> None:
    body = {
        "id": "env",
        "metadata": {"daimon_tenant": str(TENANT)},
        "archived_at": None,
        "created_at": "not-a-date",
    }
    replies: list[tuple[str, str, dict[str, Any]]] = [("GET", "/v1/environments/env", body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        with warnings.catch_warnings(record=True) as original_warnings:
            warnings.simplefilter("always")
            expected = await before.beta.environments.retrieve("env")
            assert expected.archived_at is None
            assert expected.metadata["daimon_tenant"] == str(TENANT)

        async def no_apply() -> None:
            pytest.fail("the identity is live")

        with warnings.catch_warnings(record=True) as migrated_warnings:
            warnings.simplefilter("always")
            assert (
                await ma_resolver.resolve_environment(
                    after,
                    tenant_id=TENANT,
                    daimon_tag="test",
                    apply_callable=no_apply,
                    cache=ma_resolver.new_resolver_cache(),
                    cached_id="env",
                )
                == "env"
            )
        assert original_warnings == migrated_warnings == []
    same(old, new)


@pytest.mark.parametrize(
    "body",
    [
        {"id": "store"},
        {"id": "store", "created_at": None, "updated_at": None, "archived_at": None},
        {"id": "store", "created_at": "2026-09-15T12:00:00Z"},
    ],
)
async def test_memory_creation_keeps_partial_native_timestamps(body: dict[str, Any]) -> None:
    from daimon.core.mux_compat import create_memory_store

    replies: list[tuple[str, str, dict[str, Any]]] = [("POST", "/v1/memory_stores", body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.memory_stores.create(name="test")
        actual = await create_memory_store(after, {"name": "test"}, scope=SCOPE)
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        assert actual.model_dump(mode="json", exclude_unset=True) == expected.model_dump(
            mode="json", exclude_unset=True
        )
        assert actual.model_fields_set == expected.model_fields_set
    same(old, new)
