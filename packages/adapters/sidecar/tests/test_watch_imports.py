"""Offline import safety and queued private migration notices."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE, ImportedWatch, WatchExport
from daimon.core.continuity.watch_notices import poll_watch_notices
from daimon.core.stores import watches
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from pydantic import ValidationError

pytestmark = pytest.mark.fresh_schema


@pytest.fixture
async def definition(db_session_factory, monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        tenant = await make_tenant(db)
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="123"
        )
        await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="4242",
            channel_id="4241",
            account_id=principal.account_id,
            ma_session_id="session-bound",
            ma_agent_id="agent-bound",
        )
    binding = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        requester_account_id=principal.account_id,
        requester_external_user_id="123",
        target_ma_agent_id="agent-bound",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    return ImportedWatch(
        legacy_id="internal-4",
        os_binding=binding,
        source="poll",
        instructions="Report changes.",
        tool="artifact_db_query",
        arguments={"collection": "legacy"},
        expires_at=datetime.now(UTC) + timedelta(days=1),
        baseline={"value": False},
    )


async def test_unmapped_import_is_paused_and_notice_never_resumes(db_session_factory, definition):
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, definition, now=datetime.now(UTC))
        assert row.status == "paused" and row.source_state["reason"] == "source_unmapped"
        assert row.source_state["confirmed"] is False
        assert row.baseline == {"value": False}
        assert await watches.claim_poll_watches(db) == []
    deliver = AsyncMock(return_value=True)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    assert deliver.await_args.args[0].owner_external_id == "123"
    assert deliver.await_args.args[2] == UNMAPPED_NOTICE
    assert UNMAPPED_NOTICE == "this watch is paused until page data moves to Daimon OS"
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    async with db_session_factory.begin() as db:
        current = await watches.get_watch(db, row.id)
        assert current.status == "paused"
        with pytest.raises(ValueError, match="operator confirmation"):
            await watches.resume_watch(db, row.id, now=datetime.now(UTC))
        with pytest.raises(ValueError, match="Unmapped"):
            await watches.confirm_import(
                db,
                row.id,
                tenant_id=row.tenant_id,
                owner_account_id=row.owner_account_id,
                agent_id=row.agent_id,
                parent_channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                now=datetime.now(UTC),
            )
        # Pausing again must not erase the mapping refusal.
        await watches.pause_watch(db, row.id)
        assert (await watches.get_watch(db, row.id)).source_state["reason"] == "source_unmapped"


async def test_import_is_idempotent_and_never_copies_a_signing_key(db_session_factory, definition):
    page = definition.model_copy(
        update={"source": "page_event", "page_id": str(uuid.uuid4()), "legacy_id": "internal-1"}
    )
    async with db_session_factory.begin() as db:
        first = await watches.import_watch(db, page, now=datetime.now(UTC))
        again = await watches.import_watch(db, page, now=datetime.now(UTC))
        assert first.id == again.id and first.status == "paused"
        with pytest.raises(ValueError, match="fresh page signing key"):
            await watches.confirm_import(
                db,
                first.id,
                tenant_id=first.tenant_id,
                owner_account_id=first.owner_account_id,
                agent_id=first.agent_id,
                parent_channel_id=first.parent_channel_id,
                thread_id=first.thread_id,
                now=datetime.now(UTC),
            )
    payload = page.model_dump(mode="json")
    payload["signing_key"] = "never-copy-internal-secrets"
    with pytest.raises(ValidationError):
        ImportedWatch.model_validate(payload)


async def test_mapped_import_requires_exact_confirmation_before_poll_claim(
    db_session_factory, definition
):
    mapped = definition.model_copy(
        update={"tool": "discord_read_messages", "arguments": {"channel_or_thread_id": "4343"}}
    )
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, mapped, now=datetime.now(UTC))
        assert row.status == "paused" and row.source_state["reason"] == "awaiting_confirmation"
    deliver = AsyncMock(return_value=True)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_not_awaited()
    async with db_session_factory.begin() as db:
        with pytest.raises(ValueError, match="confirmation mismatch"):
            await watches.confirm_import(
                db,
                row.id,
                tenant_id=row.tenant_id,
                owner_account_id=row.owner_account_id,
                agent_id="wrong-agent",
                parent_channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                now=datetime.now(UTC),
            )
        row = await watches.confirm_import(
            db,
            row.id,
            tenant_id=row.tenant_id,
            owner_account_id=row.owner_account_id,
            agent_id=row.agent_id,
            parent_channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            now=datetime.now(UTC),
        )
        assert row.status == "active" and row.source_state["confirmed"] is True
        assert row.source_state["arguments"] == {"thread_id": "4343", "limit": 20}
        await watches.set_watch_control(db, scope="global", enabled=True)
        claimed = await watches.claim_poll_watches(db)
        assert len(claimed) == 1 and claimed[0]["watch_id"] == str(row.id)


async def test_failed_private_notice_is_retried_without_changing_status(
    db_session_factory, definition
):
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, definition, now=datetime.now(UTC))
    deliver = AsyncMock(return_value=False)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    async with db_session_factory() as db:
        row = await watches.get_watch(db, row.id)
        assert row.status == "paused" and row.source_state["notice_pending"] is True


def test_import_manifest_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        WatchExport.model_validate({"version": 1, "watches": [], "callback_token": "forbidden"})


@pytest.mark.parametrize(
    "payload,expected",
    [("false", None), ("null", None), ("0", "0"), ('{"status":"done"}', '{"status": "done"}')],
)
async def test_imported_callback_projection_filters_only_verified_signed_body(
    db_session_factory, definition, payload, expected
):
    from daimon.core.continuity.page_events import sign_page_event

    page = definition.model_copy(
        update={
            "source": "page_event",
            "page_id": str(uuid.uuid4()),
            "legacy_id": "callback-filter",
            "projection": ".payload",
        }
    )
    key = "fresh-os-key-" + "a" * 48
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, page, now=datetime.now(UTC))
        await watches.rotate_signing_key(
            db, row.id, key_id="fresh", key=key, expires_at=row.expires_at
        )
        row = await watches.confirm_import(
            db,
            row.id,
            tenant_id=row.tenant_id,
            owner_account_id=row.owner_account_id,
            agent_id=row.agent_id,
            parent_channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            now=datetime.now(UTC),
        )
        await watches.set_watch_control(db, scope="global", enabled=True)
    event = sign_page_event(
        key=key,
        watch_id=row.id,
        page_id=row.page_id,
        key_id="fresh",
        body=('{"owner":"forged","payload":' + payload + "}").encode(),
        now=datetime.now(UTC),
    )
    async with db_session_factory.begin() as db:
        wake = await watches.accept_page_event(db, event, key=key)
    async with db_session_factory() as db:
        evidence = await watches.list_evidence(db, row.id)
        if expected is None:
            assert wake is None and evidence == []
        else:
            assert wake is not None and evidence[0].payload == expected


@pytest.mark.parametrize("expression", [".x | length", "..", ".x[99999]", "." + "x" * 256])
async def test_database_projection_refuses_unreviewed_expression(
    db_session_factory, definition, expression
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with db_session_factory.begin() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("SELECT project_watch_document('{}'::jsonb,:expression)"),
                {"expression": expression},
            )


def test_import_validation_never_echoes_rejected_signing_secrets():
    key = "private-key-material-that-must-not-appear"
    with pytest.raises(ValidationError) as refused:
        WatchExport.model_validate({"version": 1, "watches": [], "signing_key": key})
    assert key not in str(refused.value)


async def test_import_confirmation_cannot_activate_self_reporting_source(
    db_session_factory, definition
):
    mapped = definition.model_copy(
        update={"tool": "read_thread", "arguments": {"thread_id": "4242"}}
    )
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, mapped, now=datetime.now(UTC))
        with pytest.raises(ValueError, match="differ from the reporting thread"):
            await watches.confirm_import(
                db,
                row.id,
                tenant_id=row.tenant_id,
                owner_account_id=row.owner_account_id,
                agent_id=row.agent_id,
                parent_channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                now=datetime.now(UTC),
            )
    async with db_session_factory() as db:
        current = await watches.get_watch(db, row.id)
        assert current.status == "paused" and current.source_state["confirmed"] is False


@pytest.mark.parametrize(
    "tool,arguments,expected,error",
    [
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343", "limit": 10},
            ("read_thread", {"thread_id": "4343", "limit": 10}),
            None,
        ),
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343"},
            ("read_thread", {"thread_id": "4343", "limit": 20}),
            None,
        ),
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343", "limit": 50},
            ("read_thread", {"thread_id": "4343", "limit": 20}),
            None,
        ),
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343", "limit": True},
            None,
            "limit must be an integer",
        ),
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343", "limit": 0},
            None,
            "Poll read limit must be 1 to 20",
        ),
        (
            "discord_read_channel_since",
            {"channel_id": "4343", "since": "2026-10-01", "limit": 10},
            None,
            "Unsupported imported channel-history arguments",
        ),
        (
            "discord_read_channel_since",
            {"channel_id": "4343", "since": "2026-10-01T00:00:00Z"},
            ("read_channel", {"channel_id": "4343"}),
            None,
        ),
        (
            "discord_read_channel_since",
            {"channel_id": "discord_channel:4343", "since": "2026-10-01"},
            ("read_channel", {"channel_id": "4343"}),
            None,
        ),
        ("discord_read_messages", {"limit": 10}, None, "canonical message resource ID"),
        (
            "discord_read_messages",
            {"channel_or_thread_id": "4343", "limit": 10, "before": "1234"},
            None,
            "Unsupported imported message-read arguments",
        ),
        (
            "discord_read_channel_since",
            {"channel_id": "4343", "since": "2026-10-01", "before": "2026-10-02"},
            None,
            "Unsupported imported channel-history arguments",
        ),
        (
            "discord_read_channel_since",
            {"since": "2026-10-01"},
            None,
            "canonical message resource ID",
        ),
        (
            "discord_read_channel_since",
            {"channel_id": "4343", "since": "2026-10-01", "before_message_id": 1234},
            None,
            "Unsupported imported channel-history arguments",
        ),
    ],
)
async def test_internal_discord_manifest_import(
    db_session_factory, definition, tool, arguments, expected, error
):
    # Source: daimon-internal/apps/bot/src_v2/db/models/watch.py
    # WatchDefinition and core/watches/model.py WatchState. The internal checkout
    # has no exporter; docs/self-hosting.md specifies this operator-built export:
    # tool_args -> arguments, select_expr -> projection, interval_seconds ->
    # interval, state.value -> baseline.value, with a verified OS binding.
    internal = {
        "id": "00000000-0000-0000-0000-000000000004",
        "source": "poll",
        "instructions": "Report changes.",
        "tool": tool,
        "tool_args": arguments,
        "select_expr": ".",
        "rule": "change",
        "interval_seconds": 600,
        "state": {"value": False, "delivery_hashes": []},
        "expires_at": definition.expires_at.isoformat(),
        "max_turns_per_day": 24,
    }
    manifest = {
        "version": 1,
        "watches": [
            {
                "legacy_id": internal["id"],
                "os_binding": definition.os_binding.model_dump(mode="json"),
                "source": internal["source"],
                "instructions": internal["instructions"],
                "tool": internal["tool"],
                "arguments": internal["tool_args"],
                "projection": internal["select_expr"],
                "rule": internal["rule"],
                "interval": internal["interval_seconds"],
                "baseline": {"value": internal["state"]["value"]},
                "expires_at": internal["expires_at"],
                "max_turns_per_day": internal["max_turns_per_day"],
            }
        ],
    }
    imported = WatchExport.model_validate(manifest).watches[0]
    async with db_session_factory.begin() as db:
        row = await watches.import_watch(db, imported, now=datetime.now(UTC))
        assert row.status == "paused"
        assert row.source_state["confirmed"] is False
        assert row.source_state["notice_pending"] is False
        assert await watches.claim_import_notice(db, now=datetime.now(UTC)) is None
        assert await watches.claim_poll_watches(db) == []
        if expected:
            assert row.source_state["reason"] == "awaiting_confirmation"
            assert row.source_state["mapping_error"] is None
            assert (row.source_state["tool"], row.source_state["arguments"]) == expected
        else:
            assert row.source_state["reason"] == "source_unmapped"
            assert error in row.source_state["mapping_error"]
            with pytest.raises(ValueError, match="Unmapped"):
                await watches.confirm_import(
                    db,
                    row.id,
                    tenant_id=row.tenant_id,
                    owner_account_id=row.owner_account_id,
                    agent_id=row.agent_id,
                    parent_channel_id=row.parent_channel_id,
                    thread_id=row.thread_id,
                    now=datetime.now(UTC),
                )
