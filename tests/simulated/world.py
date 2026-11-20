"""One fresh install, as production builds it, on the simulated provider network."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from daimon.core import backend_capture, output_delivery
from daimon.core.authz import build_subject
from daimon.core.channel_backend import (
    AgentBackendService,
    BackendActor,
    load_backend_agent,
    make_backend_service,
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
    TurnSettings,
)
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.provider_keys import DEFAULT_KEY_ENV, anthropic_key_present
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.stores import usage_events
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.domain import Platform
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.thread_model_flow import thread_pick_result
from daimon.core.thread_model_switch import pick_thread, pick_value, thread_pick_for
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.openai_late_usage import LATE_USAGE
from daimon.core.turn.outcomes import drain_outcomes
from daimon.testing.shared_helpers import OfflineSettings
from daimon.testing.sim.anthropic import AnthropicAPI
from daimon.testing.sim.gemini import GeminiAPI
from daimon.testing.sim.network import ProviderNetwork
from daimon.testing.sim.openai import OpenAIAPI
from daimon.testing.sim.script import Script
from mux.contracts.ids import Provider
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def turn_settings() -> TurnSettings:
    """The code default, or the CI leg's pins (`DAIMON_TURN__*_BACKENDS=false`)."""
    pins = {
        name: os.environ[f"DAIMON_TURN__{name.upper()}"].lower() == "true"
        for name in ("channel_backends", "agent_backends")
        if f"DAIMON_TURN__{name.upper()}" in os.environ
    }
    return TurnSettings.model_validate(pins)


@dataclass
class World:
    """Settings, the production clients and turn deps, and every provider fake.

    The fakes and the database outlive `restart`, as the providers and Postgres
    outlive a worker; the clients, caches and turn deps are built afresh.
    """

    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    script: Script
    network: ProviderNetwork
    api: AnthropicAPI
    openai: OpenAIAPI
    gemini: GeminiAPI
    anthropic: AsyncAnthropic = field(init=False)
    deps: TurnDeps = field(init=False)

    def start(self) -> None:
        # The client build_runtime makes; only its socket layer is simulated.
        self.anthropic = AsyncAnthropic(
            api_key=self.settings.anthropic.resolved_api_key(),
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=SkillsRateLimitedTransport(600)),
        )
        self.deps = build_turn_deps(
            self.settings,
            self.anthropic,
            self.sessions,
            deployment_default=parse_deployment_default(self.settings.defaults_root),
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )

    async def stop(self) -> None:
        await drain_outcomes()
        await self.deps.provider_clients.aclose()
        await self.anthropic.close()

    async def restart(self) -> None:
        """A worker restart: every in-process client, cache and journal is new."""
        await self.stop()
        self.start()


async def enable_backend(world: World, tenant_id: UUID, backend: str) -> None:
    """An owner turning another model provider on for the workspace."""
    async with policy_write_transaction(world.sessions, tenant_id=tenant_id) as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if backend not in policy.enabled_backends:
            enabled = {
                **policy.model_dump(),
                "enabled_backends": (*policy.enabled_backends, backend),
            }
            await set_access_policy(
                session, tenant_id=tenant_id, policy=type(policy).model_validate(enabled)
            )


async def _backends(
    world: World, tenant_id: UUID, platform: Platform, user_id: str
) -> tuple[AgentBackendService, BackendActor]:
    """The provider-switch service and admin actor every switch surface builds."""
    async with world.sessions() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
        await session.commit()
    subject = build_subject(is_admin=True, platform_user_id=user_id)
    actor = BackendActor(tenant_id=tenant_id, account_id=principal.account_id, subject=subject)
    capture = backend_capture.for_agent(
        world.deps, account_id=principal.account_id, subject=subject
    )
    return make_backend_service(world.sessions, world.settings.turn, capture_source=capture), actor


async def set_agent_backend(
    world: World, tenant_id: UUID, *, platform: Platform, user_id: str, backend: Provider
) -> None:
    """An admin moving the default agent onto `backend` in its settings card."""
    service, actor = await _backends(world, tenant_id, platform, user_id)
    name = world.deps.deployment_default.agent_name
    assert name, "the deployment default names an agent"
    agent = await load_backend_agent(
        world.anthropic,
        world.sessions,
        tenant_id=tenant_id,
        agent_name=name,
    )
    current = await service.current(agent)
    await service.set_backend(agent, backend, current.local if current else 0, actor)


async def switch_to(
    world: World, tenant_id: UUID, provider: Provider, *, platform: Platform, user_id: str
) -> None:
    """Turn `provider` on and move the default agent onto it; Anthropic needs neither."""
    if provider != "anthropic":
        await enable_backend(world, tenant_id, provider)
        await set_agent_backend(
            world, tenant_id, platform=platform, user_id=user_id, backend=provider
        )


async def metering(
    world: World, tenant_id: UUID, provider: Provider
) -> tuple[list[tuple[int | None, int | None]], list[tuple[int, int]]]:
    """The usage rows Daimon wrote, and the calls the providers billed."""
    async with world.sessions() as session:
        rows = await usage_events.list_for_tenant(session, tenant_id=tenant_id)
    fake = {"anthropic": world.api, "openai": world.openai, "gemini": world.gemini}[provider]
    # Daimon's own small Claude calls (thread titles) are billed whichever provider answers.
    billed = world.api.reported + ([] if fake is world.api else fake.reported)
    return sorted((r.input_tokens, r.output_tokens) for r in rows), sorted(billed)


async def pick_model(
    world: World,
    tenant_id: UUID,
    *,
    platform: Platform,
    channel_id: str,
    thread_id: str,
    user_id: str,
    value: str,
) -> str:
    """`/model <value>` in a conversation, as the command keeps it; returns the reply."""
    service, actor = await _backends(world, tenant_id, platform, user_id)
    pick = await thread_pick_for(
        service=service,
        anthropic=world.anthropic,
        sessionmaker=world.sessions,
        settings=world.settings,
        default=world.deps.deployment_default,
        actor=actor,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        platform_user_id=user_id,
    )
    assert pick is not None, "no agent answers in this conversation"
    done = await pick_thread(pick, pick_value(value), thread_id=thread_id, admin=True)
    return thread_pick_result(done)


@contextmanager
def _compressed_waits() -> Iterator[None]:
    """Output settling and late usage re-reads at 1/100 speed: the same waits, in milliseconds."""
    saved = output_delivery._POLL_DELAYS_S, output_delivery._MIN_SETTLE_S  # pyright: ignore[reportPrivateUsage]
    late = LATE_USAGE._delays  # pyright: ignore[reportPrivateUsage]
    output_delivery._POLL_DELAYS_S = tuple(d / 100 for d in saved[0])  # pyright: ignore[reportPrivateUsage]
    output_delivery._MIN_SETTLE_S = saved[1] / 100  # pyright: ignore[reportPrivateUsage]
    LATE_USAGE._delays = tuple(d / 100 for d in late)  # pyright: ignore[reportPrivateUsage]
    try:
        yield
    finally:
        output_delivery._POLL_DELAYS_S, output_delivery._MIN_SETTLE_S = saved  # pyright: ignore[reportPrivateUsage]
        LATE_USAGE._delays = late  # pyright: ignore[reportPrivateUsage]
        for task in LATE_USAGE._tasks.values():  # pyright: ignore[reportPrivateUsage]
            task.cancel()


@asynccontextmanager
async def world(
    sessions: async_sessionmaker[AsyncSession],
    *,
    keyed: tuple[Provider, ...] = ("anthropic", *DEFAULT_KEY_ENV),
    **overrides: Any,
) -> AsyncIterator[World]:
    """`overrides` replace whole top-level settings, e.g. a surface's own block.

    `keyed` names the providers whose deployment key the install holds."""
    base: dict[str, Any] = {
        "database": DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://sim:sim@localhost:5432/daimon_test_nc_sim")
        ),
        "anthropic": AnthropicSettings(api_key=SecretStr("sim") if "anthropic" in keyed else None),
        "discord": DiscordSettings(bot_token=SecretStr("sim"), thread_open_notice_after_s=0),
        "mcp": McpSettings(public_url=HttpUrl("https://daimon.example.com/mcp")),
        "turn": turn_settings(),
        "defaults_root": ROOT / "defaults",
    }
    settings = OfflineSettings(**(base | overrides))
    script = Script()
    api, openai, gemini = AnthropicAPI(script), OpenAIAPI(script=script), GeminiAPI(script=script)
    network = ProviderNetwork({fake.host: fake for fake in (api, openai, gemini)})
    envs = {"anthropic": "DAIMON_ANTHROPIC__API_KEY", **DEFAULT_KEY_ENV}
    saved = {env: os.environ.pop(env, None) for env in envs.values()}
    os.environ.update({env: "sim" for p, env in envs.items() if p in keyed})
    anthropic_key_present.cache_clear()  # read once per process; this world changes it
    sim = World(settings, sessions, script, network, api, openai, gemini)
    try:
        with network.installed(), _compressed_waits():
            sim.start()
            try:
                yield sim
            finally:
                await sim.stop()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        anthropic_key_present.cache_clear()
