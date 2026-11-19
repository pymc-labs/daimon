"""Fakes and helpers that several test files in this directory share."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsStreamSessionEvents,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.turn.io import TurnEvent, TurnStream
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_router import build_turn_router
from discord import app_commands
from discord.ext import commands
from mux.contracts.receipts import StopObservation
from mux.testing.drivers.anthropic.ma import json_body, make_fake_memory_store_handler
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.isolated_sdk import build_async_anthropic
from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 9, tzinfo=UTC)


class OfflineSettings(Settings):
    """Validate synthetic settings without reading environment or credential files."""

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


class Stream:
    def __init__(self, events: Sequence[TurnEvent]) -> None:
        self.events = iter(events)

    def __aiter__(self) -> Stream:
        return self

    async def __anext__(self) -> TurnEvent:
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        pass


class Wire:
    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.requests: list[httpx.Request] = []
        self.created: list[str] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.stream_hits: list[str] = []
        self.router = build_turn_router(
            str(tenant_id), fresh_event_ids=True, stream_hits=self.stream_hits
        )
        self.memory = make_fake_memory_store_handler()
        self.router.add("POST", r"/v1/sessions", self.create)
        self.router.add(
            "GET",
            r"/v1/sessions/(?P<session_id>[^/]+)",
            lambda _r, m: httpx.Response(200, json=self.sessions[m["session_id"]]),
        )

    def create(self, request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        body = json_body(request)
        native = f"sess_backend_{len(self.created) + 1}"
        self.created.append(native)
        response = ma_session(
            id=native,
            agent_id="ag_parity_test",
            environment_id="env_parity_test",
            metadata=body.get("metadata"),
            resources=body.get("resources", ()),
        ).model_dump(mode="json")
        self.sessions[native] = response
        return httpx.Response(200, json=response)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/v1/memory_stores"):
            return self.memory(request)
        return self.router.dispatch(request)


@dataclasses.dataclass
class FakeSocketClient:
    call_log: list[str] = dataclasses.field(default_factory=list[str])
    sent_responses: list[SocketModeResponse] = dataclasses.field(
        default_factory=list[SocketModeResponse]
    )

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("send_socket_mode_response")
        self.sent_responses.append(response)

    async def close(self) -> None:
        self.call_log.append("close")


class Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()

    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


def all_app_command_names(cog_cls: type[commands.Cog]) -> set[str]:
    """Return the set of slash-command names registered by ``cog_cls``."""
    names: set[str] = set()
    for cmd in cog_cls.__cog_app_commands__:  # type: ignore[attr-defined]
        if isinstance(cmd, app_commands.Group):
            names.add(cmd.name)
            for child in cmd.commands:
                names.add(f"{cmd.name} {child.name}")
        else:
            names.add(cmd.name)
    return names


def cred_wire(
    *,
    cred_id: str,
    vault_id: str,
    mcp_server_url: str,
) -> dict[str, Any]:
    """Minimal static_bearer credential wire shape."""
    return {
        "id": cred_id,
        "type": "vault_credential",
        "vault_id": vault_id,
        "metadata": {},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "auth": {
            "type": "static_bearer",
            "mcp_server_url": mcp_server_url,
        },
    }


async def drain(app: SlackApp) -> None:
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def make_app() -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr("dummykey"),)
    settings.slack.max_concurrent_turns_per_tenant = 3
    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=MagicMock(),
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=replace(deps_for(), turn_path=None),  # stub, turn path not exercised
    )
    return SlackApp(runtime=runtime)


def make_clock(times: list[float]) -> Callable[[], float]:
    """Return a clock callable that yields `times` in order, repeating the last
    value once exhausted (extra internal reads stay deterministic)."""
    remaining = list(times)

    def clock() -> float:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    return clock


async def role_and_invitations(
    sessions: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> tuple[Role, Sequence[Mapping[Any, Any]]]:
    async with sessions() as session:
        account = await get_account(session, account_id)
        rows = await session.execute(
            text(
                "SELECT agent_id, agent_ma_id, requester_platform_user_id "
                "FROM github_connect_invitations WHERE requester_account_id = :account"
            ),
            {"account": account_id},
        )
    assert account is not None
    return account.role, list(rows.mappings())


def settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def vault_obj(vault_id: str, display_name: str, created_at: str) -> dict[str, Any]:
    return {
        "id": vault_id,
        "type": "vault",
        "display_name": display_name,
        "metadata": None,
        "archived_at": None,
        "created_at": created_at,
    }


def walk(item: Any) -> list[Any]:  # noqa: ANN401
    """Every component in the tree, the way discord.py counts them."""
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(walk(child))
    accessory = getattr(item, "accessory", None)
    if accessory is not None:
        found.extend(walk(accessory))
    return found


class Clock:
    now_at = NOW

    @classmethod
    def now(cls, *args: object) -> datetime:
        return cls.now_at


class Codec:
    def __init__(self, events: Sequence[TurnEvent]) -> None:
        self.events = events

    async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
        return Stream(self.events)

    async def send(self, events: Sequence[BetaManagedAgentsEventParams]) -> None:
        pass

    async def replay(self, *, timeout_s: float = 30) -> list[BetaManagedAgentsSessionEvent]:
        return []

    async def status(self) -> str:
        return "idle"

    async def interrupt(self, *, timeout_s: float) -> StopObservation | None:
        return None

    async def archive(self) -> None:
        pass


def build_client(wire: Wire) -> AsyncAnthropic:
    """Keep the original test wire policy while refusing ambient SDK settings."""
    return build_async_anthropic(
        transport=httpx.MockTransport(wire.handle),
        api_key="test",
        base_url="https://api.anthropic.com",
        max_retries=2,
        timeout=httpx.Timeout(600.0, connect=5.0),
    )


def native_events() -> tuple[BetaManagedAgentsStreamSessionEvents, ...]:
    text = BetaManagedAgentsAgentMessageEvent(
        id="native-message",
        type="agent.message",
        content=[BetaManagedAgentsTextBlock(type="text", text="Actual answer")],
        processed_at=NOW,
    )
    done = BetaManagedAgentsSessionStatusIdleEvent(
        id="native-done",
        type="session.status_idle",
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        processed_at=NOW,
    )
    return text, done


class Session:
    """Stands in for a session where the code under test only passes it on."""

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None
