from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import secrets
import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.testing.effect_recorder import (
    DATABASE_EXTENSIONS,
    DB_TABLES,
    LEGACY_DB_COLUMNS,
    EffectRecorder,
    FakeClock,
    Normalizer,
    RecordingPlatformClient,
    database_metadata,
)
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.turn_router import turn_events
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


async def test_script_runs_real_sdk_and_records_stream_request_without_credentials() -> None:
    transport = ScriptedTransport()
    events = turn_events(now=FakeClock().now())
    transport.queue(ScriptedReply.stream("/v1/sessions/sess_test/events/stream", events))
    async with (
        transport.client() as client,
        await client.beta.sessions.events.stream("sess_test") as stream,
    ):
        actual = [event async for event in stream]
    assert [event.type for event in actual] == [event["type"] for event in events]
    assert len(transport.requests) == 1
    assert transport.requests[0].path == "/v1/sessions/sess_test/events/stream"
    assert not any(
        "key" in key or "authorization" in key for key, _ in transport.requests[0].protocol_headers
    )
    transport.assert_consumed()


def test_transport_reuses_router_but_rejects_out_of_order_and_unconsumed_scripts() -> None:
    router = MARouter()
    router.add_session(ma_session(id="sess_test"))
    transport = ScriptedTransport(router=router)
    transport.queue(
        ScriptedReply("POST", "/first", httpx.Response(200, json={})),
        ScriptedReply("GET", "/second", httpx.Response(200, json={})),
    )
    assert (
        transport.dispatch(
            httpx.Request("GET", "https://offline/v1/sessions/sess_test")
        ).status_code
        == 200
    )
    with pytest.raises(AssertionError, match="Out-of-order"):
        transport.dispatch(httpx.Request("GET", "https://offline/second"))
    with pytest.raises(AssertionError, match="MA script violations"):
        transport.assert_consumed()


def test_transport_checks_body_and_preserves_repeated_query_parameters() -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/events",
            httpx.Response(200, json={}),
            query=(("types[]", "one"), ("types[]", "two")),
            request_json={"events": []},
            check_json=True,
        )
    )
    request = httpx.Request(
        "POST", "https://offline/events?types[]=one&types[]=two", json={"events": []}
    )
    assert transport.dispatch(request).status_code == 200
    assert transport.requests[0].json() == {"events": []}
    transport.assert_consumed()


async def test_transport_rate_limit_surfaces_without_sdk_retry() -> None:
    import anthropic

    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_test",
            httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "scripted rate limit"},
                },
                headers={"retry-after": "0"},
            ),
        )
    )
    async with transport.client() as client:
        with pytest.raises(anthropic.RateLimitError):
            await client.beta.sessions.retrieve("sess_test")
    assert len(transport.requests) == 1
    transport.assert_consumed()


async def test_platform_recorder_keeps_order_payloads_files_and_error_kind() -> None:
    recorder = EffectRecorder()
    fake = AsyncMock()
    fake.send.return_value = {"message_id": "message_random"}
    fake.chat_update.return_value = {"ok": True}
    fake.reactions_add.return_value = {"ok": True}
    fake.files_upload_v2.return_value = {"file_id": "file_random"}
    fake.edit.side_effect = ValueError("edit failed")
    discord = cast(Any, RecordingPlatformClient(fake, recorder, platform="discord"))
    slack = cast(Any, RecordingPlatformClient(fake, recorder, platform="slack"))
    assert await discord.send("hello", caller="user_7") == {"message_id": "message_random"}
    await slack.chat_update(text="edited", message_id="message_random")
    await slack.reactions_add(name="eyes", message_id="message_random")
    await slack.files_upload_v2(file=b"exact bytes", filename="output.txt")
    with pytest.raises(ValueError):
        await discord.edit(content="failed")
    result = json.loads(recorder.transcript())
    assert [effect["operation"] for effect in result["effects"]] == [
        "send",
        "chat_update",
        "reactions_add",
        "files_upload_v2",
        "edit",
    ]
    assert result["effects"][0]["payload"]["kwargs"]["caller"] == "user_7"
    assert (
        result["effects"][1]["payload"]["kwargs"]["message_id"]
        == result["effects"][0]["result"]["message_id"]
    )
    assert result["effects"][-1]["result"]["error_kind"] == "ValueError"
    assert result["effects"][3]["payload"]["kwargs"]["file"] == {"base64": "ZXhhY3QgYnl0ZXM="}


def test_normalizer_preserves_identity_price_continuity_and_distinct_ids() -> None:
    recorder = EffectRecorder()
    recorder.record(
        "discord",
        "post",
        {
            "caller": {"id": "caller_fixed"},
            "tenant_id": "tenant_fixed",
            "model": {"id": "model_fixed"},
            "price": Decimal("0.0000001234"),
            "duration_ms": 17,
            "session_id": "old",
            "predecessor_id": "old",
            "replaced_by_id": "new",
            "transfer_kind": "history",
            "created_at": "2026-10-09T00:00:00+00:00",
        },
    )
    payload = json.loads(recorder.transcript())["effects"][0]["payload"]
    assert payload["caller"]["id"] == "caller_fixed"
    assert payload["tenant_id"] == "tenant_fixed"
    assert payload["model"]["id"] == "model_fixed"
    assert payload["price"] == "0.0000001234"
    assert payload["duration_ms"] == 17
    assert payload["transfer_kind"] == "history"
    assert payload["session_id"] == payload["predecessor_id"]
    assert payload["session_id"] != payload["replaced_by_id"]
    assert payload["created_at"] == "<time>"
    assert Normalizer().normalize({"error_kind": "APIConnectionError"}) == {
        "error_kind": "APIConnectionError"
    }


async def test_database_recorder_captures_all_five_tables(db_session: AsyncSession) -> None:
    from daimon.core.stores import tenant_ledger
    from daimon.testing.factories import make_tenant

    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("1.234567"),
        reason="trial",
        idempotency_key="oracle_trial",
    )
    await db_session.commit()
    snapshot = await EffectRecorder().database(db_session)
    assert set(snapshot) - {DATABASE_EXTENSIONS} == {
        "usage_events",
        "tenant_ledger",
        "turn_outcomes",
        "thread_sessions",
        "task_continuations",
    }
    added_columns = {
        name: added
        for name in DB_TABLES
        if (
            added := [
                column.name
                for column in database_metadata().tables[name].columns
                if column.name not in LEGACY_DB_COLUMNS[name]
            ]
        )
    }
    if not added_columns:
        assert DATABASE_EXTENSIONS not in snapshot
    else:
        extensions = snapshot[DATABASE_EXTENSIONS]
        assert isinstance(extensions, dict) and set(extensions) == set(added_columns)
        for name, columns in added_columns.items():
            section = extensions[name]
            assert isinstance(section, dict) and set(section) == {"columns", "rows"}
            assert section["columns"] == columns
            legacy_rows = snapshot[name]
            extra_rows = section["rows"]
            assert isinstance(legacy_rows, list) and isinstance(extra_rows, list)
            assert len(extra_rows) == len(legacy_rows)
            if not legacy_rows:
                assert extra_rows == [], "empty tables still declare their additive columns"
            for index, extra in enumerate(extra_rows):
                assert isinstance(extra, dict) and set(extra) == {"legacy_row_index", "values"}
                assert extra["legacy_row_index"] == index
                values = extra["values"]
                assert isinstance(values, dict) and set(values) == set(columns)
    ledger = snapshot["tenant_ledger"]
    assert isinstance(ledger, list) and len(ledger) == 1
    row = ledger[0]
    assert isinstance(row, dict) and Decimal(str(row["delta_usd"])) == Decimal("1.234567")


async def test_db_snapshot_repeats_after_regenerating_six_random_ledger_ids(
    db_engine: AsyncEngine,
) -> None:
    # Exercise independent schemas with this package's existing DDL helpers.
    from daimon.core.stores import tenant_ledger
    from daimon.testing.db import (
        _create_schema_with_tables,  # pyright: ignore[reportPrivateUsage]
        _drop_schema,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.testing.factories import make_tenant

    transcripts: list[str] = []
    generated: list[set[str]] = []
    for attempt in range(2):
        schema = f"test_f{os.getpid()}_{secrets.token_hex(4)}"
        await _create_schema_with_tables(db_engine, schema)
        engine = create_async_engine(
            db_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema}, public"}},
        )
        try:
            async with AsyncSession(engine) as session:
                tenant = await make_tenant(session, workspace_id="oracle_fixed_tenant")
                for index in range(6):
                    await tenant_ledger.insert_entry(
                        session,
                        tenant_id=tenant.id,
                        delta_usd=Decimal(index) + Decimal("0.000123"),
                        reason="trial",
                        idempotency_key=f"oracle_trial_{index}",
                        occurred_at=FakeClock().now() + timedelta(seconds=attempt),
                    )
                await session.commit()
                recorder = EffectRecorder()
                snapshot = await recorder.database(session)
                rows = cast(list[dict[str, Any]], snapshot["tenant_ledger"])
                generated.append({row["id"] for row in rows})
                transcripts.append(recorder.transcript(database=snapshot))
        finally:
            await engine.dispose()
            await _drop_schema(db_engine, schema)
    assert generated[0].isdisjoint(generated[1]), "both runs must mint independent runtime PKs"
    assert transcripts[0] == transcripts[1]


async def test_real_db_clock_is_stable_across_three_commits_in_all_five_tables(
    db_engine: AsyncEngine,
) -> None:
    from daimon.core._models import (
        TaskContinuation,
        TenantLedger,
        ThreadSession,
        TurnOutcome,
        UsageEvent,
    )
    from daimon.testing.db import (
        _create_schema_with_tables,  # pyright: ignore[reportPrivateUsage]
        _drop_schema,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.testing.factories import make_account, make_tenant
    from sqlalchemy import func, text

    transcripts: list[str] = []
    observed: list[list[str]] = []
    for delay in (0.005, 0.018):
        schema = f"test_f{os.getpid()}_{secrets.token_hex(4)}"
        await _create_schema_with_tables(db_engine, schema)
        engine = create_async_engine(
            db_engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": f"{schema}, public"}},
        )
        try:
            async with AsyncSession(engine) as session:
                tenant = await make_tenant(session, workspace_id="oracle_real_clock")
                account = await make_account(session, tenant=tenant, id=uuid.UUID(int=99))
                await session.commit()
                for index in range(3):
                    session.add_all(
                        [
                            UsageEvent(
                                tenant_id=tenant.id,
                                managed_session_id=f"session_{index}",
                                event_id=f"event_{index}",
                                platform_user_id="caller_fixed",
                                model="claude-sonnet-4-6",
                                input_tokens=index + 1,
                            ),
                            TenantLedger(
                                tenant_id=tenant.id,
                                delta_usd=Decimal("1.234567") + index,
                                reason="trial",
                                idempotency_key=f"money_identity_{index}",
                            ),
                            ThreadSession(
                                tenant_id=tenant.id,
                                account_id=account.id,
                                platform="discord",
                                thread_id=f"thread_{index}",
                                ma_session_id=f"session_{index}",
                                active_turn_started_at=func.now(),
                            ),
                            TaskContinuation(
                                tenant_id=tenant.id,
                                platform="discord",
                                parent_channel_id="channel_fixed",
                                thread_id=f"thread_{index}",
                                requester_account_id=account.id,
                                requester_external_user_id="caller_fixed",
                                target_ma_agent_id="agent_fixed",
                                target_name="fixed-agent",
                                requested_work=f"wake_{index}",
                                reason="timer",
                                status="delivered",
                                idempotency_key=uuid.UUID(int=index + 1),
                                claimed_at=func.now(),
                                delivered_at=func.now(),
                                started_at=func.now(),
                                available_at=func.now() + text("INTERVAL '300 seconds'"),
                                lease_expires_at=func.now() + text("INTERVAL '60 seconds'"),
                            ),
                            TurnOutcome(
                                id=uuid.uuid4(),
                                tenant_id=tenant.id,
                                account_id=account.id,
                                platform="discord",
                                thread_id=f"thread_{index}",
                                session_id=f"session_{index}",
                                origin="chat",
                                reason="completed",
                                started_at=func.now(),
                                ended_at=func.now(),
                                duration_ms=17,
                                recovered=False,
                                release="oracle",
                                usage_refs=[{"event_id": f"event_{index}"}],
                                cost_usd=Decimal("0.0000001234"),
                            ),
                        ]
                    )
                    await session.commit()
                    await asyncio.sleep(delay)
                recorder = EffectRecorder()
                snapshot = await recorder.database(session)
                for name in DB_TABLES:
                    rows = snapshot[name]
                    assert isinstance(rows, list) and len(rows) == 3
                ledger = cast(list[dict[str, Any]], snapshot["tenant_ledger"])
                observed.append([row["occurred_at"] for row in ledger])
                transcripts.append(recorder.transcript(database=snapshot))
        finally:
            await engine.dispose()
            await _drop_schema(db_engine, schema)
    assert len(set(observed[0])) == len(set(observed[1])) == 3
    assert observed[0] != observed[1], "test must exercise distinct real DB clocks"
    assert transcripts[0] == transcripts[1]
    assert "<time:anchor+300s>" in transcripts[0]
    assert "<time:anchor+60s>" in transcripts[0]
    assert "1.234567" in transcripts[0] and "0.0000001234" in transcripts[0]


def test_continuation_uuid_keys_normalize_without_erasing_ledger_money_keys() -> None:
    transcripts: list[str] = []
    for suffix in ("a", "b"):
        data = {
            "task_continuations": [
                {
                    "id": f"row_{suffix}",
                    "idempotency_key": f"wake_{suffix}",
                    "requested_work": "wake",
                }
            ],
            "tenant_ledger": [{"id": f"ledger_{suffix}", "idempotency_key": "exact_money_key"}],
        }
        transcripts.append(EffectRecorder().transcript(database=data))
    assert transcripts[0] == transcripts[1]
    assert "exact_money_key" in transcripts[0]


def test_all_captured_db_timestamp_columns_and_relative_timer_durations() -> None:
    from daimon.core._models import Base
    from daimon.testing.effect_recorder import DB_TABLES, TIME_FIELDS
    from sqlalchemy import DateTime

    snapshots: list[str] = []
    for shift in (0, 30):
        clock = FakeClock()
        clock.advance(shift)
        data: dict[str, Any] = {}
        for name in DB_TABLES:
            timestamps = [
                column.name
                for column in Base.metadata.tables[name].columns
                if isinstance(column.type, DateTime)
            ]
            assert set(timestamps) <= TIME_FIELDS
            data[name] = [
                {
                    "id": f"runtime_{name}_{shift}",
                    "account_id": "caller_fixed",
                    "cost_usd": Decimal("0.0000001234"),
                    **{field: clock.now() for field in timestamps},
                }
            ]
        snapshots.append(EffectRecorder().transcript(database=data))
    assert snapshots[0] == snapshots[1]
    now = FakeClock().now()
    first = Normalizer(epoch=now).normalize({"due_at": (now + timedelta(seconds=300)).isoformat()})
    changed = Normalizer(epoch=now).normalize(
        {"due_at": (now + timedelta(seconds=301)).isoformat()}
    )
    assert first == {"due_at": "<time:anchor+300s>"}
    assert first != changed, "a changed timer duration must remain visible"


@pytest.mark.parametrize(
    "container", ["author", "member", "requester", "owner", "mentions", "guild", "channel", "team"]
)
def test_normalizing_swapped_callers_stays_different(container: str) -> None:
    first = Normalizer().normalize({container: [{"id": "user_A"}, {"id": "user_B"}]})
    second = Normalizer().normalize({container: [{"id": "user_B"}, {"id": "user_A"}]})
    assert first != second
    assert Normalizer().normalize({"id": "unclassified_caller"}) == {"id": "unclassified_caller"}


def test_json_request_bodies_normalize_registered_runtime_ids_and_times() -> None:
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("POST", "/events", httpx.Response(200, json={})))
    transport.dispatch(
        httpx.Request(
            "POST",
            "https://offline/events",
            json={
                "id": "runtime_123",
                "processed_at": FakeClock().now().isoformat(),
            },
        )
    )
    data = json.loads(
        EffectRecorder().transcript(requests=transport.requests, runtime_ids=["runtime_123"])
    )
    assert data["requests"][0]["body"] == {"id": "<id:1>", "processed_at": "<time>"}
    transport.assert_consumed()


@pytest.mark.parametrize("scenario", ["unscripted", "out_of_order", "query", "body", "router"])
async def test_sdk_wrapped_unscripted_violation_cannot_be_swallowed(scenario: str) -> None:
    import anthropic

    transport = ScriptedTransport()
    if scenario == "out_of_order":
        transport.queue(
            ScriptedReply("GET", "/first", httpx.Response(200, json={})),
            ScriptedReply("GET", "/v1/sessions/missing", httpx.Response(200, json={})),
        )
    elif scenario == "query":
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/missing",
                httpx.Response(200, json={}),
                query=(("expected", "value"),),
            )
        )
    elif scenario == "body":
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/missing/events",
                httpx.Response(200, json={}),
                request_json={"different": True},
                check_json=True,
            )
        )
    elif scenario == "router":
        transport.router = MARouter()
    async with transport.client() as client:
        with pytest.raises(anthropic.APIConnectionError):
            if scenario == "body":
                await client.beta.sessions.events.send("missing", events=[])
            else:
                await client.beta.sessions.retrieve("missing")
    assert transport.violations
    with pytest.raises(AssertionError, match="MA script violations"):
        transport.assert_consumed()


def test_router_get_can_share_path_with_a_later_scripted_post() -> None:
    router = MARouter()
    router.add("GET", "/shared", lambda _request, _match: httpx.Response(200, json={}))
    transport = ScriptedTransport(router=router)
    transport.queue(
        ScriptedReply("POST", "/first", httpx.Response(200, json={})),
        ScriptedReply("POST", "/shared", httpx.Response(200, json={})),
    )
    transport.dispatch(httpx.Request("GET", "https://offline/shared"))
    assert not transport.violations
    assert transport.client(max_retries=8).max_retries == 8


async def test_existing_discord_fake_returned_message_keeps_edits_and_reactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "tests"))  # pyright: ignore[reportUnknownMemberType]  # pytest leaves path unannotated
    helpers = importlib.import_module("parity.drivers.discord_driver")
    message = helpers.DiscordDriver()._make_message(
        workspace_id="900001001",
        channel_id="100000",
        user_id="555000111",
        text="hello",
    )
    receipt = message.channel.send.return_value
    receipt.edit.return_value = receipt
    receipt.add_reaction = AsyncMock(return_value=None)
    recorder = EffectRecorder()
    client = cast(
        Any,
        RecordingPlatformClient(message.channel, recorder, platform="discord", wrap_result=True),
    )
    sent = await client.send("hello")
    assert sent.id == 42
    await sent.edit(content="edited")
    await sent.add_reaction("eyes")
    assert [effect["operation"] for effect in recorder.effects] == ["send", "edit", "add_reaction"]
    assert client.id == 100000, "plain attributes must pass through"
    assert client.get_partial_message is message.channel.get_partial_message, (
        "sync methods pass through"
    )


async def test_existing_slack_transport_fake_records_post_edit_and_reaction() -> None:
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "oracle_slack_helpers", root / "packages/adapters/slack/tests/conftest.py"
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helpers
    spec.loader.exec_module(helpers)
    from aioresponses import aioresponses
    from slack_sdk.web.async_client import AsyncWebClient

    with aioresponses() as mock:
        helpers._register_slack_defaults(mock)
        existing = helpers.FakeSlackWebClient(client=AsyncWebClient(token="xoxb-test"), mock=mock)
        recorder = EffectRecorder()
        client = cast(Any, RecordingPlatformClient(existing.client, recorder, platform="slack"))
        await client.chat_postMessage(channel="C_TEST", text="hello")
        await client.chat_update(channel="C_TEST", ts="1000000000.000001", text="edited")
        await client.reactions_add(channel="C_TEST", timestamp="1000000000.000001", name="eyes")
        assert [effect["operation"] for effect in recorder.effects] == [
            "chat_postMessage",
            "chat_update",
            "reactions_add",
        ]
