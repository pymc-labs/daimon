"""A DM with no conversation starts one on the routed agent, offers a choice, or says why not."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.runtime import new_dm_bursts
from daimon.core.authz import AgentRef
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.direct_messages import DM_AGENT_REFUSED, ColdDmNotices
from daimon.core.dm_access import DmAgent
from daimon.core.dm_routing import DmChoose, DmDecision, DmRefused, DmStart, DmUnchecked, Route
from daimon.core.native_admins import GroupMembersCache
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores.direct_messages import DirectMessageRow, set_dm_enabled
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OFF = (
    "Private chats with Daimon are off here.\n"
    "Mention me in a channel, or ask a workspace admin to run `/dm-me enable`."
)
_ON = (
    "I answer in channels first.\n"
    "Mention me in a channel, or run `/dm-me` in one to continue privately here."
)


@pytest.fixture(autouse=True)
def fresh_notices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_module, "_COLD_DM_NOTICES", ColdDmNotices())
    monkeypatch.setattr(dm_module, "_PENDING_PICKS", {})
    monkeypatch.setattr(dm_module, "debounce_seconds", AsyncMock(return_value=0.0))
    monkeypatch.setattr(dm_module, "list_dm_agents", AsyncMock(return_value=[]))


def _runtime(db_session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    runtime = MagicMock()
    runtime.dm_bursts = new_dm_bursts()
    runtime.sessionmaker = db_session_factory
    runtime.settings.direct_message_policies = {}
    runtime.settings.features = {}
    runtime.settings.agent_identity.enabled = False
    runtime.group_members = GroupMembersCache()
    return runtime


def _client(monkeypatch: pytest.MonkeyPatch, team: str = "T_ROUTE") -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.chat_update = AsyncMock()
    client.reactions_add = client.reactions_remove = AsyncMock()
    client.users_info = AsyncMock(return_value={"user": {"team_id": team}})
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    return client


def _event(*, team_user: str = "U1", channel: str = "D_COLD", ts: str = "1.1") -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": team_user,
        "ts": ts,
        "text": "hello?",
    }


async def test_cold_dm_with_private_chats_off_gets_the_two_line_answer(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_OFF"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", **notice_fields(_OFF))


async def test_cold_dm_with_private_chats_on_points_to_dm_me(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T_COLD_ON")
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.tenant_id, enabled=True)
    client = _client(monkeypatch, team="T_COLD_ON")

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_ON"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", **notice_fields(_ON))


async def test_dms_off_after_a_conversation_exists_gets_the_off_answer(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key="T_COLD_CONV:D_COLD",
            external_user_id="U1",
            workspace_id="T_COLD_CONV",
            source_channel_id="C1",
        )
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=False)
    client = _client(monkeypatch)

    await dm_module.handle_direct_message(
        _runtime(db_session_factory), _event(), team_id="T_COLD_CONV"
    )

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", **notice_fields(_OFF))


async def test_each_person_is_answered_once_per_process(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch)
    runtime = _runtime(db_session_factory)

    await dm_module.handle_direct_message(runtime, _event(ts="1.1"), team_id="T_COLD_ONCE")
    await dm_module.handle_direct_message(runtime, _event(ts="1.2"), team_id="T_COLD_ONCE")
    await dm_module.handle_direct_message(
        runtime, _event(team_user="U2", channel="D_OTHER"), team_id="T_COLD_ONCE"
    )

    assert [call.kwargs["channel"] for call in client.chat_postMessage.await_args_list] == [
        "D_COLD",
        "D_OTHER",
    ]


async def test_a_failed_answer_is_retried_on_the_next_message(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch)
    client.chat_postMessage.side_effect = [
        SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"}),
        None,
    ]
    runtime = _runtime(db_session_factory)

    await dm_module.handle_direct_message(runtime, _event(ts="1.1"), team_id="T_COLD_RETRY")
    await dm_module.handle_direct_message(runtime, _event(ts="1.2"), team_id="T_COLD_RETRY")

    assert client.chat_postMessage.await_count == 2


async def _dms_on(factory: async_sessionmaker[AsyncSession], team: str = "T_ROUTE") -> Any:
    tenant = await provision_tenant(factory, platform="slack", workspace_id=team)
    async with factory.begin() as session:
        await set_dm_enabled(session, tenant_id=tenant.tenant_id, enabled=True)
    return tenant


def _decided(monkeypatch: pytest.MonkeyPatch, route: Route, allowed: tuple[str, ...] = ()) -> None:
    decide = AsyncMock(return_value=(DmDecision(route, allowed), None))
    monkeypatch.setattr(dm_module, "_decide_cold", decide)


def _row(tenant_id: Any) -> DirectMessageRow:
    return DirectMessageRow(
        platform="slack", route_key="T_ROUTE:D_COLD", external_user_id="U1",
        tenant_id=tenant_id, account_id=uuid.uuid4(), memory_read_only=False,
        workspace_id="T_ROUTE", channel_id="D_COLD",
        scope_id="dm:x", source_url="", context="", history=[], recent_message_ids=[],
        active_until=None,
    )  # fmt: skip


def _starts(monkeypatch: pytest.MonkeyPatch, tenant_id: Any) -> tuple[AsyncMock, AsyncMock]:
    start = AsyncMock(return_value=_row(tenant_id))
    reply = AsyncMock(return_value="hi there")
    monkeypatch.setattr(dm_module, "admit_cold_dm", AsyncMock())
    monkeypatch.setattr(dm_module, "start_cold_dm", start)
    monkeypatch.setattr(dm_module, "reply_to_dm", reply)
    return start, reply


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (DmRefused("locked_unavailable"), DM_AGENT_REFUSED),
        (DmUnchecked(), dm_module._UNCHECKED),  # pyright: ignore[reportPrivateUsage]
        (DmRefused("no_agent"), _ON),
    ],
)
async def test_cold_dm_when_no_agent_takes_it_then_the_reason_is_an_error_card(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    route: Route,
    expected: str,
) -> None:
    await _dms_on(db_session_factory)
    client = _client(monkeypatch)
    _decided(monkeypatch, route)

    await dm_module.handle_direct_message(_runtime(db_session_factory), _event(), team_id="T_ROUTE")

    client.chat_postMessage.assert_awaited_once_with(channel="D_COLD", **notice_fields(expected))


async def test_cold_dm_when_one_agent_is_routed_then_it_starts_and_answers_at_once(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _dms_on(db_session_factory)
    client = _client(monkeypatch)
    _decided(monkeypatch, DmStart("ada", "only"))
    start, reply = _starts(monkeypatch, tenant.tenant_id)

    await dm_module.handle_direct_message(_runtime(db_session_factory), _event(), team_id="T_ROUTE")

    kwargs = start.await_args.kwargs
    assert (kwargs["platform"], kwargs["route_key"], kwargs["channel_id"]) == (
        "slack", "T_ROUTE:D_COLD", "D_COLD",
    ), "the route is the workspace's DM; the binding names the DM channel"  # fmt: skip
    assert reply.await_args.kwargs["text"] == "hello?", "the first message is answered"
    assert client.chat_postMessage.await_args.kwargs["text"] == "hi there", "the reply is posted"


async def test_cold_dm_when_several_agents_fit_then_a_pick_starts_and_answers_the_wait(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _dms_on(db_session_factory)
    client = _client(monkeypatch)
    runtime = _runtime(db_session_factory)
    _decided(monkeypatch, DmChoose(("ada", "bob")), allowed=("ada", "bob"))
    start, reply = _starts(monkeypatch, tenant.tenant_id)

    await dm_module.handle_direct_message(runtime, _event(), team_id="T_ROUTE")
    select = client.chat_postMessage.await_args.kwargs["blocks"][0]["accessory"]
    assert [o["value"] for o in select["options"]] == ["ada", "bob"], "both agents are offered"
    assert start.await_count == 0, "nothing starts before the pick"

    await dm_module.handle_dm_agent_pick(runtime, _pick("bob"))

    assert start.await_args.kwargs["remember_pick"] is True, "a chosen agent is remembered"
    assert reply.await_args.kwargs["text"] == "hello?", "the waiting message is answered"
    await dm_module.handle_dm_agent_pick(runtime, _pick("bob"))
    assert client.chat_postMessage.await_args.kwargs == {
        "channel": "D_COLD",
        **notice_fields(dm_module._PICK_EXPIRED),  # pyright: ignore[reportPrivateUsage]
    }, "a second click finds the choice used"


async def test_cold_dm_when_more_arrive_before_the_pick_then_one_offer_answers_them_all(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _dms_on(db_session_factory)
    client = _client(monkeypatch)
    runtime = _runtime(db_session_factory)
    monkeypatch.setattr(dm_module, "debounce_seconds", AsyncMock(return_value=0.2))
    _decided(monkeypatch, DmChoose(("ada", "bob")), allowed=("ada", "bob"))
    _, reply = _starts(monkeypatch, tenant.tenant_id)
    first = asyncio.create_task(
        dm_module.handle_direct_message(runtime, _event(), team_id="T_ROUTE")
    )
    await asyncio.sleep(0.05)
    await dm_module.handle_direct_message(
        runtime, {**_event(ts="1.2"), "text": "yes"}, team_id="T_ROUTE"
    )
    await first

    offers = [c for c in client.chat_postMessage.await_args_list if "blocks" in c.kwargs]
    assert len(offers) == 1, "one choice for the whole burst"
    await dm_module.handle_dm_agent_pick(runtime, _pick("ada"))
    texts = [c.kwargs["text"] for c in reply.await_args_list]
    assert texts == ["hello?", "yes"], "the pick answers both, the approval on its own"


async def test_agent_pick_when_access_changed_meanwhile_then_refused(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _dms_on(db_session_factory)
    client = _client(monkeypatch)
    runtime = _runtime(db_session_factory)
    _decided(monkeypatch, DmChoose(("ada", "bob")), allowed=("ada", "bob"))
    start, _ = _starts(monkeypatch, tenant.tenant_id)
    await dm_module.handle_direct_message(runtime, _event(), team_id="T_ROUTE")
    _decided(monkeypatch, DmStart("ada", "locked"))

    await dm_module.handle_dm_agent_pick(runtime, _pick("bob"))

    assert start.await_count == 0, "a lock set meanwhile wins"
    assert client.chat_postMessage.await_args.kwargs == {
        "channel": "D_COLD",
        **notice_fields(DM_AGENT_REFUSED),
    }, "the person is told to ask an admin"


def _pick(agent: str) -> dict[str, Any]:
    return {
        "type": "block_actions", "team": {"id": "T_ROUTE"}, "user": {"id": "U1"},
        "channel": {"id": "D_COLD"}, "container": {"message_ts": "9.9"},
        "actions": [{"action_id": "dm_pick_agent", "selected_option": {"value": agent}}],
    }  # fmt: skip


@pytest.mark.parametrize(
    ("reply", "expected"),
    [({"users": ["U1"]}, DmStart("ada", "only")), (None, DmUnchecked())],
)
async def test_decide_cold_when_a_grant_lists_a_user_group_then_slack_membership_decides(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any] | None,
    expected: Route,
) -> None:
    tenant = await _dms_on(db_session_factory)
    async with db_session_factory.begin() as session:
        await dm_store.set_grant(
            session, tenant_id=tenant.tenant_id, agent_name="ada", who="listed", user_ids=[],
            group_ids=["S1"], set_by=None, now=datetime.now(UTC),
        )  # fmt: skip
    agents = [DmAgent("ada", "ag_ada", AgentRef.of("ada"))]
    monkeypatch.setattr(dm_module, "list_dm_agents", AsyncMock(return_value=agents))
    client = _client(monkeypatch)
    failure = SlackApiError("missing_scope", {"ok": False, "error": "missing_scope"})
    client.usergroups_users_list = AsyncMock(
        return_value=reply, side_effect=None if reply else failure
    )

    decision, _ = await dm_module._decide_cold(  # pyright: ignore[reportPrivateUsage]
        _runtime(db_session_factory), client, team_id="T_ROUTE", user_id="U1", role=Role.USER
    )

    assert decision.route == expected, "the user group decides; a failed lookup fails closed"
