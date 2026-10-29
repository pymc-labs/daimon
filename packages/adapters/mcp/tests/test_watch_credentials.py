"""Owner-supplied watch keys: each owner's watches read with that owner's key only.

The fake server is the MCP test fixture with per-owner bearers, so its
journal names whose key every poll used.
"""

# ruff: noqa: F811

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp import watch_polling
from daimon.adapters.mcp.tools.credential_requests import (
    _forget_watch_credential_impl,  # pyright: ignore[reportPrivateUsage]
    _request_watch_credential_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.watches import create_watch_impl
from daimon.adapters.mcp.watch_polling import (
    CredentialMissing,
    UrlBudget,
    poll_credential,
    run_poll,
)
from daimon.core.continuity.watch_notices import notice_text
from daimon.core.credential_requests import mint_request_token
from daimon.core.credential_submit import apply_watch_credential_submit
from daimon.core.github_credentials import encrypt_token
from daimon.core.mcp_tool_call import mcp_tool_session
from daimon.core.stores import watch_credentials, watches
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.factories import make_tenant
from daimon.testing.mcp_fixture_server import build_fixture_server
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from .test_watch_mcp_source import (
    BRIDGE,
    NATIVE,
    PINNED,
    Recording,
    agent_with,
    entry,
    lease,
    pinned,
)
from .test_watch_polling import attended_owner, runtime_watch  # noqa: F401

pytestmark = pytest.mark.fresh_schema

AGENT_TOKEN = "agent-bearer-sentinel-" + "l" * 40
OWNER_A = "owner-a-key-sentinel-" + "a" * 40
OWNER_A2 = "owner-a-rotated-key-sentinel-" + "r" * 40
OWNER_B = "owner-b-key-sentinel-" + "b" * 40


@pytest.fixture
async def owner_server(
    attended_owner, monkeypatch, tmp_path: Path
) -> AsyncIterator[SimpleNamespace]:
    runtime, native, auth, origin = attended_owner
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read", credential="owner")]),
    )
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    runtime.fernet = fernet
    runtime.settings.tool_safety = ToolSafetyPolicy()
    async with runtime.session_factory.begin() as db:
        other = await get_or_create_platform_principal(
            db, tenant_id=native.tenant_id, platform="discord", external_id="456"
        )
    app = build_fixture_server(
        token=AGENT_TOKEN,
        journal_path=tmp_path / "calls.jsonl",
        owner_tokens={"owner-a": OWNER_A, "owner-a-rotated": OWNER_A2, "owner-b": OWNER_B},
    )
    transport = Recording(httpx.ASGITransport(app=app))
    monkeypatch.setattr(
        watch_polling, "open_source", partial(mcp_tool_session, resolve=pinned, transport=transport)
    )
    agent = AsyncMock(return_value=agent_with(("fixture-bridge", BRIDGE)))
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.retrieve_agent", agent)
    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.retrieve_agent", agent)
    monkeypatch.setattr("daimon.adapters.mcp.tools.credential_requests.retrieve_agent", agent)
    monkeypatch.setattr(watch_polling, "SOURCE_BUDGET", UrlBudget())

    async def save(account_id: uuid.UUID, key: str, tenant_id: uuid.UUID = native.tenant_id):
        async with runtime.session_factory.begin() as db:
            await watch_credentials.save_watch_credential(
                db,
                tenant_id=tenant_id,
                owner_account_id=account_id,
                server_url=BRIDGE,
                token_encrypted=encrypt_token(fernet, key),
                now=datetime.now(UTC),
            )

    async def journal() -> list[dict[str, Any]]:
        return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]

    async with asgi_lifespan(app):
        yield SimpleNamespace(
            runtime=runtime,
            native=native,
            auth=auth,
            origin=origin,
            other=other.account_id,
            transport=transport,
            save=save,
            journal=journal,
        )


async def create(fx: SimpleNamespace):
    return await create_watch_impl(
        fx.runtime,
        fx.auth,
        origin_context_id=fx.origin,
        instructions="Report new markers.",
        source="poll",
        server="fixture-bridge",
        tool="fixture_counter_read",
        arguments={},
        projection=".markers",
        rule="added",
        interval=60,
    )


async def get(fx: SimpleNamespace, watch_id: uuid.UUID):
    async with fx.runtime.session_factory() as db:
        return await watches.get_watch(db, watch_id)


def executed_by(calls: list[dict[str, Any]]) -> list[str]:
    return [c["caller"] for c in calls if c["phase"] == "executed"]


async def test_a_watch_polls_with_its_owners_key_sent_through_the_private_form(owner_server):
    fx = owner_server
    async with fx.runtime.session_factory.begin() as db:
        row = await create_credential_request(
            db,
            token=mint_request_token(),
            kind="watch_credential",
            tenant_id=fx.native.tenant_id,
            agent_id=uuid.uuid4(),
            account_id=fx.native.owner_account_id,
            target="fixture.example",
            mcp_server_url=BRIDGE,
            requester_platform_user_id=fx.native.owner_external_id,
            channel_id=fx.native.thread_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=fx.native.agent_id,
            target_name="analyst",
            requested_work=None,
        )
    assert await apply_watch_credential_submit(
        fx.runtime.session_factory,
        row=row,
        fernet=fx.runtime.fernet,
        value=OWNER_A,
        now=datetime.now(UTC),
    )
    created = await create(fx)
    watch = await get(fx, created.watch_id)
    assert watch.source_state["credential"] == "owner"
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    # Every request went to the exact registered URL, with the owner's own key.
    assert {
        (request.url.host, request.headers["host"], request.url.path)
        for request in fx.transport.requests
    } == {(PINNED, "fixture.example", "/mcp")}
    assert {request.headers["authorization"] for request in fx.transport.requests} == {
        f"Bearer {OWNER_A}"
    }
    assert executed_by(await fx.journal()) == ["owner-a"]


async def test_owner_as_watch_never_loads_owner_bs_key(owner_server):
    fx = owner_server
    await fx.save(fx.other, OWNER_B)
    with pytest.raises(ToolError, match="request_watch_credential"):
        await create(fx)
    with pytest.raises(CredentialMissing):
        await poll_credential(
            fx.runtime,
            tenant_id=fx.native.tenant_id,
            agent_id=fx.native.agent_id,
            owner_account_id=fx.native.owner_account_id,
            server_url=BRIDGE,
            credential="owner",
        )
    # The same owner id in another tenant is a different key, never this one.
    async with fx.runtime.session_factory.begin() as db:
        elsewhere = await make_tenant(db)
    with pytest.raises(CredentialMissing):
        await poll_credential(
            fx.runtime,
            tenant_id=elsewhere.id,
            agent_id=fx.native.agent_id,
            owner_account_id=fx.other,
            server_url=BRIDGE,
            credential="owner",
        )
    # Nothing was sent anywhere: create refused before reading the server.
    assert fx.transport.requests == []


async def test_a_rotated_key_takes_effect_on_the_next_poll(owner_server):
    fx = owner_server
    await fx.save(fx.native.owner_account_id, OWNER_A)
    watch = await get(fx, (await create(fx)).watch_id)
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    await fx.save(fx.native.owner_account_id, OWNER_A2)
    assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert executed_by(await fx.journal()) == ["owner-a", "owner-a-rotated"]


async def test_a_forgotten_key_pauses_the_watch_and_tells_the_owner(owner_server):
    fx = owner_server
    await fx.save(fx.native.owner_account_id, OWNER_A)
    watch = await get(fx, (await create(fx)).watch_id)
    forgotten = await _forget_watch_credential_impl(
        fx.runtime, fx.auth, server="fixture-bridge", origin_context_id=fx.origin
    )
    assert forgotten.forgotten and forgotten.server_host == "fixture.example"
    before = len(fx.transport.requests)
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    assert len(fx.transport.requests) == before, "no request is sent without the owner's key"
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "credential_missing"
    async with fx.runtime.session_factory.begin() as db:
        claimed = await watches.claim_import_notice(db, now=datetime.now(UTC))
    assert claimed is not None and claimed[0].id == watch.id
    assert "read-only key" in notice_text(claimed[0], "credential_missing")


async def test_erasure_deletes_the_owners_keys(owner_server):
    fx = owner_server
    await fx.save(fx.native.owner_account_id, OWNER_A)
    await fx.save(fx.other, OWNER_B)
    async with fx.runtime.session_factory.begin() as db:
        await watches.delete_for_account(db, account_id=fx.native.owner_account_id)
    async with fx.runtime.session_factory() as db:
        owners = set(await db.scalars(text("SELECT owner_account_id FROM watch_credentials")))
    assert owners == {fx.other}, "only the erased account's keys are gone"


async def test_no_key_reaches_logs_rows_or_evidence(owner_server, caplog):
    fx = owner_server
    caplog.set_level(logging.DEBUG)
    await fx.save(fx.native.owner_account_id, OWNER_A)
    with structlog.testing.capture_logs() as logs:
        watch = await get(fx, (await create(fx)).watch_id)
        assert await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
        # A failing poll must not leak the key through its error path either.
        await fx.save(fx.native.owner_account_id, "x" * 40)
        assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    async with fx.runtime.session_factory() as db:
        dumped = [
            await db.scalar(text(f"SELECT coalesce(string_agg(t::text, ''), '') FROM {table} t"))
            for table in (
                "watches",
                "watch_inbox",
                "watch_firings",
                "task_continuations",
                "credential_requests",
                "watch_credentials",
            )
        ]
    for sentinel in (OWNER_A, OWNER_A2, OWNER_B, AGENT_TOKEN):
        assert sentinel not in caplog.text and sentinel not in repr(logs)
        assert all(sentinel not in str(table) for table in dumped)


async def test_the_form_is_posted_only_for_a_server_listed_with_owner_keys(
    owner_server, monkeypatch
):
    fx = owner_server
    posted = AsyncMock(return_value="sentinel")
    monkeypatch.setattr("daimon.adapters.mcp.tools.credential_requests._mint_and_post", posted)
    assert (
        await _request_watch_credential_impl(
            fx.runtime, fx.auth, server="fixture-bridge", origin_context_id=fx.origin
        )
        == "sentinel"
    )
    sent = posted.await_args.kwargs
    assert sent["kind"] == "watch_credential" and sent["mcp_server_url"] == BRIDGE
    assert sent["changes_agent"] is False and sent["requested_work"] is None
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read")]),
    )
    with pytest.raises(ToolError, match="Ask an admin or an owner"):
        await _request_watch_credential_impl(
            fx.runtime, fx.auth, server="fixture-bridge", origin_context_id=fx.origin
        )
    assert posted.await_count == 1


async def test_an_owner_keyed_entry_switched_to_agent_pauses_the_watch(owner_server, monkeypatch):
    fx = owner_server
    await fx.save(fx.native.owner_account_id, OWNER_A)
    watch = await get(fx, (await create(fx)).watch_id)
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS",
        json.dumps([*NATIVE, entry("fixture_counter_read")]),
    )
    before = len(fx.transport.requests)
    assert not await run_poll(fx.runtime, watch.id, await lease(fx.runtime, watch))
    current = await get(fx, watch.id)
    assert current.status == "paused" and current.source_state["reason"] == "access_revoked"
    assert len(fx.transport.requests) == before, "the changed entry is refused before any read"
