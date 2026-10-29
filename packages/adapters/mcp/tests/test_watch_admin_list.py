"""Listing every watch is admin-only, one tenant at a time, and never shows secrets."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.watches import WatchListEntry, list_watches_impl
from daimon.core.stores.domain import Role, WatchRow
from daimon.core.stores.platform_names import KnownName
from daimon.core.stores.watches import WatchActivity
from fastmcp.exceptions import ToolError

TENANT = uuid.uuid4()
CALLER = uuid.uuid4()


def watch(owner: uuid.UUID, external_id: str) -> WatchRow:
    now = datetime.now(UTC)
    return WatchRow(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        owner_account_id=owner,
        owner_external_id=external_id,
        platform="discord",
        parent_channel_id="11",
        thread_id="12",
        agent_id="agent",
        agent_name="analyst",
        session_id="session-secret",
        instructions="SECRET INSTRUCTIONS",
        producer="watch:producer-secret",
        page_id=uuid.uuid4(),
        revision=1,
        source="poll",
        status="paused",
        expires_at=now + timedelta(days=1),
        max_turns_per_day=24,
        max_fires=None,
        fire_count=3,
        baseline={"value": "EVIDENCE"},
        rule={"projection": ".rows", "kind": "change"},
        source_state={
            "tool": "read_channel",
            "arguments": {"channel_id": "SOURCE-ID"},
            "interval": 60,
            "reason": "access_revoked",
        },
        lease_owner="lease-secret",
        lease_expires_at=None,
        next_check_at=None,
        backoff_until=now + timedelta(minutes=5),
        consecutive_errors=2,
    )


@pytest.fixture
def runtime():
    @asynccontextmanager
    async def session():
        yield object()

    return SimpleNamespace(session_factory=session)


def caller() -> AuthIdentity:
    return AuthIdentity(
        account_id=CALLER,
        tenant_id=TENANT,
        platform="discord",
        platform_user_id="1",
        external_id="guild",
        role=Role.ADMIN,
    )


async def test_non_admin_is_refused_before_any_read(runtime):
    listing = AsyncMock()
    with (
        patch("daimon.adapters.mcp.tools.watches.fresh_admin", AsyncMock(return_value=False)),
        patch("daimon.core.stores.watches.list_tenant_watches", listing),
        pytest.raises(ToolError, match="Only an admin can see every watch. Ask an admin"),
    ):
        await list_watches_impl(runtime, caller(), scope="all")
    listing.assert_not_called()


async def test_admin_listing_is_tenant_scoped_and_carries_no_secrets(runtime):
    mine, theirs = watch(CALLER, "1"), watch(uuid.uuid4(), "2")
    last = datetime.now(UTC)
    listing = AsyncMock(return_value=[WatchActivity(mine, 0, None), WatchActivity(theirs, 4, last)])
    with (
        patch("daimon.adapters.mcp.tools.watches.fresh_admin", AsyncMock(return_value=True)),
        patch("daimon.core.stores.watches.list_tenant_watches", listing),
        patch(
            "daimon.core.stores.platform_names.get_user_names",
            AsyncMock(return_value={"2": KnownName("Ada")}),
        ),
    ):
        listed = await list_watches_impl(runtime, caller(), scope="all")
    assert listing.await_args.kwargs["tenant_id"] == TENANT
    assert listing.await_args.kwargs["owner_account_id"] is None
    assert [entry.owner for entry in listed] == ["you", "Ada"]
    entry = listed[1]
    assert (entry.status, entry.reason, entry.source, entry.source_tool) == (
        "paused",
        "access_revoked",
        "poll",
        "read_channel",
    )
    assert (entry.destination, entry.fires_today, entry.last_fired_at, entry.errors) == (
        "discord thread",
        4,
        last,
        2,
    )
    assert entry.retry_after == theirs.backoff_until and entry.key_id is None
    dumped = WatchListEntry.model_json_schema() | {"rows": [e.model_dump() for e in listed]}
    for secret in ("SECRET", "EVIDENCE", "SOURCE-ID", "lease-secret", "producer-secret"):
        assert secret not in str(dumped)
    assert "instructions" not in WatchListEntry.model_fields


async def test_own_listing_needs_no_admin_and_asks_for_own_watches(runtime):
    listing = AsyncMock(return_value=[])
    with (
        patch("daimon.adapters.mcp.tools.watches.fresh_admin", AsyncMock()) as admin,
        patch("daimon.core.stores.watches.list_tenant_watches", listing),
    ):
        assert await list_watches_impl(runtime, caller()) == []
    admin.assert_not_called()
    assert listing.await_args.kwargs["owner_account_id"] == CALLER
