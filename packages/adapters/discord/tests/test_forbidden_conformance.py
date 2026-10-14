"""C4 offline conformance: real ingress/admission/Discord serialization, owned offline edge."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, cast
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
import sentry_sdk
from daimon.adapters.discord import bot as bot_module
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
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
from daimon.testing import ma_agent, ma_environment
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma_transport import ScriptedTransport
from discord.http import HTTPClient
from mux.contracts.config import BackendConfig, ConfigRevision
from mux.contracts.ids import Provider, Scope
from pydantic import PostgresDsn, SecretStr, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.adapters.discord.tests.forbidden_hook import (
    HOST_THREAD_NAMING_REQUEST,
    DiscordForbiddenHook,
    ForbiddenGateway,
    ForbiddenReceipt,
    OverwriteReceipt,
    evaluate_new14,
)

if TYPE_CHECKING:
    from discord.types.channel import TextChannel as TextChannelPayload
    from discord.types.guild import Guild as GuildPayload
    from discord.types.message import Message as MessagePayload
    from discord.types.user import User as UserPayload


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
    bot: DaimonBot
    message: discord.Message
    gateway: ForbiddenGateway
    native: ScriptedTransport
    tenant_id: uuid.UUID
    account_id: uuid.UUID


def require_owned_or_skip_contributor(factory: async_sessionmaker[AsyncSession]) -> None:
    try:
        require_owned_sessionmaker(factory)
    except ValueError:
        binding = factory.kw.get("bind")
        engine = binding.engine if isinstance(binding, AsyncConnection) else binding
        if (
            isinstance(engine, AsyncEngine)
            and engine.url.get_backend_name() == "postgresql"
            and engine.url.host in ("localhost", "127.0.0.1")
            and engine.url.database == "daimon_test"
            and not engine.url.query
            and factory.kw.get("binds") is None
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("requires an isolated test database; plain local contributor database")
        raise


@pytest.fixture
async def ingress(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[OwnedIngress]:
    require_owned_or_skip_contributor(db_session_factory)
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
            idempotency_key="c4-owned-credit",
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
                "id": "msg_c4_owned_title",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [{"type": "text", "text": "QA permission check"}],
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
    bot = DaimonBot(runtime=runtime, intents=discord.Intents.none())
    state = bot._connection  # pyright: ignore[reportPrivateUsage]
    state.user = discord.ClientUser(
        state=state,
        data=cast(
            "UserPayload",
            {
                "id": "104",
                "username": "Daimon",
                "discriminator": "0000",
                "avatar": None,
                "bot": True,
            },
        ),
    )
    guild = discord.Guild(
        state=state,
        data=cast(
            "GuildPayload",
            {
                "id": "100",
                "name": "owned QA",
                "roles": [],
                "emojis": [],
                "stickers": [],
            },
        ),
    )
    channel = discord.TextChannel(
        state=state,
        guild=guild,
        data=cast(
            "TextChannelPayload",
            {
                "id": "101",
                "name": "owned-parent",
                "type": 0,
                "position": 0,
                "permission_overwrites": [],
            },
        ),
    )
    state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
    guild._add_channel(channel)  # pyright: ignore[reportPrivateUsage]
    gateway = ForbiddenGateway(evidence_id="c4-owned", channel_id=101, clock=time.monotonic)
    state.http = cast(HTTPClient, gateway)
    bot.http = cast(HTTPClient, gateway)
    message = discord.Message(
        state=state,
        channel=channel,
        data=cast(
            "MessagePayload",
            {
                "id": "1525400123000000000",
                "channel_id": "101",
                "type": 0,
                "content": "<@104> QA no-thread-perm: hello",
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
                ],
                "mention_roles": [],
                "mention_everyone": False,
                "pinned": False,
                "timestamp": "2026-10-10T20:00:00+00:00",
                "edited_timestamp": None,
            },
        ),
    )
    try:
        yield OwnedIngress(bot, message, gateway, native, tenant.id, account.id)
    finally:
        await drain_outcomes()
        await client.close()


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_real_forbidden_path(ingress: OwnedIngress, backend: Provider) -> None:
    hook, overwrite = await configured_hook(ingress, backend)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
        receipt = await hook.mention(ingress.message)
    assert overwrite.backend_revision.backend == receipt.backend == backend
    assert len(ingress.gateway.creations) == 1
    assert len(receipt.parent_posts) == 1
    post = receipt.parent_posts[0]
    assert post.operation == "send" and post.message.channel_id == "101"
    assert post.message.content == "Couldn't open a thread. @mention Daimon again."
    wire = json.loads(post.request_json)
    assert wire["message_reference"]["message_id"] == ingress.message.id
    assert wire["allowed_mentions"]["replied_user"] is False
    assert receipt.native_send_count == 0
    checks = evaluate_new14(receipt)
    assert checks.no_silent_drop and checks.zero_native_sends
    assert not checks.retry_text_absent and not checks.permission_text_present
    assert checks.verdict == "FAIL"
    assert receipt.queue_empty_after and receipt.processing_empty_after
    print(
        "C4_REAL_RECEIPT="
        + json.dumps(
            {
                "receipt": receipt.model_dump(mode="json"),
                "checks": checks.model_dump(mode="json"),
                "verdict": checks.verdict,
            }
        )
    )


async def ready_hook(
    ingress: OwnedIngress, backend: Provider = "anthropic"
) -> DiscordForbiddenHook:
    profiles = {
        "anthropic": "anthropic.managed_agents",
        "openai": "openai.persistent_workspace",
        "gemini": "gemini.inline_reuse",
    }
    models = {"anthropic": None, "openai": "gpt-6-luna", "gemini": "gemini-3.8-flash"}
    async with policy_write_transaction(
        ingress.bot.runtime.sessionmaker, tenant_id=ingress.tenant_id
    ) as session:
        policy = await load_access_policy(session, tenant_id=ingress.tenant_id)
        if backend not in policy.enabled_backends:
            await set_access_policy(
                session,
                tenant_id=ingress.tenant_id,
                policy=type(policy).model_validate(
                    {**policy.model_dump(), "enabled_backends": (*policy.enabled_backends, backend)}
                ),
            )
        await set_channel_backend(
            session,
            channel_ref(ingress.tenant_id, "discord", "101"),
            BackendConfig(backend=backend, profile=profiles[backend], model=models[backend]),
        )
    return DiscordForbiddenHook(bot=ingress.bot, gateway=ingress.gateway, native=ingress.native)


async def configured_hook(
    ingress: OwnedIngress, backend: Provider = "anthropic"
) -> tuple[DiscordForbiddenHook, OverwriteReceipt]:
    hook = await ready_hook(ingress, backend)
    overwrite = await hook.discord_overwrite_edge(
        scope=Scope(
            tenant_id=str(ingress.tenant_id),
            account_id=str(ingress.account_id),
            principal_id="qa-admin:100",
            authorization_id="qa-offline:100",
        ),
        owned_workspace_id="100",
        owned_channel_id="101",
        source={
            "do": "admin",
            "tool": "discord_overwrite",
            "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
        },
        capture_started_s=time.monotonic(),
    )
    return hook, overwrite


@pytest.mark.parametrize("mutation", ["notice_removed", "copy_corrected", "native_write"])
async def test_actual_error_path_mutations(
    ingress: OwnedIngress, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    hook, _ = await configured_hook(ingress)
    original = bot_module._explain_thread_open_failure  # pyright: ignore[reportPrivateUsage]
    if mutation == "notice_removed":
        monkeypatch.setattr(bot_module, "_explain_thread_open_failure", AsyncMock())
    elif mutation == "copy_corrected":
        monkeypatch.setattr(
            bot_module, "THREAD_OPEN_FAILED_NOTICE", "Missing permission: Create Public Threads."
        )
    else:
        from daimon.testing.ma_transport import ScriptedReply

        ingress.native.replies.append(
            ScriptedReply(method="POST", path="/c4-mutant", response=httpx.Response(200, json={}))
        )

        async def notice_after_native_write(message: discord.Message) -> None:
            await ingress.bot.runtime.anthropic._client.post("/c4-mutant", json={})  # pyright: ignore[reportPrivateUsage]
            await original(message)

        monkeypatch.setattr(bot_module, "_explain_thread_open_failure", notice_after_native_write)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
        receipt = await hook.mention(ingress.message)
    checks = evaluate_new14(receipt)
    assert len(ingress.gateway.creations) == 1
    if mutation == "notice_removed":
        assert not receipt.parent_posts and not checks.no_silent_drop
        assert checks.verdict == "FAIL"
    elif mutation == "copy_corrected":
        assert checks.verdict == "PASS"
    else:
        assert receipt.native_send_count == 1 and not checks.zero_native_sends
        assert checks.verdict == "FAIL"
    assert ForbiddenReceipt.model_validate_json(receipt.model_dump_json()) == receipt


async def test_removed_ingress_cannot_certify_itself(
    ingress: OwnedIngress, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook, _ = await configured_hook(ingress)
    monkeypatch.setattr(ingress.bot, "on_message", AsyncMock())
    with pytest.raises(ValueError, match="did not reach genuine Forbidden"):
        await hook.mention(ingress.message)
    assert not ingress.gateway.events and not ingress.gateway.creations


async def test_unbound_native_trace_is_refused(ingress: OwnedIngress) -> None:
    with pytest.raises(ValueError, match="actual isolated SDK transport"):
        DiscordForbiddenHook(bot=ingress.bot, gateway=ingress.gateway, native=ScriptedTransport())
    assert not ingress.gateway.armed and not ingress.native.requests


@pytest.mark.parametrize("bad_binding", ["account", "tenant", "workspace", "channel", "platform"])
async def test_foreign_overwrite_is_refused(ingress: OwnedIngress, bad_binding: str) -> None:
    hook = DiscordForbiddenHook(bot=ingress.bot, gateway=ingress.gateway, native=ingress.native)
    scope = Scope(
        tenant_id=str(uuid.uuid4()) if bad_binding == "tenant" else str(ingress.tenant_id),
        account_id=str(uuid.uuid4()) if bad_binding == "account" else str(ingress.account_id),
        principal_id="qa-admin:100",
        authorization_id="qa-offline:100",
    )
    if bad_binding == "platform":
        scope = Scope.platform(reason="mutation control")
    with pytest.raises(ValueError):
        await hook.discord_overwrite_edge(
            scope=scope,
            owned_workspace_id="999" if bad_binding == "workspace" else "100",
            owned_channel_id="999" if bad_binding == "channel" else "101",
            source={
                "do": "admin",
                "tool": "discord_overwrite",
                "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
            },
            capture_started_s=time.monotonic(),
        )
    assert not ingress.gateway.armed and not ingress.gateway.creations
    assert not ingress.native.requests


@pytest.mark.parametrize(
    "bad_args",
    [
        {"target": "other_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
        {"target": "daimon_bot", "deny": ["SEND_MESSAGES"]},
        {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"], "allow": []},
        {"target": "daimon_bot", "deny": []},
    ],
)
async def test_non_original_admin_operation_is_refused(
    ingress: OwnedIngress, bad_args: dict[str, object]
) -> None:
    hook = DiscordForbiddenHook(bot=ingress.bot, gateway=ingress.gateway, native=ingress.native)
    with pytest.raises(ValidationError):
        await hook.discord_overwrite_edge(
            scope=Scope(
                tenant_id=str(ingress.tenant_id),
                account_id=str(ingress.account_id),
                principal_id="qa-admin:100",
                authorization_id="qa-offline:100",
            ),
            owned_workspace_id="100",
            owned_channel_id="101",
            source={"do": "admin", "tool": "discord_overwrite", "args": bad_args},
            capture_started_s=time.monotonic(),
        )
    assert not ingress.gateway.armed and not ingress.native.requests


async def test_concurrent_overwrites_and_reuse_are_refused(ingress: OwnedIngress) -> None:
    hook = await ready_hook(ingress)

    async def bind_again() -> OverwriteReceipt:
        return await hook.discord_overwrite_edge(
            scope=Scope(
                tenant_id=str(ingress.tenant_id),
                account_id=str(ingress.account_id),
                principal_id="qa-admin:100",
                authorization_id="qa-offline:100",
            ),
            owned_workspace_id="100",
            owned_channel_id="101",
            source={
                "do": "admin",
                "tool": "discord_overwrite",
                "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
            },
            capture_started_s=time.monotonic(),
        )

    results = await asyncio.gather(bind_again(), bind_again(), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, OverwriteReceipt) for result in results) == 1
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
        await hook.mention(ingress.message)
    with pytest.raises(ValueError, match="one owned configured capture"):
        await hook.mention(ingress.message)
    assert len(ingress.gateway.creations) == 1


@pytest.mark.parametrize("routed", [False, True])
async def test_every_database_bind_is_refused_before_connection(
    ingress: OwnedIngress, monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test")
    connections: list[bool] = []

    def refuse_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise AssertionError("foreign connection must not be opened")

    event.listen(foreign.sync_engine, "do_connect", refuse_connection)
    own = cast(AsyncEngine, ingress.bot.runtime.sessionmaker.kw["bind"])
    async with ingress.bot.runtime.sessionmaker() as session:
        account = await get_account(session, ingress.account_id)
    assert account is not None
    factory = async_sessionmaker(
        own if routed else foreign,
        binds={type(account): foreign} if routed else None,
        expire_on_commit=False,
    )
    ingress.bot.runtime = dataclasses.replace(ingress.bot.runtime, sessionmaker=factory)
    try:
        with pytest.raises(ValueError, match="owned"):
            DiscordForbiddenHook(bot=ingress.bot, gateway=ingress.gateway, native=ingress.native)
        assert not connections
        assert not ingress.gateway.armed and not ingress.gateway.events
    finally:
        await foreign.dispose()


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_default_host_naming_post_is_present_and_excluded(
    ingress: OwnedIngress, backend: Provider
) -> None:
    assert ingress.bot.runtime.settings.thread_naming == ThreadNamingSettings()
    hook, _ = await configured_hook(ingress, backend)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
        receipt = await hook.mention(ingress.message)
    writes = [r for r in ingress.native.requests if r.method not in ("GET", "HEAD")]
    assert [(r.method, r.path) for r in writes] == [HOST_THREAD_NAMING_REQUEST]
    assert json.loads(writes[0].body)["model"] == "claude-haiku-4-5"
    assert ingress.gateway.typing_channels and set(ingress.gateway.typing_channels) == {101}
    assert json.loads(receipt.creation.request_json)["name"] == "QA permission check"
    assert receipt.native_send_count == 0 and evaluate_new14(receipt).zero_native_sends
    assert evaluate_new14(receipt).verdict == "FAIL"


async def test_fixture_skips_only_plain_local_contributor_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    engine = create_async_engine("postgresql+asyncpg://unused:unused@localhost/daimon_test")
    try:
        with pytest.raises(pytest.skip.Exception):
            require_owned_or_skip_contributor(async_sessionmaker(engine))
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("CI", "true")
        with pytest.raises(ValueError, match="owned"):
            require_owned_or_skip_contributor(async_sessionmaker(engine))
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://unused:unused@localhost/foreign_db",
        "postgresql+asyncpg://unused:unused@foreign-host/daimon_test",
        "postgresql+asyncpg://unused:unused@localhost/daimon_test?application_name=foreign",
    ],
)
async def test_fixture_does_not_skip_foreign_database_boundaries(url: str) -> None:
    engine = create_async_engine(url)
    try:
        with pytest.raises(ValueError, match="owned"):
            require_owned_or_skip_contributor(async_sessionmaker(engine))
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "dependency",
    [
        "sessionmaker",
        "anthropic",
        "messages",
        "turn_runtime_factory",
        "default_factory",
        "turn_runtimes",
        "backend",
        "backend_session_ref",
        "state_store",
    ],
)
@pytest.mark.parametrize("boundary", ["construct", "overwrite", "mention"])
async def test_every_reachable_runtime_dependency_refused_before_observation(
    ingress: OwnedIngress, monkeypatch: pytest.MonkeyPatch, dependency: str, boundary: str
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
        raise RuntimeError("foreign connection reached")

    def trap_runtime(*args: object, **kwargs: object) -> Never:
        calls.append(True)
        raise RuntimeError("uncaptured runtime reached")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    monkeypatch.setitem(
        runtime_module._RUNTIME_FACTORIES,  # pyright: ignore[reportPrivateUsage]
        "openai.persistent_workspace",
        trap_runtime,
    )
    values: dict[str, object] = {
        "sessionmaker": async_sessionmaker(foreign),
        "anthropic": outside_sdk,
        "messages": outside_sdk,
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
    original = ingress.bot.runtime
    bad = dataclasses.replace(
        original, turn_deps=dataclasses.replace(original.turn_deps, **{field: values[dependency]})
    )
    try:
        if boundary == "construct":
            ingress.bot.runtime = bad
            with pytest.raises(ValueError):
                DiscordForbiddenHook(
                    bot=ingress.bot, gateway=ingress.gateway, native=ingress.native
                )
        elif boundary == "overwrite":
            hook = await ready_hook(ingress, "openai")
            ingress.bot.runtime = bad
            with pytest.raises(ValueError):
                await hook.discord_overwrite_edge(
                    scope=Scope(
                        tenant_id=str(ingress.tenant_id),
                        account_id=str(ingress.account_id),
                        principal_id="qa-admin:100",
                        authorization_id="qa-offline:100",
                    ),
                    owned_workspace_id="100",
                    owned_channel_id="101",
                    source={
                        "do": "admin",
                        "tool": "discord_overwrite",
                        "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
                    },
                    capture_started_s=time.monotonic(),
                )
            assert hook.overwrite is None and not ingress.gateway.armed
        else:
            hook, _ = await configured_hook(ingress, "openai")
            ingress.bot.runtime = bad
            with pytest.raises(ValueError):
                await hook.mention(ingress.message)
        assert (
            not calls and not connections and not outside.requests and not ingress.native.requests
        )
        assert not ingress.gateway.creations and not ingress.gateway.events
    finally:
        ingress.bot.runtime = original
        await outside_sdk.close()
        await foreign.dispose()


@pytest.mark.parametrize("local_contributor", [False, True])
async def test_actual_forbidden_fixture_preflight_precedes_any_connection(
    tmp_path: Path, local_contributor: bool
) -> None:
    from packages.adapters.discord.tests.fixture_preflight_probe import run_fixture_preflight_probe

    code, output, connected = await run_fixture_preflight_probe(
        tmp_path=tmp_path,
        target=__file__ + "::test_real_forbidden_path[anthropic]",
        local_contributor=local_contributor,
    )
    assert not connected, output
    if local_contributor:
        assert code == 0 and "1 skipped" in output
    else:
        assert code != 0 and "owned local lane" in output
    assert "connection trap reached" not in output


@pytest.mark.parametrize("boundary", ["overwrite_read", "message_callback"])
async def test_replaced_runtime_after_await_is_refused_before_receipt(
    ingress: OwnedIngress, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from daimon.core.stores.mux_state import PostgresStateStore

    from packages.adapters.discord.tests import forbidden_hook as hook_module

    foreign = create_async_engine("postgresql+asyncpg://unused:unused@127.0.0.1:1/foreign_test")
    connections: list[bool] = []

    def trap_connection(*args: object, **kwargs: object) -> None:
        connections.append(True)
        raise RuntimeError("foreign connection reached")

    event.listen(foreign.sync_engine, "do_connect", trap_connection)
    original = ingress.bot.runtime
    bad = dataclasses.replace(
        original,
        turn_deps=dataclasses.replace(
            original.turn_deps, state_store=PostgresStateStore(async_sessionmaker(foreign))
        ),
    )
    try:
        if boundary == "overwrite_read":
            hook = await ready_hook(ingress)
            original_read = hook_module.current_backend

            async def replace_after_read(*args: Any, **kwargs: Any) -> ConfigRevision | None:
                result = await original_read(*args, **kwargs)
                ingress.bot.runtime = bad
                return result

            monkeypatch.setattr(hook_module, "current_backend", replace_after_read)
            with pytest.raises(ValueError, match="absent injected"):
                await hook.discord_overwrite_edge(
                    scope=Scope(
                        tenant_id=str(ingress.tenant_id),
                        account_id=str(ingress.account_id),
                        principal_id="qa-admin:100",
                        authorization_id="qa-offline:100",
                    ),
                    owned_workspace_id="100",
                    owned_channel_id="101",
                    source={
                        "do": "admin",
                        "tool": "discord_overwrite",
                        "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
                    },
                    capture_started_s=time.monotonic(),
                )
            assert hook.overwrite is None and not ingress.gateway.armed
            assert not ingress.native.requests and not ingress.gateway.creations
        else:
            hook, _ = await configured_hook(ingress)
            original_callback = ingress.bot.on_message

            async def replace_after_message(message: discord.Message) -> None:
                await original_callback(message)
                ingress.bot.runtime = bad

            monkeypatch.setattr(ingress.bot, "on_message", replace_after_message)
            with sentry_sdk.isolation_scope() as scope:
                scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
                with pytest.raises(ValueError, match="absent injected"):
                    await hook.mention(ingress.message)
            assert len(ingress.gateway.creations) == 1
            assert ingress.native.requests and ingress.gateway.events
        assert not connections
    finally:
        ingress.bot.runtime = original
        await foreign.dispose()
