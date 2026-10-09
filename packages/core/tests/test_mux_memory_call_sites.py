"""Host memory reads and MCP attachment keep their original SDK requests."""

import uuid
from collections import deque
from collections.abc import Mapping
from typing import Any, Literal, cast

import httpx
import pytest
from anthropic import AsyncAnthropic, ConflictError
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsMemoryStore
from daimon.core import mcp_attach, memory_view
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_memory_store, create_memory_store
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope

TENANT = uuid.UUID(int=1)
SCOPE = resource_scope(tenant_id=str(TENANT))
STAMP = "2026-09-15T12:00:00Z"


def script(replies: list[tuple[str, str, int, dict[str, Any]]]) -> ScriptedTransport:
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(status, json=body))
            for method, path, status, body in replies
        )
    )


def equal_requests(old: ScriptedTransport, new: ScriptedTransport) -> None:
    old.assert_consumed()
    new.assert_consumed()
    assert [r.to_dict() for r in old.requests] == [r.to_dict() for r in new.requests]


def entry(native_id: str, path: str) -> dict[str, Any]:
    return {
        "type": "memory",
        "id": native_id,
        "path": path,
        "memory_store_id": "store",
        "created_at": STAMP,
        "updated_at": STAMP,
        "memory_version_id": "v1",
        "content": None,
    }


@pytest.mark.parametrize("operation", ["paths", "content", "missing"])
async def test_memory_view_keeps_prefixes_order_and_selected_read(
    operation: Literal["paths", "content", "missing"],
) -> None:
    rows = [entry("z", "/z.md"), {"type": "memory_prefix", "path": "/folder/"}]
    replies = [
        ("GET", "/v1/memory_stores/store/memories", 200, {"data": rows, "next_page": "next"}),
        (
            "GET",
            "/v1/memory_stores/store/memories",
            200,
            {"data": [entry("a", "/a.md")], "next_page": None},
        ),
    ]
    selected = "/missing.md" if operation == "missing" else "/a.md"
    if operation == "content":
        replies.append(
            (
                "GET",
                "/v1/memory_stores/store/memories/a",
                200,
                {**entry("a", selected), "content": "chosen content"},
            )
        )
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        page = await before.beta.memory_stores.memories.list("store", path_prefix="/")
        if operation == "paths":
            expected = sorted([item.path async for item in page if item.type == "memory"])
            actual = await memory_view.list_memory_paths(after, "store", scope=SCOPE)
        else:
            expected = None
            async for item in page:
                if item.type == "memory" and item.path == selected:
                    value = await before.beta.memory_stores.memories.retrieve(
                        item.id, memory_store_id="store", view="full"
                    )
                    expected = value.content or ""
                    break
            actual = await memory_view.get_memory_content(after, "store", selected, scope=SCOPE)
        assert actual == expected
    equal_requests(old, new)


@pytest.mark.parametrize("operation", ["agent", "environment", "live_agent", "live_environment"])
async def test_resolver_reads_preserve_sdk_requests_and_cache_hits(operation: str) -> None:
    from daimon.core import ma_resolver

    environment = operation.endswith("environment")
    resource = (
        ma_environment(id="resource", tenant_id=TENANT)
        if environment
        else ma_agent(id="resource", tenant_id=TENANT)
    )
    path = "/v1/environments/resource" if environment else "/v1/agents/resource"
    replies = [("GET", path, 200, resource.model_dump(mode="json"))]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await (
            before.beta.environments.retrieve("resource")
            if environment
            else before.beta.agents.retrieve("resource")
        )
        if operation.startswith("live_"):
            # Exercise the two existing private liveness paths directly.
            load = ma_resolver._is_live_environment if environment else ma_resolver._is_live_agent  # pyright: ignore[reportPrivateUsage]
            assert await load(after, "resource", TENANT)
        else:
            cache = ma_resolver.new_resolver_cache()
            load = (
                ma_resolver.retrieve_environment_cached
                if environment
                else ma_resolver.retrieve_agent_cached
            )
            actual = await load(after, cache, TENANT, "resource")
            assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
            assert await load(after, cache, TENANT, "resource") is actual
    equal_requests(old, new)


@pytest.mark.parametrize("explicit_null", [False, True], ids=["omitted-config", "null-config"])
async def test_resolver_environment_keeps_partial_sdk_response_and_cache_hit(
    explicit_null: bool,
) -> None:
    from daimon.core import ma_resolver

    # Existing admission consumers read metadata/archive state from partial
    # SDK responses; the port must not require unused configuration fields.
    body = ma_agent(id="resource", tenant_id=TENANT).model_dump(mode="json")
    if explicit_null:
        body["config"] = None
    replies = [("GET", "/v1/environments/resource", 200, body)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.environments.retrieve("resource")
        cache = ma_resolver.new_resolver_cache()
        actual = await ma_resolver.retrieve_environment_cached(after, cache, TENANT, "resource")
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        assert actual.model_dump(mode="json", exclude_unset=True) == expected.model_dump(
            mode="json", exclude_unset=True
        )
        assert actual.model_fields_set == expected.model_fields_set
        assert (
            await ma_resolver.retrieve_environment_cached(after, cache, TENANT, "resource")
            is actual
        )
    equal_requests(old, new)


async def test_setup_exact_identity_retrieve_preserves_sdk_request() -> None:
    from daimon.core.setup_conversations import get_setup_agent

    agent = ma_agent(id="exact", tenant_id=TENANT)
    replies = [("GET", "/v1/agents/exact", 200, agent.model_dump(mode="json"))]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.agents.retrieve("exact")
        actual = await get_setup_agent(after, tenant_id=TENANT, ma_agent_id="exact")
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    equal_requests(old, new)


@pytest.mark.parametrize("missing", [False, True])
async def test_memory_archive_keeps_request_and_clears_binding_after_404(
    missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anthropic import NotFoundError
    from daimon.core import memory_resource as host

    agent_id = uuid.UUID(int=2)
    body = (
        {"type": "error", "error": {"type": "not_found_error", "message": "missing"}}
        if missing
        else {"type": "memory_store", "id": "store"}
    )
    replies = [("POST", "/v1/memory_stores/store/archive", 404 if missing else 200, body)]
    old, new = script(replies), script(replies)
    cleared: list[dict[str, Any]] = []

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def begin(self) -> "Session":
            return self

    async def binding(*args: object, **kwargs: object) -> str | None:
        return "store"

    async def clear(*args: object, **kwargs: Any) -> None:
        cleared.append(kwargs)

    monkeypatch.setattr(host, "get_memory_store_id", binding)
    monkeypatch.setattr(host, "clear_memory_store", clear)
    original_archive = archive_memory_store

    async def scoped_archive(client: AsyncAnthropic, store_id: str, *, scope: Scope) -> None:
        assert scope.tenant_id == str(TENANT)
        assert not scope.is_platform
        assert not scope.is_legacy_host_authorized
        await original_archive(client, store_id, scope=scope)

    monkeypatch.setattr(host, "archive_memory_store", scoped_archive)
    async with old.client() as before, new.client() as after:
        try:
            await before.beta.memory_stores.archive("store")
        except NotFoundError:
            assert missing
        await host.archive_memory_store_for_agent(
            after, cast(Any, Session), tenant_id=TENANT, agent_id=agent_id
        )
    assert cleared == [{"tenant_id": TENANT, "agent_id": agent_id}]
    equal_requests(old, new)


@pytest.mark.parametrize("operation", ["create", "update", "warm"])
async def test_reader_variant_keeps_create_update_payloads_and_warm_no_io(
    operation: Literal["create", "update", "warm"], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core import reader_agent as host
    from daimon.core.defaults.metadata import build_metadata, compute_spec_fingerprint
    from daimon.core.specs import AgentSpec, dump_agent_spec

    account = uuid.UUID(int=3)
    source = ma_agent(
        id="source",
        name="alpha",
        tenant_id=TENANT,
        system="source instructions",
        metadata={"daimon_spec_hash": "source-hash"},
    )
    reader_spec = host.derive_reader_spec(
        AgentSpec(name="alpha", model=source.model.id, system="source instructions")
    )
    skills = [{"type": "custom", "skill_id": "sk_reader"}]
    metadata = build_metadata(
        tenant_id=TENANT,
        name="alpha-reader",
        account_id=account,
        managed=False,
        isolated=True,
        spec_hash=compute_spec_fingerprint(
            {"spec": dump_agent_spec(reader_spec, mode="json"), "skills": skills}
        ),
    )
    metadata.update(daimon_reader_of="source-hash", daimon_reader_source="alpha")
    payload: dict[str, Any] = {
        **dump_agent_spec(reader_spec),
        "skills": skills,
        "metadata": metadata,
    }
    reader = ma_agent(
        id="reader", name="alpha-reader", tenant_id=TENANT, metadata=metadata, version=4
    )
    existing = (
        reader
        if operation == "warm"
        else reader.model_copy(update={"metadata": {**metadata, "daimon_reader_of": "old"}})
    )

    async def find_source(*args: object, **kwargs: object) -> BetaManagedAgentsAgent:
        return source

    async def find_readers(*args: object, **kwargs: object) -> list[BetaManagedAgentsAgent]:
        return [] if operation == "create" else [existing]

    async def resolve_skills(*args: object, **kwargs: object) -> list[dict[str, str]]:
        return skills

    monkeypatch.setattr(host, "find_agent_by_daimon_tag", find_source)
    monkeypatch.setattr(host, "find_agents_by_daimon_tag", find_readers)
    monkeypatch.setattr(host, "resolve_refs", resolve_skills)
    replies: list[tuple[str, str, int, dict[str, Any]]] = []
    if operation == "update":
        replies.append(("GET", "/v1/agents/reader", 200, existing.model_dump(mode="json")))
    if operation != "warm":
        path = "/v1/agents" if operation == "create" else "/v1/agents/reader"
        replies.append(("POST", path, 200, reader.model_dump(mode="json")))
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        if operation == "create":
            await before.beta.agents.create(**payload)
        elif operation == "update":
            fresh = await before.beta.agents.retrieve("reader")
            await before.beta.agents.update("reader", version=fresh.version, **payload)
        actual = await host.ensure_reader_variant(
            after, tenant_id=TENANT, account_id=account, source_name="alpha"
        )
        assert actual.id == "reader"
        if operation == "warm":
            assert actual is existing
    equal_requests(old, new)


@pytest.mark.parametrize("conflict", [False, True])
async def test_mcp_attachment_recomputes_version_and_access_check_after_conflict(
    conflict: bool,
) -> None:
    first = ma_agent(id="agent", name="test", tenant_id=TENANT, version=1)
    second = first.model_copy(update={"version": 2})
    replies = [("GET", "/v1/agents/agent", 200, first.model_dump(mode="json"))]
    if conflict:
        replies.extend(
            [
                (
                    "POST",
                    "/v1/agents/agent",
                    409,
                    {
                        "type": "error",
                        "error": {"type": "conflict_error", "message": "version changed"},
                    },
                ),
                ("GET", "/v1/agents/agent", 200, second.model_dump(mode="json")),
            ]
        )
    replies.append(("POST", "/v1/agents/agent", 200, second.model_dump(mode="json")))
    old, new = script(replies), script(replies)
    checks: list[str] = []

    async def authorize() -> None:
        checks.append("allowed")

    async with old.client() as before, new.client() as after:
        current = await before.beta.agents.retrieve("agent")
        for attempt in range(2):
            servers, tools = mcp_attach.build_attached_spec(
                current, server_name="external", url="https://example.test/mcp"
            )
            try:
                await before.beta.agents.update(
                    "agent", version=current.version, mcp_servers=servers, tools=tools
                )
                break
            except ConflictError:
                assert attempt == 0
                current = await before.beta.agents.retrieve("agent")
        await mcp_attach.attach_mcp_server_to_agent(
            after,
            "agent",
            server_name="external",
            url="https://example.test/mcp",
            replace_allowed=True,
            before_update=authorize,
            scope=SCOPE,
        )
    assert checks == ["allowed"] * (2 if conflict else 1)
    equal_requests(old, new)


@pytest.mark.parametrize("case", ["cold", "read_only", "lost_race", "insert_error", "warm"])
async def test_memory_provisioning_keeps_cold_payload_and_orphan_cleanup(
    case: Literal["cold", "read_only", "lost_race", "insert_error", "warm"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import memory_resource as host

    agent_id = uuid.UUID(int=2)
    payload: dict[str, Any] = {
        "name": f"daimon alpha {TENANT}",
        "description": (
            "Persistent memory for the daimon agent 'alpha'. Written by the "
            "agent itself across sessions; managed by daimon."
        ),
        "metadata": {"daimon_tenant": str(TENANT), "daimon_agent": str(agent_id)},
    }
    response = {
        "type": "memory_store",
        "id": "created",
        **payload,
        "created_at": STAMP,
        "updated_at": STAMP,
        "archived_at": None,
    }
    replies: list[tuple[str, str, int, dict[str, Any]]] = (
        [] if case == "warm" else [("POST", "/v1/memory_stores", 200, response)]
    )
    if case in {"lost_race", "insert_error"}:
        replies.append(
            (
                "DELETE",
                "/v1/memory_stores/created",
                200,
                {"type": "memory_store_deleted", "id": "created", "deleted": True},
            )
        )
    old, new = script(replies), script(replies)

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def begin(self) -> "Session":
            return self

    async def binding(*args: object, **kwargs: object) -> str | None:
        return "winner" if case == "warm" else None

    async def insert(*args: object, **kwargs: Any) -> str:
        if case == "insert_error":
            raise RuntimeError("binding unavailable")
        return "winner" if case == "lost_race" else kwargs["memory_store_id"]

    monkeypatch.setattr(host, "get_memory_store_id", binding)
    monkeypatch.setattr(host, "insert_memory_store", insert)
    original_create = create_memory_store

    async def scoped_create(
        client: AsyncAnthropic, payload: Mapping[str, object], *, scope: Scope
    ) -> BetaManagedAgentsMemoryStore:
        assert scope.tenant_id == str(TENANT)
        assert not scope.is_platform
        assert not scope.is_legacy_host_authorized
        return await original_create(client, payload, scope=scope)

    monkeypatch.setattr(host, "create_memory_store", scoped_create)
    async with old.client() as before, new.client() as after:
        if case != "warm":
            created = await before.beta.memory_stores.create(**payload)
            if case in {"lost_race", "insert_error"}:
                await before.beta.memory_stores.delete(created.id)
        if case == "insert_error":
            with pytest.raises(RuntimeError, match="binding unavailable"):
                await host.ensure_memory_store_and_mount(
                    after,
                    cast(Any, Session),
                    tenant_id=TENANT,
                    agent_id=agent_id,
                    agent_name="alpha",
                )
        else:
            mount = await host.ensure_memory_store_and_mount(
                after,
                cast(Any, Session),
                tenant_id=TENANT,
                agent_id=agent_id,
                agent_name="alpha",
                read_only=case == "read_only",
            )
            assert mount["memory_store_id"] == (
                "winner" if case in {"lost_race", "warm"} else "created"
            )
            assert mount.get("access") == ("read_only" if case == "read_only" else "read_write")
    equal_requests(old, new)


async def test_setup_foreign_tag_keeps_exact_unavailable_copy() -> None:
    from daimon.core.errors import DaimonError
    from daimon.core.setup_conversations import get_setup_agent

    foreign = ma_agent(id="exact", tenant_id=uuid.UUID(int=2))
    replies = [("GET", "/v1/agents/exact", 200, foreign.model_dump(mode="json"))]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.agents.retrieve("exact")
        assert expected.metadata["daimon_tenant"] != str(TENANT)
        with pytest.raises(DaimonError) as error:
            await get_setup_agent(after, tenant_id=TENANT, ma_agent_id="exact")
        assert str(error.value) == (
            "That agent is no longer available in this workspace. Choose another agent."
        )
    equal_requests(old, new)
