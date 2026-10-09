"""Cleanup ports retain real SDK request bytes, paging, failures and response leniency."""

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, Self, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from anthropic import APIError, APIStatusError, AsyncAnthropic
from daimon.core import direct_messages, ma, session_preparation_stages, session_snapshot
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_lenient
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.stores.direct_messages import DirectMessageRow
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn import session_identity
from daimon.core.turn.deps import TurnDeps
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.core_admin import CoreAdmin
from mux.errors import ScopeViolation
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID(int=1)
ACCOUNT = uuid.UUID(int=2)
SCOPE = resource_scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), authorization_id="account-purge"
)
SENTINEL: dict[str, JsonValue] = {
    "id": "agent_sentinel",
    "metadata": {"daimon_workspace": "disposable"},
}


def queue(
    transport: ScriptedTransport,
    method: str,
    path: str,
    payload: dict[str, JsonValue],
    *,
    status: int = 200,
) -> None:
    transport.queue(ScriptedReply(method, path, httpx.Response(status, json=payload)))


def page(data: list[dict[str, JsonValue]], cursor: str | None = None) -> dict[str, JsonValue]:
    return {"data": cast(JsonValue, data), "has_more": cursor is not None, "next_page": cursor}


def error() -> dict[str, JsonValue]:
    return {"error": {"type": "api_error", "message": "existing failure"}}


async def legacy_workspace_cleanup(client: AsyncAnthropic) -> None:
    """The unchanged SDK arguments and fallback order from the integration base."""
    sentinel = None
    async for agent in client.beta.agents.list(limit=100):
        if agent.metadata.get("daimon_workspace") == "disposable":
            sentinel = agent
            break
    assert sentinel is not None
    skills, _ = await list_skills_lenient(client)
    assert not skills
    async for env in client.beta.environments.list(limit=100):
        try:
            await client.beta.environments.delete(env.id)
        except APIStatusError as err:
            if err.status_code == 409:
                await client.beta.environments.archive(env.id)
            else:
                assert err.status_code == 404
    async for agent in client.beta.agents.list(limit=100):
        if agent.id != sentinel.id:
            await client.beta.agents.archive(agent.id)


async def test_disposable_cleanup_preserves_lazy_paging_partial_replies_and_409_fallback() -> None:
    ordinary: dict[str, JsonValue] = {"id": "agent_other", "metadata": {"daimon_tenant": "other"}}
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(transport, "GET", "/v1/agents", page([ordinary], "agents_second"))
        queue(transport, "GET", "/v1/agents", page([SENTINEL]))
        queue(transport, "GET", "/v1/skills", page([]))
        queue(transport, "GET", "/v1/environments", page([{"id": "env_busy"}], "env_second"))
        queue(transport, "DELETE", "/v1/environments/env_busy", error(), status=409)
        queue(transport, "POST", "/v1/environments/env_busy/archive", {})
        queue(transport, "GET", "/v1/environments", page([{"id": "env_gone"}]))
        queue(transport, "DELETE", "/v1/environments/env_gone", error(), status=404)
        queue(transport, "GET", "/v1/agents", page([SENTINEL], "agents_third"))
        queue(transport, "GET", "/v1/agents", page([ordinary]))
        queue(transport, "POST", "/v1/agents/agent_other/archive", {})
    async with old.client() as legacy, new.client() as client:
        await legacy_workspace_cleanup(legacy)
        await ma.delete_entire_workspace_for_testing(
            client, i_understand_this_destroys_all_tenants=True
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    assert len(new.requests) == 11


async def test_sentinel_stops_at_first_match_without_fetching_next_page() -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(transport, "GET", "/v1/agents", page([SENTINEL], "unused"))
    async with old.client() as legacy, new.client() as client:
        original = None
        async for agent in legacy.beta.agents.list(limit=100):
            if agent.metadata.get("daimon_workspace") == "disposable":
                original = agent
                break
        result = await ma.find_workspace_disposable_sentinel(client)
    assert original is not None and result is not None
    assert result.model_dump(mode="json", exclude_unset=True) == original.model_dump(
        mode="json", exclude_unset=True
    )
    assert old.requests == new.requests
    assert len(new.requests) == 1


async def test_cleanup_without_opt_in_or_sentinel_never_mutates() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(RuntimeError, match="ALL tenants"):
            await ma.delete_entire_workspace_for_testing(client)
        assert not transport.requests
        queue(transport, "GET", "/v1/agents", page([]))
        with pytest.raises(DaimonError, match="not marked disposable"):
            await ma.delete_entire_workspace_for_testing(
                client, i_understand_this_destroys_all_tenants=True
            )
    transport.assert_consumed()
    assert len(transport.requests) == 1


async def legacy_account_purge(client: AsyncAnthropic) -> ma.SessionDeletionReport:
    targets: set[str] = set()
    for agent in await list_agents_by_tenant(client, tenant_id=TENANT):
        async for session in client.beta.sessions.list(agent_id=agent.id):
            if session.metadata.get("daimon_account") == str(ACCOUNT):
                targets.add(session.id)
    deleted = failed = 0
    for session_id in targets:
        try:
            await client.beta.sessions.delete(session_id)
            deleted += 1
        except APIStatusError as err:
            if err.status_code == 404:
                deleted += 1
            else:
                failed += 1
    return ma.SessionDeletionReport(deleted=deleted, failed=failed)


@pytest.mark.parametrize("delete_status", [200, 404, 500])
async def test_account_purge_keeps_paging_account_filter_and_status_counts(
    delete_status: int,
) -> None:
    from daimon.testing.ma_models import ma_agent

    agent = ma_agent(id="agent_tenant", metadata={"daimon_tenant": str(TENANT)})
    target: dict[str, JsonValue] = {
        "id": "sess_target",
        "metadata": {"daimon_account": str(ACCOUNT)},
    }
    other: dict[str, JsonValue] = {"id": "sess_other", "metadata": {"daimon_account": "other"}}
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(transport, "GET", "/v1/agents", page([agent.model_dump(mode="json")]))
        queue(transport, "GET", "/v1/sessions", page([other], "sessions_second"))
        queue(transport, "GET", "/v1/sessions", page([target, target]))
        queue(
            transport,
            "DELETE",
            "/v1/sessions/sess_target",
            {} if delete_status == 200 else error(),
            status=delete_status,
        )
    async with old.client() as legacy, new.client() as client:
        original = await legacy_account_purge(legacy)
        result = await ma.delete_sessions_for_account(client, tenant_id=TENANT, account_id=ACCOUNT)
    old.assert_consumed()
    new.assert_consumed()
    assert result == original
    assert result == ma.SessionDeletionReport(
        deleted=0 if delete_status == 500 else 1, failed=1 if delete_status == 500 else 0
    )
    assert old.requests == new.requests


@pytest.mark.parametrize("status", [200, 400, 500])
async def test_orphan_interrupt_keeps_sdk_wire_and_best_effort_errors(status: int) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(
            transport,
            "POST",
            "/v1/sessions/sess_orphan/events",
            {} if status == 200 else error(),
            status=status,
        )
    async with old.client() as legacy, new.client() as client:
        with contextlib.suppress(APIError):
            await legacy.beta.sessions.events.send(
                "sess_orphan", events=[{"type": "user.interrupt"}]
            )
        result = await ma.interrupt_orphaned_session(client, session_id="sess_orphan", scope=SCOPE)
    old.assert_consumed()
    new.assert_consumed()
    assert result is (status == 200)
    assert old.requests == new.requests


@pytest.mark.parametrize("operation", ["delete", "interrupt", "agent", "workspace"])
async def test_missing_grant_and_mismatched_scopes_fail_before_io(operation: str) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        backend = managed_agents(client, scope=SCOPE)
        port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
        with pytest.raises(ScopeViolation):
            if operation == "delete":
                await port.delete_session(
                    SCOPE, resource_ref(backend, "session", "ungranted", scope=SCOPE)
                )
            elif operation == "interrupt":
                different = SCOPE.model_copy(update={"tenant_id": "other"})
                await port.interrupt_orphan(
                    different, resource_ref(backend, "session", "sess", scope=SCOPE)
                )
            elif operation == "agent":
                _ = [
                    item
                    async for item in port.sessions_for_agent(
                        SCOPE, resource_ref(backend, "agent", "ungranted", scope=SCOPE)
                    )
                ]
            else:
                _ = [item async for item in port.workspace_agents(SCOPE)]
    assert not transport.requests


async def test_orphan_without_account_scope_logs_skip_without_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    monkeypatch.setattr(ma, "log", logger)
    transport = ScriptedTransport()
    async with transport.client() as client:
        assert not await ma.interrupt_orphaned_session(client, session_id="sess_old", scope=None)
    assert not transport.requests
    logger.info.assert_called_once_with(
        "turn.orphan_interrupt_skipped", session_id="sess_old", reason="missing_account_id"
    )


@pytest.mark.parametrize(
    "scope", [Scope.platform(reason="test"), Scope.legacy_host_authorized(call_site="test")]
)
async def test_account_purge_refuses_platform_and_legacy_scope_before_io(scope: Scope) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        backend = managed_agents(client, scope=scope)
        port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
        with pytest.raises(ScopeViolation, match="tenant scope"):
            await port.delete_session(scope, resource_ref(backend, "session", "sess", scope=scope))
    assert not transport.requests


class SessionFactory:
    def __call__(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)

    def begin(self) -> contextlib.nullcontext[Self]:
        return contextlib.nullcontext(self)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"daimon_channel": None},
        {"daimon_thread": None},
        {"daimon_channel": None, "daimon_thread": None},
    ],
    ids=["absent", "null-channel", "null-thread", "null-channel-and-thread"],
)
async def test_legacy_identity_lookup_keeps_partial_sdk_reply_and_one_request(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, JsonValue]
) -> None:
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    mapping = ThreadSessionRow(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        account_id=ACCOUNT,
        ma_agent_id=None,
        ma_session_id="sess_identity",
        platform="discord",
        thread_id="thread",
        watermark_message_id=None,
        status="live",
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
        updated_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    backfill = AsyncMock()
    monkeypatch.setattr(session_identity, "update_agent_identity", backfill)
    response: dict[str, JsonValue] = {
        "id": "sess_identity",
        "status": "future_status",
        "agent": {"id": "agent_original"},
    }
    if metadata:
        response["metadata"] = metadata
        response["resources"] = []
        response["vault_ids"] = []
        response["environment_id"] = "env_identity"
        response["agent"] = {
            "id": "agent_original",
            "name": "identity",
            "version": 1,
            "model": {"id": "model_identity"},
            "system": "system",
            "skills": [],
            "tools": [],
            "mcp_servers": [],
        }
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(transport, "GET", "/v1/sessions/sess_identity", response)
    async with old.client() as legacy, new.client() as client:
        original = await legacy.beta.sessions.retrieve("sess_identity")
        result = await session_identity.check_session_agent(
            client, factory, mapping=mapping, responder_ma_agent_id="agent_original"
        )
    assert result.session_exists
    backfill.assert_awaited_once_with(factory, id=mapping.id, ma_agent_id="agent_original")
    assert result.observed is not None
    assert result.observed.model_dump(mode="json", exclude_unset=True) == original.model_dump(
        mode="json", exclude_unset=True
    )
    if metadata:
        snapshot_backfill = AsyncMock()
        monkeypatch.setattr(session_preparation_stages, "record_snapshot", snapshot_backfill)
        recorded = await session_preparation_stages.recorded_snapshot(
            client, factory, existing=mapping, observed=result.observed
        )
        assert recorded is not None
        expected = session_snapshot.snapshot_from_retrieved_session(original)
        assert recorded == expected
        snapshot_backfill.assert_awaited_once_with(
            factory,
            id=mapping.id,
            snapshot=expected,
            identity_fingerprint=session_snapshot.fingerprint_identity(expected),
            mutable_fingerprint=session_snapshot.fingerprint_mutable(expected),
        )
    assert old.requests == new.requests
    assert len(new.requests) == 1


@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_dm_quarantine_keeps_archive_wire_and_sealed_error(
    status: int, path: Literal["legacy", "mux"], monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = cast(async_sessionmaker[AsyncSession], SessionFactory())
    conversation = DirectMessageRow(
        platform="slack",
        route_key="route",
        external_user_id="user",
        tenant_id=TENANT,
        account_id=ACCOUNT,
        workspace_id="workspace",
        channel_id="channel",
        scope_id="dm-scope",
        source_url="https://example.com/source",
        context="",
        memory_read_only=False,
        history=[],
        recent_message_ids=[],
        active_until=None,
    )
    monkeypatch.setattr(
        direct_messages, "quarantine_conversation", AsyncMock(return_value=["sess_dm"])
    )
    old, new, unused = ScriptedTransport(), ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        queue(
            transport,
            "POST",
            "/v1/sessions/sess_dm/archive",
            {} if status == 200 else error(),
            status=status,
        )
    async with old.client() as legacy, new.client() as client, unused.client() as unused_client:
        with contextlib.suppress(APIError):
            await legacy.beta.sessions.archive("sess_dm")
        scope = resource_scope(
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            authorization_id="sealed-dm-retirement",
        )
        backend = managed_agents(client, scope=scope, resources=frozenset({("session", "sess_dm")}))

        def session_ref(session_id: str, authorized: Scope) -> ResourceRef:
            return resource_ref(backend, "session", session_id, scope=authorized)

        deps = cast(
            TurnDeps,
            SimpleNamespace(
                anthropic=client if path == "legacy" else unused_client,
                sessionmaker=factory,
                turn_path=path,
                backend=backend if path == "mux" else None,
                backend_session_ref=session_ref,
            ),
        )
        with pytest.raises(DaimonError) as caught:
            await direct_messages._quarantine(deps, conversation)  # pyright: ignore[reportPrivateUsage]
        assert str(caught.value) == direct_messages.SEALED_SINCE_MESSAGE
    assert old.requests == new.requests
    assert not unused.requests
