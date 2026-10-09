"""Remaining CLI calls preserve raw SDK bytes, errors and tenant scopes."""

# pyright: reportPrivateUsage=false
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from io import StringIO
from typing import cast
from uuid import UUID

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.memory_stores.beta_managed_agents_memory_list_item import (
    BetaManagedAgentsMemoryListItem,
)
from daimon.adapters.cli.commands import environments, memory, sessions
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_compat import list_memory_entries, read_memory
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import CliPrincipalRow
from daimon.testing.ma_models import ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = derive_tenant_uuid(platform="discord", workspace_id="999")


class Transaction:
    async def __aenter__(self) -> "Transaction":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def begin(self) -> "Transaction":
        return self


def runtime(client: AsyncAnthropic) -> CliRuntime:
    return CliRuntime(
        settings=Settings.model_validate(
            {
                "database": {"url": "postgresql+asyncpg://localhost/offline"},
                "anthropic": {"api_key": "offline"},
            }
        ),
        anthropic=client,
        sessionmaker=cast(async_sessionmaker[AsyncSession], lambda: Transaction()),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def equal(before: ScriptedTransport, after: ScriptedTransport) -> None:
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests  # includes raw body bytes and protocol headers


def queue_memory(t: ScriptedTransport, mode: str, status: int) -> None:
    t.queue(
        ScriptedReply(
            "GET",
            "/v1/memory_stores/store1/memories",
            httpx.Response(
                200,
                json={
                    "data": [
                        {"type": "memory_prefix", "path": "/notes/"},
                        {"type": "memory", "id": "m1", "path": "/a.md"},
                    ],
                    "next_page": "page2",
                    "has_more": True,
                },
            ),
        )
    )
    if mode != "early":
        t.queue(
            ScriptedReply(
                "GET",
                "/v1/memory_stores/store1/memories",
                httpx.Response(
                    200,
                    json={
                        "data": [{"type": "memory", "id": "m2", "path": "/z.md"}],
                        "next_page": None,
                        "has_more": False,
                    },
                ),
            )
        )
    if mode in ("early", "late"):
        t.queue(
            ScriptedReply(
                "GET",
                f"/v1/memory_stores/store1/memories/{'m1' if mode == 'early' else 'm2'}",
                httpx.Response(
                    status,
                    json={
                        "content": "[red]agent text[/red]",
                        "id": "m1",
                        "path": "/a.md",
                        "type": "memory",
                        "memory_version_id": "v1",
                    }
                    if status == 200
                    else {"error": {"type": "permission_error", "message": "refused"}},
                ),
            )
        )


async def sdk_memory(client: AsyncAnthropic, mode: str) -> list[str] | str:
    paths: list[str] = []
    page = await client.beta.memory_stores.memories.list("store1", path_prefix="/")
    async for item in page:
        if item.type == "memory":
            if mode == "list":
                paths.append(item.path)
            elif item.path == ("/a.md" if mode == "early" else "/z.md"):
                return (
                    await client.beta.memory_stores.memories.retrieve(
                        item.id, memory_store_id="store1", view="full"
                    )
                ).content or ""
    return sorted(paths)


@pytest.mark.parametrize(
    "mode,status",
    [("list", 200), ("early", 200), ("late", 200), ("early", 403), ("late", 404), ("late", 409)],
)
async def test_cli_memory_actual_callsite_bytes_pagination_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: int,
) -> None:
    before, after = ScriptedTransport(), ScriptedTransport()
    for t in (before, after):
        queue_memory(t, mode, status)
    scopes: list[Scope] = []

    async def resolve(*args: object, **kwargs: object) -> str:
        assert kwargs == {"platform": "discord", "workspace": "999", "agent": "agent"}
        return "store1"

    async def walk(
        client: AsyncAnthropic, store_id: str, *, path_prefix: str, scope: Scope
    ) -> AsyncIterator[BetaManagedAgentsMemoryListItem]:
        scopes.append(scope)
        async for item in list_memory_entries(
            client, store_id, path_prefix=path_prefix, scope=scope
        ):
            yield item

    async def read(client: AsyncAnthropic, store_id: str, memory_id: str, *, scope: Scope):
        scopes.append(scope)
        return await read_memory(client, store_id, memory_id, scope=scope)

    monkeypatch.setattr(memory, "_resolve_store_id", resolve)
    monkeypatch.setattr(memory, "list_memory_entries", walk)
    monkeypatch.setattr(memory, "read_memory", read)
    output = StringIO()
    async with before.client() as old, after.client() as new:
        expected_error: Exception | None = None
        expected: list[str] | str = []
        try:
            expected = await sdk_memory(old, mode)
        except Exception as exc:
            expected_error = exc
        actual_error: Exception | None = None
        try:
            if mode == "list":
                await memory.memory_list_impl(
                    rt=runtime(new),
                    console=Console(file=output),
                    platform="discord",
                    workspace="999",
                    agent="agent",
                    as_json=True,
                )
            else:
                await memory.memory_show_impl(
                    rt=runtime(new),
                    console=Console(file=output),
                    path="/a.md" if mode == "early" else "/z.md",
                    platform="discord",
                    workspace="999",
                    agent="agent",
                )
        except Exception as exc:
            actual_error = exc
    equal(before, after)
    assert type(actual_error) is type(expected_error)
    assert str(actual_error) == str(expected_error)
    assert scopes and all(
        s.tenant_id == str(TENANT) and s.platform_reason is None and s.legacy_call_site is None
        for s in scopes
    )
    assert all(s is scopes[0] for s in scopes)
    if status == 200:
        if mode == "list":
            import json

            assert [row["path"] for row in json.loads(output.getvalue())] == expected
        else:
            assert output.getvalue().strip() == expected
    else:
        assert output.getvalue() == ""


@pytest.mark.parametrize("description", [None, "", "description"])
@pytest.mark.parametrize("status", [200, 403, 409])
async def test_environment_fork_callsite_null_description_and_error_bytes(
    monkeypatch: pytest.MonkeyPatch,
    description: str | None,
    status: int,
) -> None:
    record = ma_environment(id="env1", name="source", tenant_id=TENANT).model_dump(mode="json")
    record["description"] = description
    before, after = ScriptedTransport(), ScriptedTransport()
    for t in (before, after):
        t.queue(
            ScriptedReply("GET", "/v1/environments/env1", httpx.Response(200, json=record)),
            ScriptedReply(
                "POST",
                "/v1/environments",
                httpx.Response(
                    status,
                    json=record
                    if status == 200
                    else {"error": {"type": "conflict_error", "message": "refused"}},
                ),
            ),
        )

    async def discover(*args: object, **kwargs: object) -> UUID:
        return TENANT

    async def principal(*args: object, **kwargs: object) -> CliPrincipalRow:
        return CliPrincipalRow(
            id=UUID(int=9),
            tenant_id=TENANT,
            account_id=UUID(int=8),
            os_user="operator",
            created_at=datetime(2026, 10, 9, tzinfo=UTC),
        )

    async def find(*args: object, **kwargs: object):
        return ma_environment(id="env1", name="source", tenant_id=TENANT)

    async def matches(*args: object, **kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(environments, "discover_tenant", discover)
    monkeypatch.setattr(environments, "get_or_create_cli_principal", principal)
    monkeypatch.setattr(environments, "find_environment_by_daimon_tag", find)
    monkeypatch.setattr(environments, "find_environments_by_daimon_tag", matches)
    output = StringIO()
    errors: list[Exception | None] = []
    async with before.client() as old, after.client() as new:
        source = await old.beta.environments.retrieve("env1")
        try:
            from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams

            await old.beta.environments.create(
                name="copy",
                config=cast(
                    BetaCloudConfigParams,
                    {
                        k: source.config.model_dump(mode="json")[k]
                        for k in ("type", "networking", "packages")
                        if k in source.config.model_dump(mode="json")
                    },
                ),
                description=description,
                metadata={"daimon_tenant": str(TENANT), "daimon_name": "copy"},
            )
            errors.append(None)
        except Exception as exc:
            errors.append(exc)
        try:
            await environments.environments_fork(
                rt=runtime(new),
                console=Console(file=output, color_system=None),
                src="source",
                dst="copy",
            )
            errors.append(None)
        except Exception as exc:
            errors.append(exc)
    equal(before, after)
    assert type(errors[0]) is type(errors[1])
    assert str(errors[0]) == str(errors[1])
    assert output.getvalue().strip() == (
        "✓ forked environment 'source' → 'copy'" if status == 200 else ""
    )


@pytest.mark.parametrize("status", [200, 403, 404, 409])
@pytest.mark.parametrize("as_json", [True, False])
async def test_raw_session_inspection_keeps_operator_authority_output_and_no_db(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    as_json: bool,
) -> None:
    from dataclasses import replace

    from anthropic.types.beta import BetaManagedAgentsSession
    from daimon.adapters.cli.output import emit_rows
    from daimon.core.session_ports_compat import retrieve_session_record

    # Inspecting another tenant's raw session is existing workspace operator behavior.
    body = {
        "id": "session1",
        "status": "idle",
        "environment_id": "env1",
        "title": "title",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "metadata": {"daimon_tenant": str(UUID(int=123))},
    }
    before, after = ScriptedTransport(), ScriptedTransport()
    for transport in (before, after):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/session1",
                httpx.Response(
                    status,
                    json=body
                    if status == 200
                    else {"error": {"type": "permission_error", "message": "refused"}},
                ),
            )
        )
    scopes: list[Scope] = []

    async def read(
        client: AsyncAnthropic, session_id: str, *, scope: Scope
    ) -> BetaManagedAgentsSession:
        scopes.append(scope)
        return await retrieve_session_record(client, session_id, scope=scope)

    def no_database() -> AsyncSession:
        raise AssertionError("raw operator inspection must not add database work")

    monkeypatch.setattr(sessions, "retrieve_session_record", read)
    outputs = [StringIO(), StringIO()]
    errors: list[Exception | None] = []
    async with before.client() as old, after.client() as new:
        try:
            record = await old.beta.sessions.retrieve("session1")
            emit_rows(
                Console(file=outputs[0]),
                [record],
                columns=("id", "status", "environment_id", "title", "created_at", "updated_at"),
                as_json=as_json,
            )
            errors.append(None)
        except Exception as exc:
            errors.append(exc)
        try:
            rt = replace(
                runtime(new), sessionmaker=cast(async_sessionmaker[AsyncSession], no_database)
            )
            await sessions.sessions_get(
                rt=rt, console=Console(file=outputs[1]), session_id="session1", as_json=as_json
            )
            errors.append(None)
        except Exception as exc:
            errors.append(exc)
    equal(before, after)
    assert type(errors[0]) is type(errors[1])
    assert str(errors[0]) == str(errors[1])
    assert outputs[0].getvalue() == outputs[1].getvalue()
    assert len(scopes) == 1
    assert scopes[0].is_platform
    assert scopes[0].platform_reason == "CLI operator raw session inspection"
    assert scopes[0].legacy_call_site is None
