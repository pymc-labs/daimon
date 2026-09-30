"""The channel admins form's submission: what it reads, who may save, what it stores."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import channel_admins
from daimon.adapters.slack.agent_setup.channel_admins import (
    ChannelAdminsSubmission,
    evaluate_channel_admins_submission,
    run_channel_admins_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import CHANNEL_ADMINS_INPUT_ID
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM = "T0ADMINS"
CHANNEL = "C0GROWTH"
META = PanelMetadata(team_id=TEAM, channel_id=CHANNEL, view="channel_admins", root_view_id="V1")


def _payload(users: list[str] | None) -> dict[str, Any]:
    picked: dict[str, Any] = {} if users is None else {"selected_users": users}
    return {
        "view": {
            "private_metadata": encode_panel_metadata(META),
            "state": {"values": {CHANNEL_ADMINS_INPUT_ID: {CHANNEL_ADMINS_INPUT_ID: picked}}},
        }
    }


def test_evaluate_reads_the_picked_members_and_the_panel_channel() -> None:
    decision = evaluate_channel_admins_submission(_payload(["U0LEAD"]))
    assert decision == ChannelAdminsSubmission(meta=META, user_ids=("U0LEAD",))
    empty = evaluate_channel_admins_submission(_payload(None))
    assert empty is not None and empty.user_ids == ()
    assert evaluate_channel_admins_submission({"view": {}}) is None, "no panel metadata"


def _client() -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.views_update = AsyncMock()
    return client


async def _run(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    admin: bool,
    users: tuple[str, ...],
) -> MagicMock:
    monkeypatch.setattr(channel_admins, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(channel_admins, "load_routing_view", AsyncMock(return_value={"v": 1}))
    runtime = MagicMock()
    runtime.sessionmaker = sessionmaker
    client = _client()
    await run_channel_admins_submission(
        runtime,
        client,
        team_id=TEAM,
        user_id="U0ADMIN",
        submission=ChannelAdminsSubmission(meta=META, user_ids=users),
    )
    return client


async def test_admin_saves_then_clears_and_the_routing_view_refreshes(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="slack", workspace_id=TEAM)

    client = await _run(db_session_factory, monkeypatch, admin=True, users=("U0LEAD", "U0LEAD"))
    async with db_session_factory() as session:
        (row,) = await list_channel_admins(session, tenant_id=tenant_id, platform="slack")
    assert (row.channel_id, row.user_ids) == (CHANNEL, ("U0LEAD",))
    assert row.updated_by_account_id is not None, "the saver is attributed"
    client.views_update.assert_awaited_once_with(view_id="V1", view={"v": 1})

    await _run(db_session_factory, monkeypatch, admin=True, users=())
    async with db_session_factory() as session:
        assert await list_channel_admins(session, tenant_id=tenant_id, platform="slack") == []


async def test_member_submission_is_refused_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = MagicMock()
    client = await _run(sessionmaker, monkeypatch, admin=False, users=("U0LEAD",))
    client.chat_postEphemeral.assert_awaited_once()
    sessionmaker.begin.assert_not_called()
    client.views_update.assert_not_awaited()
