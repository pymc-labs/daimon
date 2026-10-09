"""Instrumentation for one existing offline scenario, isolated in a child pytest.

No production logic is replaced. Existing fakes drive existing test functions;
this plugin records their boundary calls and final database rows. Random ids and
application time are fixed before fixture creation. SQL defaults use the same fixed clock.
Frozen observed times retain epoch offsets; deadlines retain anchor offsets.
Prices, callers, errors, order and continuity remain.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as datetime_module
import functools
import hashlib
import importlib
import inspect
import itertools
import json
import os
import secrets
import sys
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import anthropic
import discord
import httpx
import pytest
from aioresponses import aioresponses
from daimon.core import platform_names
from daimon.core._models import Base
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.turn import approvals, driver, outcomes
from daimon.testing import effect_recorder, turn_fakes
from daimon.testing.effect_recorder import EffectRecorder, json_value
from daimon.testing.ma import not_found_response
from daimon.testing.ma_transport import Json, ScriptedReply, ScriptedTransport
from http_turn import HttpTurnFixtures
from mutations import apply as apply_mutation
from sqlalchemy import ColumnDefault, DateTime
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.functions import current_timestamp
from sqlalchemy.sql.functions import now as sql_now
from sqlalchemy.sql.sqltypes import Uuid

RECORDER = EffectRecorder()
PATCH = pytest.MonkeyPatch()
NOW = datetime(2026, 10, 9, tzinfo=UTC)
INSTRUMENTATION_ERRORS: list[str] = []
RECOVERY_SCRIPT = ScriptedTransport()
TEST_DSN = os.environ["DAIMON_DATABASE__TEST_URL"]
OUTPUT = Path(os.environ["DAIMON_ORACLE_OUTPUT"])
COLLECTION_IDS = itertools.count(0x100000)
HTTP_TURNS: list[HttpTurnFixtures] = []
PATCH.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(COLLECTION_IDS)))


TOKEN_COUNTERS: dict[str, int] = {}


def token_bytes(nbytes: int | None = None) -> bytes:
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    scope = "token"
    if caller is not None:
        scope = f"{Path(caller.f_code.co_filename).name}:{caller.f_code.co_name}:{caller.f_lineno}"
    ordinal = TOKEN_COUNTERS.get(scope, 0)
    TOKEN_COUNTERS[scope] = ordinal + 1
    size = 32 if nbytes is None else nbytes
    blocks = [
        hashlib.sha256(f"oracle:{scope}:{ordinal}:{index}".encode()).digest()
        for index in range((size + 31) // 32)
    ]
    return b"".join(blocks)[:size]


PATCH.setattr(secrets, "token_bytes", token_bytes)
PATCH.setattr(time, "time", lambda: NOW.timestamp())


class Clock(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


class DateModule:
    datetime = Clock

    def __getattr__(self, name: str) -> Any:
        return getattr(datetime_module, name)


class GoldenNormalizer(effect_recorder.Normalizer):
    """Unlike real-clock PR2 recordings, golden fixtures have a frozen epoch."""

    def normalize(
        self,
        value: Json,
        *,
        field: str = "",
        identity: bool = False,
        anchor: datetime | None = None,
        table: str = "",
    ) -> Json:
        if (
            isinstance(value, str)
            and field in effect_recorder.TIME_FIELDS - effect_recorder.SCHEDULED_TIME_FIELDS
            and not identity
        ):
            try:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                delta = timestamp - NOW
                seconds = (
                    Decimal(delta.days * 86400 + delta.seconds)
                    + Decimal(delta.microseconds) / 1000000
                ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                return f"<time:epoch{seconds:+f}s>"
        return super().normalize(value, field=field, identity=identity, anchor=anchor, table=table)


class RenderClock:
    """Freeze periodic render time; finalizer renders still execute normally.

    Existing fixtures finish their events without advancing this clock. They
    assert the driver's forced terminal render. Real sleeps outside that timer
    (cancel, retry, approval and deadlines) keep their existing fixture behavior.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, seconds: float) -> None:
        frame = inspect.currentframe()
        if (
            frame is not None
            and frame.f_back is not None
            and frame.f_back.f_code.co_name == "_render_loop"
        ):
            await asyncio.Future()
        else:
            await asyncio.sleep(seconds)


def wire(value: Any) -> Any:
    try:
        return _wire(value)
    except Exception as error:
        INSTRUMENTATION_ERRORS.append(f"{type(error).__name__}: {error}")
        raise


def _wire(value: Any) -> Any:
    if isinstance(value, SimpleNamespace):
        return wire(vars(value))
    if isinstance(value, discord.File):
        position = value.fp.tell()
        try:
            return {"filename": value.filename, "content": json_value(value.fp.read())}
        finally:
            value.fp.seek(position)
    if isinstance(value, discord.ui.View | discord.ui.LayoutView):
        return wire(value.to_components())
    if isinstance(value, anthropic.NotGiven):
        return "<not-given>"
    if isinstance(value, httpx.Timeout):
        return value.as_dict()
    if isinstance(value, dict):
        return {key: wire(item) for key, item in cast(dict[str, Any], value).items()}
    if isinstance(value, tuple | list):
        return [wire(item) for item in cast(list[Any] | tuple[Any, ...], value)]
    return json_value(value)


def body(request: httpx.Request) -> Any:
    raw = request.content
    if not raw:
        return None
    if request.headers.get("content-type", "").startswith("multipart/"):
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + raw
        )
        return [
            {
                "name": part.get_param("name", header="content-disposition"),
                "filename": part.get_filename(),
                "headers": dict(part.items()),
                "content": base64.b64encode(cast(bytes, part.get_payload(decode=True))).decode(
                    "ascii"
                ),
            }
            for part in message.iter_parts()
        ]
    try:
        return json.loads(raw)
    except ValueError:
        return {"base64": base64.b64encode(raw).decode("ascii")}


def protocol_header(name: str, value: str) -> str:
    if name == "content-type" and value.startswith("multipart/"):
        message = BytesParser(policy=default).parsebytes(f"Content-Type: {value}\r\n\r\n".encode())
        boundary = message.get_boundary()
        if boundary is not None:
            return value.replace(boundary, "<multipart-boundary>")
    return value


def wrap_async(cls: Any, name: str, *, result: bool = False) -> None:
    original = getattr(cls, name)

    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        payload = wire({"args": args, "kwargs": kwargs})
        try:
            actual = await original(self, *args, **kwargs)
        except Exception as error:
            RECORDER.record(cls.__name__, name, payload, result=error)
            raise
        RECORDER.record(cls.__name__, name, payload, result=wire(actual) if result else None)
        return actual

    PATCH.setattr(cls, name, wrapped)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: Any) -> None:
    PATCH.setattr(effect_recorder, "Normalizer", GoldenNormalizer)

    def fixed_sql_now(self: Any, compiler: Any, **kwargs: Any) -> str:
        return "'2026-10-09T00:00:00+00:00'::timestamptz"

    PATCH.setattr(sql_now, "_compiler_dispatch", fixed_sql_now)
    PATCH.setattr(current_timestamp, "_compiler_dispatch", fixed_sql_now)
    mutation = os.environ.get("DAIMON_ORACLE_MUTATION")
    if mutation:
        apply_mutation(PATCH, mutation)
    importlib.import_module("parity.drivers.discord_driver")
    importlib.import_module("parity.drivers.slack_driver")
    ids = itertools.count(1)
    original_uuid = uuid.uuid4

    def next_uuid() -> uuid.UUID:
        return uuid.UUID(int=next(ids))

    for module in tuple(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if not (name.startswith(("daimon.", "jwt.")) or name == item.module.__name__):
            continue
        if (
            getattr(module, "datetime", None) is datetime
            and name != "daimon.testing.effect_recorder"
        ):
            PATCH.setattr(module, "datetime", Clock)
        for alias, value in tuple(vars(module).items()):
            if value is datetime_module:
                PATCH.setattr(module, alias, DateModule())
        if getattr(module, "uuid4", None) is original_uuid:
            PATCH.setattr(module, "uuid4", next_uuid)
    PATCH.setattr(uuid, "uuid4", next_uuid)

    def fixed_uuid(context: Any) -> uuid.UUID:
        return next_uuid()

    def fixed_now(context: Any) -> datetime:
        return NOW

    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_default = cast(Any, column.default)
            server_default = cast(Any, column.server_default)
            if (
                isinstance(column.type, Uuid)
                and column_default is not None
                and callable(column_default.arg)
            ):
                PATCH.setattr(column_default, "arg", fixed_uuid)
            elif (
                isinstance(column.type, Uuid)
                and server_default is not None
                and "gen_random_uuid" in str(server_default.arg)
            ):
                PATCH.setattr(column, "default", ColumnDefault(fixed_uuid))
            if isinstance(column.type, DateTime):
                if column_default is not None and callable(column_default.arg):
                    PATCH.setattr(column_default, "arg", fixed_now)
                elif server_default is not None and "now()" in str(server_default.arg):
                    PATCH.setattr(column, "default", ColumnDefault(fixed_now))
    timestamp_fields = {
        column.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    }
    PATCH.setattr(effect_recorder, "TIME_FIELDS", effect_recorder.TIME_FIELDS | timestamp_fields)
    PATCH.setattr(
        effect_recorder,
        "ID_FIELDS",
        effect_recorder.ID_FIELDS
        | {
            "custom_id",
            "env_file_id",
            "env_resource_id",
            "repo_resource_id",
            "memory_store_id",
            "vault_id",
            "environment_id",
            "agent_id",
            "seen_event_ids",
            "event_ids",
        },
    )
    original_init = outcomes.TurnObservation.__init__

    @functools.wraps(original_init)
    def observation_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.id = next_uuid()
        self._started = 0.0

    PATCH.setattr(outcomes.TurnObservation, "__init__", observation_init)
    PATCH.setattr(outcomes, "time", SimpleNamespace(monotonic=lambda: 0.0))
    PATCH.setattr(driver, "asyncio", RenderClock())

    class PlatformBoundary:
        def __init__(self, target: Any, platform: str) -> None:
            self.target = target
            self.platform = platform

        def __getattr__(self, name: str) -> Any:
            target = getattr(self.target, name)
            if not inspect.iscoroutinefunction(target):
                return target

            async def invoke(*args: Any, **kwargs: Any) -> Any:
                payload = wire({"args": args, "kwargs": kwargs})
                try:
                    actual = await target(*args, **kwargs)
                except Exception as error:
                    RECORDER.record(self.platform, name, payload, result=error)
                    raise
                receipt = None if isinstance(actual, Mock) else wire(actual)
                RECORDER.record(self.platform, name, payload, result=receipt)
                return (
                    PlatformBoundary(actual, self.platform) if isinstance(actual, Mock) else actual
                )

            return invoke

    discord_cards = importlib.import_module("daimon.adapters.discord.tool_confirmation")
    slack_cards = importlib.import_module("daimon.adapters.slack.tool_confirmation")
    original_discord_hook = discord_cards.discord_confirmation_hook
    original_slack_hook = slack_cards.SlackConfirmationCards.hook

    def discord_hook(channel: Any, **kwargs: Any) -> Any:
        return original_discord_hook(PlatformBoundary(channel, "discord_card"), **kwargs)

    def slack_hook(self: Any, client: Any, **kwargs: Any) -> Any:
        return original_slack_hook(self, PlatformBoundary(client, "slack_card"), **kwargs)

    original_settle = discord_cards._ConfirmationView._settle

    async def record_settle(self: Any, interaction: Any, answer: Any) -> None:
        response = interaction.response
        interaction.response = PlatformBoundary(response, "discord_card")
        try:
            await original_settle(self, interaction, answer)
        finally:
            interaction.response = response

    PATCH.setattr(discord_cards._ConfirmationView, "_settle", record_settle)
    PATCH.setattr(discord_cards, "discord_confirmation_hook", discord_hook)
    PATCH.setattr(slack_cards.SlackConfirmationCards, "hook", slack_hook)

    for platform in ("discord", "slack"):
        module = importlib.import_module(f"daimon.adapters.{platform}.lifecycle")
        lifecycle_cls = getattr(module, f"{platform.title()}TurnLifecycle")
        defaults = lifecycle_cls.__init__.__kwdefaults__
        PATCH.setitem(defaults, "clock", lambda: 1_000_000.0)

    original_prompt = prompt_for_tool_call

    def record_prompt(*args: Any, **kwargs: Any) -> Any:
        prompt = original_prompt(*args, **kwargs)
        RECORDER.record("confirmation", "prompt", wire(prompt))
        return prompt

    PATCH.setattr(approvals, "prompt_for_tool_call", record_prompt)
    output_module = importlib.import_module("daimon.adapters.cli.output")
    original_json = output_module.render_json

    def record_json(console: Any, rows: Any) -> None:
        original_json(console, rows)
        RECORDER.record("cli", "json_output", wire(rows))

    PATCH.setattr(output_module, "render_json", record_json)

    # Complete the existing Discord fake receipt's async reaction contract.
    # Its incoming message already has add_reaction, but the sent receipt does not.
    for module in tuple(sys.modules.values()):
        if not getattr(module, "__name__", "").endswith("drivers.discord_driver"):
            continue
        cls = cast(Any, module).DiscordDriver
        original_message = cls._make_message

        def make_message(
            self: Any, *args: Any, _factory: Any = original_message, **kwargs: Any
        ) -> Any:
            message = _factory(self, *args, **kwargs)
            message.channel.name = "parity-thread"
            message.channel.parent.name = "parity-channel"
            message.guild.name = "parity-workspace"
            receipt = message.channel.send.return_value
            if not inspect.iscoroutinefunction(getattr(receipt, "add_reaction", None)):
                receipt.add_reaction = AsyncMock(name="add_reaction", return_value=None)
            message.channel.get_partial_message.return_value = receipt
            return message

        PATCH.setattr(cls, "_make_message", make_message)

    HTTP_TURNS.append(HttpTurnFixtures(PATCH))
    for name in (
        "on_render",
        "on_terminal_success",
        "on_terminal_failure",
        "on_sse_event",
        "on_reconnect",
        "on_rate_limited",
        "on_interrupt_sent",
    ):
        wrap_async(turn_fakes.RecordingLifecycle, name)

    recovery_path = "/v1/sessions/sess_dead_before_recovery/events"
    if "test_dead_session_recreates" in item.nodeid:
        RECOVERY_SCRIPT.queue(
            ScriptedReply("GET", recovery_path, not_found_response("session gone"))
        )
    original_http = httpx.MockTransport.handle_async_request

    async def record_http(self: httpx.MockTransport, request: httpx.Request) -> httpx.Response:
        await request.aread()
        payload = {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.params.multi_items(),
            "body": body(request),
            "protocol_headers": [
                [name, protocol_header(name, request.headers[name])]
                for name in ("anthropic-beta", "anthropic-version", "content-type")
                if name in request.headers
            ],
        }
        try:
            if RECOVERY_SCRIPT.replies and (request.method, request.url.path) == (
                "GET",
                recovery_path,
            ):
                response = RECOVERY_SCRIPT.dispatch(request)
            else:
                response = await original_http(self, request)
        except Exception as error:
            if isinstance(error, AssertionError):
                INSTRUMENTATION_ERRORS.append(f"MA script violation: {error}")
            RECORDER.record("ma_http", "request", payload, result=error)
            raise
        content = None
        if response.is_stream_consumed:
            try:
                content = response.json()
            except ValueError:
                content = response.text
        RECORDER.record(
            "ma_http", "request", payload, result={"status": response.status_code, "body": content}
        )
        return response

    PATCH.setattr(httpx.MockTransport, "handle_async_request", record_http)
    original_platform = cast(Any, aioresponses)._request_mock

    async def record_platform(
        self: Any, orig_self: Any, method: str, url: Any, *args: Any, **kwargs: Any
    ) -> Any:
        payload = {
            "method": method,
            "url": str(url),
            "json": wire(kwargs.get("json")),
            "data": wire(kwargs.get("data")),
            "params": wire(kwargs.get("params")),
            "headers": {
                name.lower(): value
                for name, value in cast(dict[str, str], kwargs.get("headers") or {}).items()
                if name.lower() in {"content-type", "accept"}
            },
        }
        try:
            response = await original_platform(self, orig_self, method, url, *args, **kwargs)
        except Exception as error:
            RECORDER.record("platform_http", "request", payload, result=error)
            raise
        RECORDER.record("platform_http", "request", payload, result={"status": response.status})
        return response

    PATCH.setattr(aioresponses, "_request_mock", record_platform)
    original_mock = AsyncMock._execute_mock_call

    async def record_mock(self: Any, *args: Any, **kwargs: Any) -> Any:
        name = self._mock_name or self._mock_new_name
        if name not in {
            "run_turn",
            "send",
            "edit",
            "add_reaction",
            "remove_reaction",
            "delete",
            "send_message",
            "edit_message",
            "edit_original_response",
            "delete_original_response",
        }:
            return await original_mock(self, *args, **kwargs)
        if name == "run_turn":
            opaque = {
                "anthropic",
                "session_factory",
                "fernet",
                "github_app_private_key",
                "github_fallback_pat",
                "mcp_settings",
                "agent_github_app",
                "on_state",
            }
            recorded: dict[str, Any] = {}
            for key, value in kwargs.items():
                if key in opaque:
                    recorded[key] = (
                        None if value is None else {"fixture_type": type(value).__name__}
                    )
                elif key == "usage_record_factory":
                    bound = value("sess_oracle", "claude-sonnet-4-6")
                    recorded[key] = {
                        "function": bound.func.__qualname__,
                        "bindings": {
                            field: "<sessionmaker>" if field == "sessionmaker" else wire(item)
                            for field, item in bound.keywords.items()
                        },
                    }
                else:
                    recorded[key] = wire(value)
            payload = {"args": wire(args), "kwargs": recorded}
        else:
            payload = wire({"args": args, "kwargs": kwargs})
            parent = self._mock_parent or self._mock_new_parent
            identifier = getattr(parent, "id", None)
            payload["receiver"] = (
                {"id": str(identifier)} if isinstance(identifier, int | str) else None
            )
        try:
            actual = await original_mock(self, *args, **kwargs)
        except Exception as error:
            RECORDER.record("platform_client", name, payload, result=error)
            raise
        receipt = None
        if isinstance(actual, Mock):
            message_id = getattr(actual, "id", None)
            if isinstance(message_id, int | str):
                receipt = {"message_id": str(message_id)}
        elif actual is not None:
            receipt = wire(actual)
        RECORDER.record("platform_client", name, payload, result=receipt)
        return actual

    PATCH.setattr(AsyncMock, "_execute_mock_call", record_mock)


async def drain_boundaries() -> None:
    await outcomes.drain_outcomes()
    await drain_budget_notices()
    await platform_names.settle()


async def database(schema: str) -> object:
    engine = create_async_engine(
        TEST_DSN,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema}, public"}},
    )
    try:
        async with AsyncSession(engine) as session:
            return await RECORDER.database(session)
    finally:
        await engine.dispose()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any) -> Generator[None]:
    yield
    RECOVERY_SCRIPT.assert_consumed()
    for fixtures in HTTP_TURNS:
        fixtures.assert_consumed()
    assert not INSTRUMENTATION_ERRORS, f"Oracle instrumentation failed: {INSTRUMENTATION_ERRORS}"
    runner = item.funcargs.get("_session_scoped_runner")
    if runner is not None:
        runner.run(drain_boundaries())
    schema = item.funcargs.get("db_schema")
    session = item.funcargs.get("db_session")
    if runner is not None and isinstance(session, AsyncSession):
        snapshot = runner.run(RECORDER.database(session))
    else:
        snapshot = asyncio.run(database(schema)) if schema else None
    OUTPUT.write_text(RECORDER.transcript(database=snapshot, epoch=NOW))


def pytest_sessionfinish() -> None:
    PATCH.undo()
