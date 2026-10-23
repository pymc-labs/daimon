"""Real gateway -> admission/queue/title proofs, with native completion unbound."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Never
from unittest.mock import AsyncMock

import httpx
import pytest
import sentry_sdk
from daimon.adapters.discord import bot as bot_module
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.thread_naming import generate_thread_name
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    ThreadNamingSettings,
    TurnSettings,
)
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import scoped_config_write, tenant_ledger
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.accounts import get_account
from daimon.core.stores.tenants import set_provision_status
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_ingress import ConcurrentCheckpoint, DiscordIngress, IngressHTTP
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import BackendConfig
from mux.contracts.ids import Provider, Scope
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


class InitOnlySettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


@dataclasses.dataclass
class OwnedIngress:
    runtime: DiscordRuntime
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    native: ScriptedTransport


@pytest.fixture
async def owned(
    db_engine: AsyncEngine,
    db_clean: None,
) -> AsyncIterator[OwnedIngress]:
    # Real concurrent gateway callbacks need independent pooled connections,
    # unlike the shared fixture factory's single checked-out connection.
    db_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        if (
            db_engine.url.get_backend_name() == "postgresql"
            and db_engine.url.host in ("localhost", "127.0.0.1")
            and db_engine.url.database == "daimon_test"
            and not db_engine.url.query
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("requires an isolated test database; plain local contributor database")
        raise
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="100")
        account = await make_account(session, tenant=tenant)
        await scoped_config_write.set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="101"),
            tenant_id=tenant.id,
            agent_name="qa-agent",
            environment_name="qa-env",
        )
        await tenant_ledger.insert_entry(
            session,
            tenant_id=tenant.id,
            delta_usd=Decimal("10"),
            reason="trial",
            idempotency_key="c7-owned-credit",
        )
    await set_provision_status(db_session_factory, tenant_id=tenant.id, status="ready")
    agent = ma_agent(tenant_id=tenant.id, name="qa-agent")
    environment = ma_environment(tenant_id=tenant.id, name="qa-env")
    router = MARouter()
    router.add_agent(agent)
    router.add_environment(environment)
    router.add("GET", "/v1/agents", lambda r, m: list_response([agent.model_dump(mode="json")]))
    router.add(
        "GET", "/v1/environments", lambda r, m: list_response([environment.model_dump(mode="json")])
    )
    router.add(
        "POST",
        "/v1/messages",
        lambda r, m: httpx.Response(
            200,
            json={
                "id": "msg_owned_title",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": "Getting started with Daimon"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 9},
            },
        ),
    )
    native = ScriptedTransport(router=router)
    client = native.client()
    settings = InitOnlySettings(
        database=DatabaseSettings(
            url=PostgresDsn(
                "postgresql+asyncpg://fixture:fixture@localhost:5432/daimon_test_nc_fixture"
            )
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        discord=DiscordSettings(bot_token=SecretStr("offline"), thread_open_notice_after_s=0),
        thread_naming=ThreadNamingSettings(),
        turn=TurnSettings(path="mux", channel_backends=True),
    )
    cache = new_resolver_cache()
    default = DeploymentDefault()
    deps = build_turn_deps(
        settings,
        client,
        db_session_factory,
        deployment_default=default,
        resolver_cache=cache,
        billing_config=None,
    )

    deps = dataclasses.replace(deps, turn_runtime_factory=None, turn_runtimes={})
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=client,
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=default,
        resolver_cache=cache,
        turn_deps=deps,
    )

    try:
        yield OwnedIngress(runtime, tenant.id, account.id, native)
    finally:
        await drain_outcomes()
        await client.close()


def payload(
    message_id: int, text: str, *, mentioned: bool = True, channel_id: int = 101
) -> dict[str, Any]:
    return {
        "id": str(message_id),
        "guild_id": "100",
        "channel_id": str(channel_id),
        "type": 0,
        "content": ("<@104> " if mentioned else "") + text,
        "attachments": [],
        "embeds": [],
        "components": [],
        "author": {
            "id": "103",
            "username": "Human",
            "discriminator": "0000",
            "avatar": None,
            "bot": False,
        },
        "mentions": [
            {
                "id": "104",
                "username": "Daimon",
                "discriminator": "0000",
                "avatar": None,
                "bot": True,
            }
        ]
        if mentioned
        else [],
        "mention_roles": [],
        "mention_everyone": False,
        "pinned": False,
        "timestamp": "2026-10-11T02:00:00+00:00",
        "edited_timestamp": None,
    }


async def start(
    owned: OwnedIngress,
    backend: Provider,
    *,
    utc_clock: Callable[[], datetime] | None = None,
) -> DiscordIngress:
    profiles = {
        "anthropic": "anthropic.managed_agents",
        "openai": "openai.persistent_workspace",
        "gemini": "gemini.inline_reuse",
    }
    models = {"anthropic": None, "openai": "gpt-6-luna", "gemini": "gemini-3.8-flash"}
    async with policy_write_transaction(
        owned.runtime.sessionmaker, tenant_id=owned.tenant_id
    ) as session:
        policy = await load_access_policy(session, tenant_id=owned.tenant_id)
        if backend not in policy.enabled_backends:
            await set_access_policy(
                session,
                tenant_id=owned.tenant_id,
                policy=type(policy).model_validate(
                    {**policy.model_dump(), "enabled_backends": (*policy.enabled_backends, backend)}
                ),
            )
        await set_channel_backend(
            session,
            channel_ref(owned.tenant_id, "discord", "101"),
            BackendConfig(backend=backend, profile=profiles[backend], model=models[backend]),
        )
    harness = DiscordIngress(
        runtime=owned.runtime,
        scope=Scope(
            tenant_id=str(owned.tenant_id),
            account_id=str(owned.account_id),
            principal_id="qa-operator:100",
            authorization_id="qa-offline:100",
        ),
        http=IngressHTTP(evidence_id="c7-owned", parent_channel_id=101, hold_first_post=True),
        native=owned.native,
        utc_clock=utc_clock,
    )
    await harness.open(
        guild={"id": "100", "name": "owned QA", "roles": [], "emojis": [], "stickers": []},
        channel={
            "id": "101",
            "name": "owned-parent",
            "type": 0,
            "position": 0,
            "permission_overwrites": [],
        },
        bot_user={
            "id": "104",
            "username": "Daimon",
            "discriminator": "0000",
            "avatar": None,
            "bot": True,
        },
    )
    return harness


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize(
    "scenario", ["QA-I6-BARE-MENTION-THREAD-TITLE", "QA-NEW9-NO-INTERNAL-PLUMBING"]
)
async def test_source_ingress_and_title(
    owned: OwnedIngress, backend: Provider, scenario: str
) -> None:
    harness = await start(owned, backend)
    context: dict[str, Any] | None = None
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            if scenario.startswith("QA-I6"):
                context = payload(
                    1525400123000000000,
                    "Client email: can we move the Q4 review to Thursday at 3pm?",
                    mentioned=False,
                )
                trigger = payload(1525400123000000001, "")
                harness.deliver(context)
                harness.deliver(trigger)
            else:
                trigger = payload(1525400123000000000, "hi! what can you do? I'm new here.")
                harness.deliver(trigger)
            await asyncio.wait_for(harness.http.first_post.wait(), timeout=15)
            receipt = await harness.receipt()
        assert not receipt.errors and receipt.backend_revision.backend == backend
        assert receipt.dispatched_message_ids == tuple(
            row.message_id for row in receipt.gateway_messages
        )
        assert len(receipt.thread_creations) == 1
        created = receipt.thread_creations[0]
        assert created.message_id == created.thread_id == trigger["id"]
        assert receipt.queue_in_flight == receipt.callbacks_pending == 1
        assert created.thread_id in receipt.processing_thread_ids
        assert len(receipt.posts) == 1 and receipt.posts[0].message.channel_id == created.thread_id
        assert not receipt.completion_certificate
        if scenario.startswith("QA-I6"):
            assert context is not None
            assert receipt.gateway_messages[0].payload_json == json.dumps(context)
            assert not receipt.naming_calls and not receipt.naming_meters
            assert created.name == "Chat with daimon"  # the bot, never the agent
            assert (
                len(created.name) <= 60
                and re.search(
                    r"(?i)I don'?t see|no message|please share|summari[sz]e|thread title|\bempty\b",
                    created.name,
                )
                is None
            )
        else:
            assert len(receipt.naming_calls) == len(receipt.naming_meters) == 1
            request = json.loads(receipt.naming_calls[0].request_json)
            assert (
                request["messages"][0]["content"]
                == "<message>\nhi! what can you do? I'm new here.\n</message>"
            )
            assert created.name == "Getting started with Daimon"
            assert receipt.naming_meters[0].managed_session_id == f"thread-naming:{trigger['id']}"
            assert receipt.naming_meters[0].platform_user_id == "103"
            assert (
                len(created.name) <= 50
                and re.search(r"(?i)^(greeting|hello|hi|help request|chat with .*)$", created.name)
                is None
            )
        assert not any(r.path.startswith("/v1/sessions") for r in owned.native.requests)
        print(
            "C7_INGRESS_RECEIPT="
            + json.dumps(
                {
                    "scenario_id": scenario,
                    "backend": backend,
                    "source_title_predicate": "PASS",
                    "whole_cell": "PENDING",
                    "reason": "NATIVE_ROOT_COMPLETION_AND_ORACLE_UNBOUND",
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
        )
    finally:
        await harness.close()
    assert harness.bot.turn_queue.in_flight() == 0


async def test_real_anthropic_path_reads_original_channel_context(owned: OwnedIngress) -> None:
    """Controlled native fixture proves host wiring, not a catalog completion pin."""
    from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
    from mux.testing.drivers.anthropic.ma import FakeMAState, make_fake_memory_store_handler

    agent = ma_agent(tenant_id=owned.tenant_id, name="qa-agent")
    state = FakeSessionsState(
        ma=FakeMAState(
            agents={agent.id: agent.model_dump(mode="json")},
            agent_versions={agent.id: {agent.version: agent.model_dump(mode="json")}},
        )
    )
    session_handler = make_fake_sessions_handler(state)
    memory_handler = make_fake_memory_store_handler()
    router = owned.native.router
    assert router is not None
    for method in ("GET", "POST", "PATCH", "DELETE"):
        router.add(
            method, r"/v1/(sessions|files).*", lambda request, match: session_handler(request)
        )
        router.add(method, r"/v1/memory_stores.*", lambda request, match: memory_handler(request))
    calls: list[datetime] = []

    def clock() -> datetime:
        value = datetime.now(UTC)
        calls.append(value)
        return value

    harness = await start(owned, "anthropic", utc_clock=clock)
    context = payload(
        1525400123000000000,
        "Client email: can we move the Q4 review to Thursday at 3pm?",
        mentioned=False,
    )
    trigger = payload(1525400123000000001, "")
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(context)
            harness.deliver(trigger)
            await asyncio.wait_for(harness.http.first_post.wait(), 15)
            harness.http.release_first_post.set()
            await asyncio.wait_for(harness.wait(), 30)
        assert not owned.native.violations
        assert len(state.sessions) == len(state.sent_batches) == 1
        sent = json.dumps(state.sent_batches)
        assert context["content"] in sent
        assert trigger["id"] in sent
        assert "channel_context" in sent
        receipt = await harness.receipt()
        assert len(receipt.history_reads) == 1
        assert json.loads(receipt.history_reads[0].response_json) == [trigger, context]
        assert not receipt.errors and receipt.queue_in_flight == receipt.callbacks_pending == 0
        assert len(calls) == 2 and calls[0] <= calls[1]
        assert not receipt.completion_certificate
    finally:
        await harness.close()


@pytest.mark.parametrize("mutation", ["ingress_removed", "title_stubbed", "wrong_create_title"])
async def test_production_path_mutations_break_observations(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    harness = await start(owned, "anthropic")
    if mutation == "ingress_removed":
        monkeypatch.setattr(harness.bot, "on_message", AsyncMock())
    elif mutation == "title_stubbed":
        monkeypatch.setattr(
            bot_module, "generate_thread_name", AsyncMock(return_value="Invented title")
        )
    else:
        original = generate_thread_name

        async def wrong_title(**kwargs: Any) -> str:
            await original(**kwargs)
            return "Hello"

        monkeypatch.setattr(bot_module, "generate_thread_name", wrong_title)
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(payload(1525400123000000000, "hi! what can you do? I'm new here."))
            if mutation == "ingress_removed":
                await harness.wait()
            else:
                await asyncio.wait_for(harness.http.first_post.wait(), 15)
        receipt = await harness.receipt()
        if mutation == "ingress_removed":
            assert not receipt.thread_creations and not receipt.posts
            assert receipt.queue_in_flight == 0
        elif mutation == "title_stubbed":
            assert receipt.thread_creations[0].name == "Invented title"
            assert not receipt.naming_calls and not receipt.naming_meters
        else:
            assert len(receipt.naming_calls) == 1
            assert receipt.thread_creations[0].name == "Hello"
            assert re.search(
                r"(?i)^(greeting|hello|hi|help request|chat with .*)$",
                receipt.thread_creations[0].name,
            )
        assert not receipt.completion_certificate
    finally:
        await harness.close()


async def test_equal_text_messages_keep_distinct_ids_and_observe_production_queue(
    owned: OwnedIngress,
) -> None:
    harness = await start(owned, "anthropic")
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            trigger = payload(1525400123000000000, "hi! what can you do? I'm new here.")
            harness.deliver(trigger)
            await asyncio.wait_for(harness.http.first_post.wait(), 15)
            one = payload(1525400123000000001, "same text", channel_id=int(trigger["id"]))
            two = payload(1525400123000000002, "same text", channel_id=int(trigger["id"]))
            harness.deliver(one)
            harness.deliver(two)
            await asyncio.gather(*harness.bot.message_tasks[1:])
        queued = harness.bot._pending[int(trigger["id"])]  # pyright: ignore[reportPrivateUsage]
        observed_ids = tuple(str(message.id) for message in queued)
        assert len(observed_ids) == 2 and set(observed_ids) == {one["id"], two["id"]}
        assert [message.content for message in queued] == [one["content"], two["content"]]
        receipt = await harness.receipt()
        assert receipt.dispatched_message_ids == (trigger["id"], one["id"], two["id"])
        assert [row.message_id for row in receipt.gateway_messages] == list(
            receipt.dispatched_message_ids
        )
        assert receipt.queue_in_flight == receipt.callbacks_pending == 1
        assert receipt.pending_threads[0].message_ids == observed_ids
        print(
            "C7_QUEUE_ORDER="
            + json.dumps({"source_ids": [one["id"], two["id"]], "actual_pending_ids": observed_ids})
        )
    finally:
        await harness.close()


async def test_foreign_gateway_destination_is_refused_before_dispatch(owned: OwnedIngress) -> None:
    harness = await start(owned, "anthropic")
    try:
        foreign = payload(1525400123000000000, "foreign")
        foreign["guild_id"] = "999"
        with pytest.raises(ValueError, match="owned gateway guild"):
            harness.deliver(foreign)
        with pytest.raises(ValueError, match="unowned Discord HTTP"):
            harness.deliver(payload(1525400123000000001, "foreign", channel_id=999))
        assert not harness.gateway_messages and not harness.bot.message_tasks
        assert not owned.native.requests
    finally:
        await harness.close()


async def test_unbound_native_trace_and_closed_ingress_are_refused(owned: OwnedIngress) -> None:
    harness = await start(owned, "anthropic")
    try:
        with pytest.raises(ValueError, match="actual isolated SDK transport"):
            DiscordIngress(
                runtime=owned.runtime,
                scope=harness.scope,
                http=IngressHTTP(evidence_id="wrong", parent_channel_id=101),
                native=ScriptedTransport(),
            )
        await harness.close()
        with pytest.raises(ValueError, match="owned gateway guild"):
            harness.deliver(payload(1525400123000000000, "closed"))
        assert not harness.gateway_messages
    finally:
        await harness.close()


@pytest.mark.parametrize("routed", [False, True])
async def test_all_database_binds_are_checked_before_gateway_allocation(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test")
    async with owned.runtime.sessionmaker() as session:
        account = await get_account(session, owned.account_id)
    assert account is not None
    own = owned.runtime.sessionmaker.kw["bind"]
    factory = async_sessionmaker(
        own if routed else foreign,
        binds={type(account): foreign} if routed else None,
        expire_on_commit=False,
    )
    try:
        with pytest.raises(ValueError, match="owned"):
            DiscordIngress(
                runtime=dataclasses.replace(owned.runtime, sessionmaker=factory),
                scope=Scope(
                    tenant_id=str(owned.tenant_id),
                    account_id=str(owned.account_id),
                    principal_id="qa-operator:100",
                    authorization_id="qa-offline:100",
                ),
                http=IngressHTTP(evidence_id="bad-bind", parent_channel_id=101),
                native=owned.native,
            )
        assert not owned.native.requests
    finally:
        await foreign.dispose()


@pytest.mark.parametrize("foreign", ["tenant", "account", "workspace"])
async def test_foreign_owned_fixture_binding_is_refused(owned: OwnedIngress, foreign: str) -> None:
    # Configure the true parent, then try to bind a fresh ingress with a foreign
    # identity. This uses actual store reads, never a mocked authorization check.
    first = await start(owned, "anthropic")
    await first.close()
    harness = DiscordIngress(
        runtime=owned.runtime,
        scope=Scope(
            tenant_id=str(uuid.uuid4()) if foreign == "tenant" else str(owned.tenant_id),
            account_id=str(uuid.uuid4()) if foreign == "account" else str(owned.account_id),
            principal_id="qa-operator:100",
            authorization_id="qa-offline:100",
        ),
        http=IngressHTTP(evidence_id="bad-scope", parent_channel_id=101),
        native=owned.native,
    )
    try:
        with pytest.raises(ValueError, match="genuinely owned"):
            await harness.open(
                guild={"id": "999" if foreign == "workspace" else "100"},
                channel={"id": "101"},
                bot_user={"id": "104"},
            )
        assert not harness.gateway_messages and not harness.http.creations
        assert not owned.native.requests
    finally:
        await harness.close()


@pytest.mark.parametrize("dependency", ["sessionmaker", "anthropic", "messages"])
@pytest.mark.parametrize("boundary", ["construct", "open", "deliver", "callback", "receipt"])
async def test_reachable_nested_dependencies_refuse_before_admission(
    owned: OwnedIngress, dependency: str, boundary: str
) -> None:
    """A valid outer bundle must not authorize a replaced nested dependency."""
    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test")
    connections: list[bool] = []

    def trap_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise RuntimeError("foreign connection reached")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    outside = ScriptedTransport()
    outside_sdk = outside.client()
    bad_dependency = (
        async_sessionmaker(foreign, expire_on_commit=False)
        if dependency == "sessionmaker"
        else outside_sdk
    )
    bad = dataclasses.replace(
        owned.runtime,
        turn_deps=dataclasses.replace(owned.runtime.turn_deps, **{dependency: bad_dependency}),
    )
    harness: DiscordIngress | None = None
    try:
        if boundary == "construct":
            with pytest.raises(ValueError):
                DiscordIngress(
                    runtime=bad,
                    scope=Scope(
                        tenant_id=str(owned.tenant_id),
                        account_id=str(owned.account_id),
                        principal_id="qa-operator:100",
                        authorization_id="qa-offline:100",
                    ),
                    http=IngressHTTP(evidence_id="nested", parent_channel_id=101),
                    native=owned.native,
                )
        elif boundary == "open":
            harness = DiscordIngress(
                runtime=owned.runtime,
                scope=Scope(
                    tenant_id=str(owned.tenant_id),
                    account_id=str(owned.account_id),
                    principal_id="qa-operator:100",
                    authorization_id="qa-offline:100",
                ),
                http=IngressHTTP(evidence_id="nested", parent_channel_id=101),
                native=owned.native,
            )
            harness.bot.runtime = bad
            with pytest.raises(ValueError):
                await harness.open(guild={}, channel={}, bot_user={})
        else:
            harness = await start(owned, "anthropic")
            if boundary == "callback":
                harness.deliver(payload(1525400123000000000, "guarded"))
                # No yield: Discord scheduled the callback, but it has not entered admission.
                harness.bot.runtime = bad
                await harness.wait()
                assert harness.bot.ingress_errors == ["on_message"]
            else:
                harness.bot.runtime = bad
                with pytest.raises(ValueError):
                    if boundary == "deliver":
                        harness.deliver(payload(1525400123000000000, "guarded"))
                    else:
                        await harness.receipt()
            assert not harness.http.creations and not harness.http.posts
        assert not connections and not outside.requests and not owned.native.requests
    finally:
        if harness is not None:
            await harness.close()
        await outside_sdk.close()
        await foreign.dispose()


async def test_nested_routed_bind_refused_before_bot_allocation(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.adapters.discord.bot import DaimonBot

    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test")
    connections: list[bool] = []
    allocations: list[bool] = []

    def trap_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise RuntimeError("foreign routed connection reached")

    def trap_allocation(*args: object, **kwargs: object) -> None:
        allocations.append(True)
        raise RuntimeError("bot allocated before nested routing guard")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    async with owned.runtime.sessionmaker() as session:
        account = await get_account(session, owned.account_id)
    assert account is not None
    own = owned.runtime.sessionmaker.kw["bind"]
    nested = async_sessionmaker(own, binds={type(account): foreign}, expire_on_commit=False)
    monkeypatch.setattr(DaimonBot, "__init__", trap_allocation)
    try:
        with pytest.raises(ValueError, match="owned"):
            DiscordIngress(
                runtime=dataclasses.replace(
                    owned.runtime,
                    turn_deps=dataclasses.replace(owned.runtime.turn_deps, sessionmaker=nested),
                ),
                scope=Scope(
                    tenant_id=str(owned.tenant_id),
                    account_id=str(owned.account_id),
                    principal_id="qa-operator:100",
                    authorization_id="qa-offline:100",
                ),
                http=IngressHTTP(evidence_id="nested-routed", parent_channel_id=101),
                native=owned.native,
            )
        assert not connections and not allocations and not owned.native.requests
    finally:
        await foreign.dispose()


@pytest.mark.parametrize(
    "dependency",
    [
        "turn_runtime_factory",
        "default_factory",
        "turn_runtimes",
        "backend",
        "backend_session_ref",
        "state_store",
    ],
)
@pytest.mark.parametrize("boundary", ["construct", "open", "deliver", "callback", "receipt"])
async def test_opaque_runtime_dependencies_refuse_before_real_event(
    owned: OwnedIngress,
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
    boundary: str,
) -> None:
    from daimon.core.mux_backend import TurnRuntime, managed_agents
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn import runtimes as runtime_module

    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/foreign_test")
    connections: list[bool] = []
    calls: list[bool] = []
    outside = ScriptedTransport()
    outside_sdk = outside.client()

    def trap_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise RuntimeError("foreign store connection reached")

    def trap_runtime(*args: object, **kwargs: object) -> Never:
        calls.append(True)
        raise RuntimeError("uncaptured runtime dependency reached")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    # The genuine default resolver is safe even under a broken guard: its
    # registered composition is trapped before deployment/credential discovery.
    monkeypatch.setitem(
        runtime_module._RUNTIME_FACTORIES,  # pyright: ignore[reportPrivateUsage]
        "openai.persistent_workspace",
        trap_runtime,
    )
    replacements: dict[str, object] = {
        "turn_runtime_factory": trap_runtime,
        "default_factory": runtime_module.build_channel_runtime,
        "turn_runtimes": {
            "openai.persistent_workspace": TurnRuntime(trap_runtime, object(), object())
        },
        "backend": managed_agents(outside_sdk),
        "backend_session_ref": trap_runtime,
        "state_store": PostgresStateStore(async_sessionmaker(foreign)),
    }
    field = "turn_runtime_factory" if dependency == "default_factory" else dependency
    bad = dataclasses.replace(
        owned.runtime,
        turn_deps=dataclasses.replace(owned.runtime.turn_deps, **{field: replacements[dependency]}),
    )
    harness: DiscordIngress | None = None
    try:
        if boundary in ("construct", "open"):
            if boundary == "construct":
                with pytest.raises(ValueError, match="absent injected"):
                    DiscordIngress(
                        runtime=bad,
                        scope=Scope(
                            tenant_id=str(owned.tenant_id),
                            account_id=str(owned.account_id),
                            principal_id="qa-operator:100",
                            authorization_id="qa-offline:100",
                        ),
                        # Unheld is deliberate: pause configuration cannot authorize a runtime.
                        http=IngressHTTP(evidence_id="opaque", parent_channel_id=101),
                        native=owned.native,
                    )
            else:
                harness = DiscordIngress(
                    runtime=owned.runtime,
                    scope=Scope(
                        tenant_id=str(owned.tenant_id),
                        account_id=str(owned.account_id),
                        principal_id="qa-operator:100",
                        authorization_id="qa-offline:100",
                    ),
                    http=IngressHTTP(evidence_id="opaque", parent_channel_id=101),
                    native=owned.native,
                )
                harness.bot.runtime = bad
                with pytest.raises(ValueError, match="absent injected"):
                    await harness.open(guild={}, channel={}, bot_user={})
        else:
            harness = await start(owned, "openai")
            harness.http.release_first_post.set()
            if boundary == "callback":
                harness.deliver(payload(1525400123000000000, "guarded"))
                harness.bot.runtime = bad  # replace after dispatch, before the scheduled callback
                await harness.wait()
                assert harness.bot.ingress_errors == ["on_message"]
            else:
                harness.bot.runtime = bad
                with pytest.raises(ValueError, match="absent injected"):
                    if boundary == "deliver":
                        harness.deliver(payload(1525400123000000000, "guarded"))
                    else:
                        await harness.receipt()
            assert not harness.http.creations and not harness.http.posts
        assert not calls and not connections and not outside.requests and not owned.native.requests
    finally:
        if harness is not None:
            await harness.close()
        await outside_sdk.close()
        await foreign.dispose()


@pytest.mark.parametrize("local_contributor", [False, True])
async def test_actual_ingress_fixture_preflight_precedes_any_connection(
    tmp_path: Path, local_contributor: bool
) -> None:
    from packages.adapters.discord.tests.fixture_preflight_probe import run_fixture_preflight_probe

    code, output, connected = await run_fixture_preflight_probe(
        tmp_path=tmp_path,
        target=__file__ + "::test_real_anthropic_path_reads_original_channel_context",
        local_contributor=local_contributor,
    )
    assert not connected, output
    if local_contributor:
        assert code == 0 and "1 skipped" in output
    else:
        assert code != 0 and "owned local lane" in output
    assert "connection trap reached" not in output


@dataclasses.dataclass
class BusyRoot:
    session_id: str
    root_id: str
    input_json: str
    answer: str
    entered: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    completed: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    records: list[dict[str, Any]] = dataclasses.field(default_factory=list[dict[str, Any]])


class BusyStream(httpx.AsyncByteStream):
    """Real SDK stream waits after consuming a native running event."""

    def __init__(self, wire: BusyWire, session_id: str) -> None:
        self.wire, self.session_id = wire, session_id
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        from anthropic.types.beta.sessions import (
            BetaManagedAgentsAgentMessageEvent,
            BetaManagedAgentsSessionEndTurn,
            BetaManagedAgentsSessionStatusIdleEvent,
            BetaManagedAgentsSessionStatusRunningEvent,
            BetaManagedAgentsTextBlock,
        )

        root = await self.wire.waiting.setdefault(self.session_id, asyncio.Queue()).get()
        running = BetaManagedAgentsSessionStatusRunningEvent(
            id=f"{root.root_id}:running",
            processed_at=datetime.now(UTC),
            type="session.status_running",
        )
        raw = running.model_dump(mode="json")
        root.records.append(raw)
        self.wire.state.events[self.session_id].append(raw)
        yield f"event: {raw['type']}\ndata: {json.dumps(raw)}\n\n".encode()
        # SDK requested the next event after folding the native running fact.
        root.entered.set()
        await root.release.wait()
        answer = BetaManagedAgentsAgentMessageEvent(
            id=f"{root.root_id}:answer",
            processed_at=datetime.now(UTC),
            type="agent.message",
            content=[BetaManagedAgentsTextBlock(type="text", text=root.answer)],
        )
        idle = BetaManagedAgentsSessionStatusIdleEvent(
            id=f"{root.root_id}:idle",
            processed_at=datetime.now(UTC),
            type="session.status_idle",
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        )
        for native_event in (answer, idle):
            raw = native_event.model_dump(mode="json")
            root.records.append(raw)
            self.wire.state.events[self.session_id].append(raw)
            if native_event.type == "session.status_idle":
                root.completed.set()
            yield f"event: {raw['type']}\ndata: {json.dumps(raw)}\n\n".encode()
        self.wire.state.sessions[self.session_id] = self.wire.state.sessions[
            self.session_id
        ].model_copy(update={"status": "idle"})
        root.completed.set()

    async def aclose(self) -> None:
        self.closed = True


class BusyWire:
    """Owned fake MA server; captures roots from actual SDK send acknowledgments."""

    def __init__(self, owned: OwnedIngress, answers: list[str]) -> None:
        from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
        from mux.testing.drivers.anthropic.ma import FakeMAState, make_fake_memory_store_handler

        agent = ma_agent(tenant_id=owned.tenant_id, name="qa-agent")
        self.state = FakeSessionsState(
            ma=FakeMAState(
                agents={agent.id: agent.model_dump(mode="json")},
                agent_versions={agent.id: {agent.version: agent.model_dump(mode="json")}},
            )
        )
        self.handler = make_fake_sessions_handler(self.state)
        self.roots: list[BusyRoot] = []
        self.answers = answers
        self.waiting: dict[str, asyncio.Queue[BusyRoot]] = {}
        self.streams: list[BusyStream] = []
        router = owned.native.router
        assert router is not None
        memory = make_fake_memory_store_handler()
        for method in ("GET", "POST", "PATCH", "DELETE"):
            router.add(
                method, r"/v1/(sessions|files).*", lambda request, match: self.handle(request)
            )
            router.add(method, r"/v1/memory_stores.*", lambda request, match: memory(request))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/stream") and request.method == "GET":
            session_id = request.url.path.split("/")[-3]
            stream = BusyStream(self, session_id)
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/events"):
            session_id = request.url.path.split("/")[-2]
            if request.method == "GET" and request.url.params.get("stream") == "true":
                stream = BusyStream(self, session_id)
                self.streams.append(stream)
                return httpx.Response(
                    200, stream=stream, headers={"content-type": "text/event-stream"}
                )
            if request.method == "POST":
                result = self.handler(request)
                assert result.status_code == 200
                user = next(row for row in result.json()["data"] if row["type"] == "user.message")
                if len(self.roots) >= len(self.answers):
                    raise AssertionError("unexpected additional native turn")
                root = BusyRoot(
                    session_id, user["id"], request.content.decode(), self.answers[len(self.roots)]
                )
                root.records.append(user)
                self.roots.append(root)
                self.state.sessions[session_id] = self.state.sessions[session_id].model_copy(
                    update={"status": "running"}
                )
                self.waiting.setdefault(session_id, asyncio.Queue()).put_nowait(root)
                return result
        return self.handler(request)

    async def running(self, count: int) -> BusyRoot:
        async with asyncio.timeout(30):
            while len(self.roots) < count:
                await asyncio.sleep(0.01)
            root = self.roots[count - 1]
            await root.entered.wait()
            return root


class InterleavingClock:
    def __init__(self) -> None:
        import time

        self.monotonic = time.monotonic
        self.offset = 0.0

    def now(self) -> float:
        return self.monotonic() + self.offset

    def utc(self) -> datetime:
        from datetime import timedelta

        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.offset += seconds


def post_text(checkpoint: ConcurrentCheckpoint, thread_id: str) -> str:
    return "\n".join(
        "\n".join((p.message.content, *p.message.embed_texts))
        for p in checkpoint.ingress.posts
        if p.message.channel_id == thread_id
    )


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_busy_native_root_queues_then_continues_same_actual_session(
    owned: OwnedIngress, backend: Provider
) -> None:
    from daimon.testing.discord_ingress import DiscordConcurrentDriver

    if backend != "anthropic":
        pytest.skip("PENDING: explicit owned alternate-provider native capture binding")
    clock = InterleavingClock()
    harness = await start(owned, backend, utc_clock=clock.utc)
    wire = BusyWire(owned, ["FIRST-DONE", "SECOND-DONE"])
    harness.http.clock = clock.now
    for surface in harness.http.surfaces.values():
        surface.clock = clock.now
    harness.bot.turn_queue.clock = clock.now
    driver = DiscordConcurrentDriver(harness)
    first = payload(
        1525400124000000000, "QA queue. Run `sleep 30` with bash, then reply with only: FIRST-DONE"
    )
    second = payload(
        1525400124000000001,
        "QA queue follow-up: also reply with only SECOND-DONE",
        channel_id=int(first["id"]),
    )
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(first)
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            root1 = await wire.running(1)
            assert not root1.completed.is_set()
            clock.advance(8)
            before = await driver.deliver_while_busy(second)
            first_card_id = before.live_bindings[0].active_card_id
            assert first_card_id is not None
            assert any(
                p.message.message_id == first_card_id and "Stop" in p.message.component_labels
                for p in before.ingress.posts
            )
            queued = await driver.wait_for(
                "follow-up-queued", lambda c: bool(c.ingress.pending_threads)
            )
            assert queued.ingress.pending_threads[0].message_ids == (second["id"],)
            assert len(wire.roots) == 1 and not root1.release.is_set()
            await asyncio.wait_for(harness.bot.message_tasks[1], 30)
            queued = await driver.checkpoint("reaction-confirmed")
            assert any(
                r.message_id == second["id"] and r.emoji == "⌛" and r.operation == "add"
                for r in queued.ingress.reaction_history
            )
            clock.advance(22)
            root1.release.set()
            root2 = await wire.running(2)
            assert root1.completed.is_set()
            assert root2.session_id == root1.session_id and root2.root_id != root1.root_id
            assert "SECOND-DONE" in root2.input_json
            assert queued.ingress.gateway_messages[-1].message_id == second["id"]
            assert (
                json.loads(queued.ingress.gateway_messages[-1].payload_json)["id"] == second["id"]
            )
            continued = await driver.checkpoint("actual-continuation-running")
            assert any(
                p.operation == "edit"
                and p.message.message_id == first_card_id
                and "Stop" not in p.message.component_labels
                for p in continued.ingress.posts
            )
            root2.release.set()
            await asyncio.wait_for(harness.wait(), 30)
            final = await driver.checkpoint("all-callbacks-finished")
        assert not owned.native.violations and not final.ingress.errors
        assert len(final.live_bindings) == 1
        assert "FIRST-DONE" in post_text(final, first["id"])
        assert "SECOND-DONE" in post_text(final, first["id"])
        assert (
            final.ingress.queue_depth
            == final.ingress.queue_in_flight
            == final.ingress.callbacks_pending
            == 0
        )
        assert not final.ingress.pending_threads
        assert all(s.closed for s in wire.streams)
        print(
            "C7_BUSY_RECEIPT="
            + json.dumps(
                {
                    "backend": backend,
                    "whole_cell": "PENDING",
                    "reason": "CONTROLLED_NATIVE_RECIPE_NOT_ORACLE_PINNED",
                    "roots": [
                        {
                            "session_id": r.session_id,
                            "root_id": r.root_id,
                            "input_json": r.input_json,
                            "records": r.records,
                        }
                        for r in wire.roots
                    ],
                    "checkpoints": [
                        c.model_dump(mode="json") for c in (before, queued, continued, final)
                    ],
                }
            )
        )
    finally:
        for root in wire.roots:
            root.release.set()
        await harness.close()


@pytest.mark.parametrize("queue_card", [False, True], ids=["default", "opt-in"])
@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_real_cap_admin_three_admissions_wait_then_release(
    owned: OwnedIngress, backend: Provider, queue_card: bool
) -> None:
    from daimon.testing.admin_bindings import AdminBindings
    from daimon.testing.discord_ingress import DiscordConcurrentDriver

    if backend != "anthropic":
        pytest.skip("PENDING: explicit owned alternate-provider native capture binding")
    discord_settings = owned.runtime.settings.discord
    assert discord_settings is not None
    discord_settings.tenant_cap_waiting_card = queue_card
    clock = InterleavingClock()
    harness = await start(owned, backend, utc_clock=clock.utc)
    wire = BusyWire(owned, ["C1", "C2", "C3"])
    harness.http.clock = clock.now
    for surface in harness.http.surfaces.values():
        surface.clock = clock.now
    harness.bot.turn_queue.clock = clock.now
    admin = AdminBindings(
        sessions=owned.runtime.sessionmaker,
        scope=harness.scope,
        backend=backend,
        platform="discord",
        workspace_id="100",
        channels=frozenset({"101"}),
        trace=ScriptedTransport(),
    )
    driver = DiscordConcurrentDriver(harness)
    source = {"do": "admin", "tool": "cli", "args": "daimon tenants turn-cap discord {guild_id} 2"}
    setup = await admin.execute(source, {"guild_id": "100"})
    assert setup.status == "executed" and setup.observations["turn_cap"] == 2
    messages = [
        payload(1525400125000000000 + i, text)
        for i, text in enumerate(
            [
                "QA cap 1: run `sleep 30`, then reply with only C1",
                "QA cap 2: run `sleep 30`, then reply with only C2",
                "QA cap 3: reply with only C3",
            ]
        )
    ]
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(messages[0])
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            one = await wire.running(1)
            clock.advance(1)
            await driver.deliver_while_busy(messages[1])
            two = await wire.running(2)
            clock.advance(1)
            before = await driver.deliver_while_busy(messages[2])
            waiting = await driver.wait_for(
                "at-cap-third-card",
                lambda c: (
                    c.ingress.queue_depth == 1
                    and len(c.ingress.thread_creations) == 3
                    and any(p.message.channel_id == messages[2]["id"] for p in c.ingress.posts)
                ),
            )
            assert before.ingress.queue_in_flight == waiting.ingress.queue_in_flight == 2
            assert len(wire.roots) == 2 and not one.release.is_set() and not two.release.is_set()
            assert waiting.ingress.callbacks_pending == 3
            assert len(waiting.live_bindings) == 2
            await asyncio.sleep(15.1)
            waiting = await driver.checkpoint("full-real-queue-copy-window")
            text = post_text(waiting, messages[2]["id"])
            visible_queue = (
                re.search(r"(?i)queued|in line|waiting for (a )?(free )?slot|busy", text)
                is not None
            )
            assert visible_queue is queue_card, (
                "actual tenant-cap waiting card must match its profile"
            )
            third_posts = [
                p
                for p in waiting.ingress.posts
                if p.message.channel_id == messages[2]["id"] and p.operation == "send"
            ]
            assert len(third_posts) == 1  # reuse the already-existing card, never a notice
            # The running turns' cards keep their elapsed timer moving; the waiting
            # turn's card is never edited while it waits.
            assert not any(
                p.operation == "edit" and p.message.channel_id == messages[2]["id"]
                for p in waiting.ingress.posts
            )
            assert all("Queued" not in post_text(waiting, m["id"]) for m in messages[:2])
            assert not re.search(r"(?i)too many chats at once|try again later", text)
            clock.advance(29)
            one.release.set()
            three = await wire.running(3)
            released = await driver.checkpoint("third-root-runs-second-still-busy")
            assert one.completed.is_set() and not two.completed.is_set()
            assert three.session_id not in {one.session_id, two.session_id}
            assert released.ingress.queue_depth == 0 and released.ingress.queue_in_flight == 2
            if queue_card:
                cleared = [
                    e
                    for e in released.ingress.posts
                    if e.message.channel_id == messages[2]["id"] and e.operation == "edit"
                ]
                assert (
                    cleared and cleared[0].message.message_id == third_posts[0].message.message_id
                )
                assert "Working on it…" in cleared[0].request_json
                assert "Queued" not in cleared[0].request_json
            two.release.set()
            three.release.set()
            await asyncio.wait_for(harness.wait(), 30)
            final = await driver.checkpoint("all-cap-callbacks-finished")
        assert len(final.live_bindings) == len(wire.roots) == 3
        assert (
            final.ingress.queue_depth
            == final.ingress.queue_in_flight
            == final.ingress.callbacks_pending
            == 0
        )
        assert all(f"C{i + 1}" in post_text(final, m["id"]) for i, m in enumerate(messages))
        assert not admin.trace.requests and not owned.native.violations
        print(
            "C7_CAP_RECEIPT="
            + json.dumps(
                {
                    "backend": backend,
                    "settings_flag": {"discord.tenant_cap_waiting_card": queue_card},
                    "queue_copy": "PASS under opt-in profile" if queue_card else "FAIL",
                    "whole_cell": "PENDING",
                    "reason": (
                        "OPT_IN_ACTUAL_WAITING_CARD_QUEUE_COPY_NATIVE_RECIPE_UNBOUND"
                        if queue_card
                        else "ACTUAL_WAITING_CARD_HAS_NO_QUEUE_COPY"
                    ),
                    "setup": setup.model_dump(mode="json"),
                    "roots": [
                        {
                            "session_id": r.session_id,
                            "root_id": r.root_id,
                            "input_json": r.input_json,
                            "records": r.records,
                        }
                        for r in wire.roots
                    ],
                    "checkpoints": [
                        c.model_dump(mode="json") for c in (before, waiting, released, final)
                    ],
                }
            )
        )
    finally:
        for root in wire.roots:
            root.release.set()
        await harness.close()
        restore = await admin.execute(
            {**source, "args": "daimon tenants turn-cap discord {guild_id} default"},
            {"guild_id": "100"},
        )
        assert restore.status == "executed" and restore.observations["turn_cap"] is None
        await admin.close()


async def test_real_cap_queue_render_witness_rejects_missing_product_hook(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "tenant_slot_waiter", lambda queue, tenant: lambda: False)
    with pytest.raises(
        AssertionError, match="actual tenant-cap waiting card must match its profile"
    ):
        await test_real_cap_admin_three_admissions_wait_then_release(owned, "anthropic", True)


@pytest.mark.parametrize("mutation", ["thread_enqueue_dropped", "tenant_cap_ignored"])
async def test_concurrent_hook_detects_production_queue_mutations(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from daimon.core.turn.thread_queue import ThreadQueue
    from daimon.testing.admin_bindings import AdminBindings
    from daimon.testing.discord_ingress import DiscordConcurrentDriver

    harness = await start(owned, "anthropic")
    wire = BusyWire(owned, ["FIRST", "SECOND", "THIRD"])
    driver = DiscordConcurrentDriver(harness)
    admin = AdminBindings(
        sessions=owned.runtime.sessionmaker,
        scope=harness.scope,
        backend="anthropic",
        platform="discord",
        workspace_id="100",
        channels=frozenset({"101"}),
        trace=ScriptedTransport(),
    )
    cap_source = {
        "do": "admin",
        "tool": "cli",
        "args": "daimon tenants turn-cap discord {guild_id} 2",
    }
    cap = await admin.execute(cap_source, {"guild_id": "100"})
    assert cap.status == "executed"
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            first = payload(1525400126000000000, "actual first root")
            harness.deliver(first)
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            root = await wire.running(1)
            if mutation == "thread_enqueue_dropped":

                def drop_enqueue(self: Any, key: Any, message: Any) -> None:
                    pass

                monkeypatch.setattr(ThreadQueue, "enqueue", drop_enqueue)
                await driver.deliver_while_busy(
                    payload(1525400126000000001, "queued follow-up", channel_id=int(first["id"]))
                )
                await asyncio.wait_for(harness.bot.message_tasks[1], 30)
                with pytest.raises(TimeoutError):
                    await driver.wait_for(
                        "must-observe-real-enqueue",
                        lambda c: bool(c.ingress.pending_threads),
                        timeout_s=0.25,
                    )
                witness = await driver.checkpoint("mutated-thread-queue")
                assert not witness.ingress.pending_threads and len(wire.roots) == 1
                # The cosmetic hourglass alone cannot certify queue admission.
                assert any(r.emoji == "⌛" for r in witness.ingress.reaction_history)
                root.release.set()
            else:
                await driver.deliver_while_busy(payload(1525400126000000001, "actual second root"))
                await wire.running(2)
                original = harness.bot.turn_queue.admit

                def ignore_cap(tenant_id: uuid.UUID, **kwargs: Any) -> Any:
                    kwargs["cap"] = 50
                    return original(tenant_id, **kwargs)

                monkeypatch.setattr(harness.bot.turn_queue, "admit", ignore_cap)
                await driver.deliver_while_busy(payload(1525400126000000002, "third admission"))
                await wire.running(3)
                with pytest.raises(TimeoutError):
                    await driver.wait_for(
                        "must-observe-real-cap-queue",
                        lambda c: c.ingress.queue_depth == 1,
                        timeout_s=0.25,
                    )
                witness = await driver.checkpoint("mutated-cap")
                assert witness.ingress.queue_in_flight == 3 and witness.ingress.queue_depth == 0
                assert all(not r.completed.is_set() for r in wire.roots)
                for r in wire.roots:
                    r.release.set()
            await asyncio.wait_for(harness.wait(), 30)
        final = await driver.checkpoint("mutation-finished")
        assert not final.ingress.errors and final.ingress.queue_in_flight == 0
        assert not final.ingress.completion_certificate
    finally:
        for root in wire.roots:
            root.release.set()
        await harness.close()
        await admin.execute(
            {**cap_source, "args": "daimon tenants turn-cap discord {guild_id} default"},
            {"guild_id": "100"},
        )
        await admin.close()


async def test_initial_card_without_native_binding_is_not_a_busy_turn(owned: OwnedIngress) -> None:
    from daimon.testing.discord_ingress import DiscordConcurrentDriver

    harness = await start(owned, "anthropic")
    driver = DiscordConcurrentDriver(harness)
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(
                payload(1525400127000000000, "first card held before native allocation")
            )
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            with pytest.raises(ValueError, match="actual active host turn"):
                await driver.deliver_while_busy(payload(1525400127000000001, "second message"))
        assert len(harness.gateway_messages) == 1
        assert not driver.checkpoints[-1].live_bindings
    finally:
        await harness.close()


@pytest.mark.parametrize("dependency", ["sessionmaker", "nested_sdk", "state_store"])
async def test_concurrent_checkpoint_revalidates_after_actual_receipt(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch, dependency: str
) -> None:
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.testing.discord_ingress import DiscordConcurrentDriver

    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/foreign_test")
    connections: list[bool] = []

    def trap_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise RuntimeError("foreign checkpoint connection reached")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    outside = ScriptedTransport()
    outside_sdk = outside.client()
    harness = await start(owned, "anthropic")
    driver = DiscordConcurrentDriver(harness)
    original = harness.receipt

    async def replace_after_receipt() -> Any:
        receipt = await original()
        if dependency == "sessionmaker":
            harness.bot.runtime = dataclasses.replace(
                owned.runtime, sessionmaker=async_sessionmaker(foreign)
            )
        else:
            replacement = (
                {"anthropic": outside_sdk}
                if dependency == "nested_sdk"
                else {"state_store": PostgresStateStore(async_sessionmaker(foreign))}
            )
            harness.bot.runtime = dataclasses.replace(
                owned.runtime,
                turn_deps=dataclasses.replace(owned.runtime.turn_deps, **replacement),
            )
        return receipt

    monkeypatch.setattr(harness, "receipt", replace_after_receipt)
    try:
        with pytest.raises(ValueError):
            await driver.checkpoint("actual-await-replaced-runtime")
        assert not connections and not outside.requests and not owned.native.requests
        assert not driver.checkpoints
    finally:
        await harness.close()
        await outside_sdk.close()
        await foreign.dispose()
