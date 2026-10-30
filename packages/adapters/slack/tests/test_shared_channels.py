"""Slack Connect: members of another workspace use the bot in shared channels as held users.

Covers, with synthetic ids only (T_HOST, T_OTHER, C_SHARED, U_EXTERNAL):
- the requester's organisation from users.info and the event's team fields
- shared-channel detection (is_ext_shared, cache, failure, envelope flag)
- channel_shared / channel_unshared events and the registry
- a mention reaching admission as a held turn (external, internal in a shared
  channel, internal in an ordinary channel)
- the click and view_submission allowlist for external members
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.slack.admin import SlackRequester
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.external_access import (
    external_action_allowed,
    external_submission_allowed,
    host_only_notice,
)
from daimon.adapters.slack.feedback import (
    FEEDBACK_DETAILS_ACTION_ID,
    FEEDBACK_TEXT_CALLBACK_ID,
    FEEDBACK_VOTE_UP,
)
from daimon.adapters.slack.requester_org import decide_requester_org
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.shared_channel import (
    ENVELOPE_EXT_SHARED_KEY,
    SharedChannelProbe,
    handle_shared_channel_event,
)
from daimon.adapters.slack.support_escalation import ASK_HUMAN_ACTION_ID
from daimon.adapters.slack.tool_confirmation import SlackConfirmationCards
from daimon.core.confirmation import ApprovedConfirmation, ConfirmationPrompt, prompt_for_tool_call
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls.confirmation import NOT_YOURS_MESSAGE
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.shared_channels import is_channel_shared
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import deps_for
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import SecretStr
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

from .conftest import shown_text
from .harness import make_orchestrate_app, register_conversations_info

_HOST = "T_HOST"
_OTHER = "T_OTHER"
_CHANNEL = "C_SHARED"
_EXTERNAL = "U_EXTERNAL"
_HOST_NAME = "Host Workspace"
_HOST_COPY = f"That's only for people at {_HOST_NAME}."


# ---------------------------------------------------------------------------
# Requester organisation
# ---------------------------------------------------------------------------


def _requester(
    *, team_id: str | None = _HOST, is_stranger: bool = False, is_admin: bool = False
) -> SlackRequester:
    host_member = team_id == _HOST and not is_stranger
    return SlackRequester(
        is_admin=is_admin and host_member,
        display_name=None,
        team_id=team_id,
        is_stranger=is_stranger,
        host_team_id=_HOST,
    )


@pytest.mark.parametrize(
    ("requester", "event", "org", "home", "admin"),
    [
        pytest.param(_requester(), {}, RequesterOrg.HOST, None, False, id="host-member"),
        pytest.param(_requester(is_admin=True), {}, RequesterOrg.HOST, None, True, id="host-admin"),
        pytest.param(
            _requester(team_id=_OTHER, is_admin=True),
            {},
            RequesterOrg.OTHER,
            _OTHER,
            False,
            id="external-without-event-fields-from-users-info",
        ),
        pytest.param(
            _requester(team_id=_OTHER),
            {"user_team": _OTHER},
            RequesterOrg.OTHER,
            _OTHER,
            False,
            id="external-with-event-fields",
        ),
        pytest.param(
            _requester(is_admin=True),
            {"user_team": _OTHER},
            RequesterOrg.OTHER,
            _OTHER,
            False,
            id="event-field-wins-over-users-info",
        ),
        pytest.param(
            _requester(team_id=None),
            {"user_team": _OTHER},
            RequesterOrg.OTHER,
            _OTHER,
            False,
            id="no-team-in-users-info-event-names-one",
        ),
        pytest.param(
            _requester(team_id=None), {}, RequesterOrg.UNKNOWN, None, False, id="unplaceable"
        ),
        pytest.param(
            _requester(is_stranger=True, is_admin=True),
            {},
            RequesterOrg.UNKNOWN,
            None,
            False,
            id="stranger-with-host-team-id",
        ),
        pytest.param(
            _requester(), {"user_team": _HOST}, RequesterOrg.HOST, None, False, id="event-agrees"
        ),
    ],
)
def test_requester_org_is_host_only_on_a_positive_users_info_verdict(
    requester: SlackRequester,
    event: dict[str, Any],
    org: RequesterOrg,
    home: str | None,
    admin: bool,
) -> None:
    standing = decide_requester_org(requester, event=event)
    assert (standing.org, standing.home_id, standing.is_admin) == (org, home, admin)


# ---------------------------------------------------------------------------
# Shared-channel detection
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _info_client(*responses: Any) -> MagicMock:
    client = MagicMock()
    client.conversations_info = AsyncMock(side_effect=list(responses))
    return client


async def test_is_ext_shared_true_means_shared_and_is_asked_once_per_ten_minutes() -> None:
    clock = _Clock()
    probe = SharedChannelProbe(clock)
    client = _info_client({"channel": {"is_ext_shared": True, "is_shared": True}})

    ask = lambda: probe.is_shared(  # noqa: E731
        cast(AsyncWebClient, client), team_id=_HOST, channel_id=_CHANNEL
    )
    assert await ask() is True
    assert await ask() is True, "the second turn is answered from the cache"
    assert client.conversations_info.await_count == 1

    clock.now = 601.0
    client.conversations_info.side_effect = [{"channel": {"is_ext_shared": False}}]
    assert await ask() is False, "after ten minutes it asks again"


async def test_is_shared_alone_is_not_shared_with_another_organisation() -> None:
    """`is_shared` also covers sharing between workspaces of one organisation."""
    probe = SharedChannelProbe()
    client = _info_client({"channel": {"is_shared": True, "is_ext_shared": False}})
    assert (
        await probe.is_shared(cast(AsyncWebClient, client), team_id=_HOST, channel_id=_CHANNEL)
        is False
    )


async def test_a_failed_lookup_is_none_and_is_not_cached() -> None:
    probe = SharedChannelProbe()
    client = _info_client(
        SlackApiError("down", {"ok": False, "error": "ratelimited"}),
        {"channel": {"is_ext_shared": True}},
    )
    web = cast(AsyncWebClient, client)
    assert await probe.is_shared(web, team_id=_HOST, channel_id=_CHANNEL) is None
    assert await probe.is_shared(web, team_id=_HOST, channel_id=_CHANNEL) is True


async def test_the_envelope_flag_answers_without_a_lookup() -> None:
    probe = SharedChannelProbe()
    client = _info_client()
    shared = await probe.is_shared(
        cast(AsyncWebClient, client), team_id=_HOST, channel_id=_CHANNEL, envelope_flag=True
    )
    assert shared is True
    client.conversations_info.assert_not_awaited()


# ---------------------------------------------------------------------------
# channel_shared / channel_unshared events
# ---------------------------------------------------------------------------


async def test_channel_shared_marks_the_registry_and_drops_the_cached_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T_EVENT_SHARED")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id="T_EVENT_SHARED")
    probe = SharedChannelProbe()
    client = _info_client(
        {"channel": {"is_ext_shared": False}}, {"channel": {"is_ext_shared": True}}
    )
    web = cast(AsyncWebClient, client)
    assert await probe.is_shared(web, team_id="T_EVENT_SHARED", channel_id=_CHANNEL) is False

    await handle_shared_channel_event(
        db_session_factory,
        probe,
        {"type": "channel_shared", "channel": _CHANNEL, "connected_team_id": _OTHER},
        team_id="T_EVENT_SHARED",
    )

    async with db_session_factory() as session:
        assert await is_channel_shared(
            session, tenant_id=tenant_id, platform="slack", channel_id=_CHANNEL
        )
    assert await probe.is_shared(web, team_id="T_EVENT_SHARED", channel_id=_CHANNEL) is True


async def test_channel_unshared_drops_the_cache_but_the_registry_stays(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T_EVENT_UNSHARED")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id="T_EVENT_UNSHARED")
    probe = SharedChannelProbe()
    shared_event = {"type": "channel_shared", "channel": _CHANNEL}
    await handle_shared_channel_event(
        db_session_factory, probe, shared_event, team_id="T_EVENT_UNSHARED"
    )
    client = _info_client(
        {"channel": {"is_ext_shared": True}}, {"channel": {"is_ext_shared": False}}
    )
    web = cast(AsyncWebClient, client)
    assert await probe.is_shared(web, team_id="T_EVENT_UNSHARED", channel_id=_CHANNEL) is True

    await handle_shared_channel_event(
        db_session_factory,
        probe,
        {"type": "channel_unshared", "channel": _CHANNEL},
        team_id="T_EVENT_UNSHARED",
    )

    assert await probe.is_shared(web, team_id="T_EVENT_UNSHARED", channel_id=_CHANNEL) is False
    async with db_session_factory() as session:
        assert await is_channel_shared(
            session, tenant_id=tenant_id, platform="slack", channel_id=_CHANNEL
        ), "the registry is sticky by design"


async def test_channel_shared_for_a_workspace_without_a_tenant_records_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await handle_shared_channel_event(
        db_session_factory,
        SharedChannelProbe(),
        {"type": "channel_shared", "channel": _CHANNEL},
        team_id="T_NO_TENANT",
    )


# ---------------------------------------------------------------------------
# A mention reaches admission with the requester's organisation and the channel's sharing
# ---------------------------------------------------------------------------


def _users_info_override(mock: Any, user: dict[str, Any]) -> None:
    import re

    pattern = re.compile(r"https://slack\.com/api/users\.info.*")
    for key in [
        k
        for k, v in mock._matches.items()  # pyright: ignore[reportUnknownMemberType, reportPrivateUsage]
        if getattr(v, "url_or_pattern", None) == pattern
    ]:
        del mock._matches[key]  # pyright: ignore[reportUnknownMemberType, reportPrivateUsage]
    mock.get(pattern, payload={"ok": True, "user": user}, repeat=True)  # pyright: ignore[reportUnknownMemberType]


@dataclasses.dataclass
class _Mention:
    admit_kwargs: dict[str, Any]
    admission: Admission | None
    account_role: Role | None
    ephemerals: list[dict[str, Any]]


async def _run_mention(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: Any,
    *,
    team_id: str,
    user_id: str,
    users_info: dict[str, Any] | None,
    event_extra: dict[str, Any] | None = None,
    is_ext_shared: bool | None = None,
    channel: str = _CHANNEL,
    ts: str = "9300000001.000001",
) -> _Mention:
    """Run one mention through `_orchestrate` with the real `admit`, capturing its call."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=team_id, signup_credit=Decimal("10")
    )
    async with db_session_factory() as s:
        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        await s.commit()
    if users_info is not None:
        _users_info_override(fake.mock, users_info)
    if is_ext_shared is not None:
        register_conversations_info(fake.mock, is_ext_shared=is_ext_shared)
    app, _ = make_orchestrate_app(db_session_factory)
    seen: list[tuple[dict[str, Any], Admission]] = []

    async def _spy(*args: Any, **kwargs: Any) -> Admission:
        admission = await admit(*args, **kwargs)
        seen.append((kwargs, admission))
        return admission

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": ts,
        "event_ts": ts,
        "channel": channel,
        "user": user_id,
        "text": "<@U_BOT> hello",
        **(event_extra or {}),
    }
    with (
        patch("daimon.adapters.slack.app.admit", new=_spy),
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock) as agent,
        patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock) as env,
        patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock) as create,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as run_turn,
        patch("daimon.adapters.slack.app.deliver_session_outputs", new_callable=AsyncMock),
    ):
        agent.return_value = "agent_test_id"
        env.return_value = "env_test_id"
        create.return_value = ma_session(
            id=f"sess-{user_id}",
            agent=ma_session_agent(id="agent_test_id"),
            environment_id="env_test_id",
        )
        run_turn.side_effect = _fake_run_turn
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=ts,
            web_client=fake.client,
            tenant_id=tenant_id,
        )
        while app._bg_tasks:  # pyright: ignore[reportPrivateUsage]
            await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0)

    async with db_session_factory() as s:
        account = await get_account(s, principal.account_id)
    ephemerals = [
        req.kwargs["json"]
        for (_, url), reqs in fake.mock.requests.items()
        if url == URL("https://slack.com/api/chat.postEphemeral")
        for req in reqs
    ]
    kwargs, admission = seen[0] if seen else ({}, None)
    return _Mention(kwargs, admission, account.role if account else None, ephemerals)


_EXTERNAL_ADMIN = {"id": _EXTERNAL, "name": "visitor", "team_id": _OTHER, "is_admin": True}


@pytest.mark.parametrize(
    "event_extra", [{}, {"user_team": _OTHER}], ids=["no-team-fields", "team-fields"]
)
async def test_an_external_mention_is_admitted_held_and_stored_as_a_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    event_extra: dict[str, Any],
) -> None:
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id=f"T_EXT_MENTION_{len(event_extra)}",
        user_id=_EXTERNAL,
        users_info=_EXTERNAL_ADMIN,
        event_extra=event_extra,
        is_ext_shared=True,
    )

    assert result.admit_kwargs["requester_org"] is RequesterOrg.OTHER
    assert result.admit_kwargs["requester_home_id"] == _OTHER
    assert result.admission is not None
    assert result.admission.hold is not None and result.admission.hold.client is True
    assert result.admission.hold.channel_id == _CHANNEL
    assert result.admission.shared_channel is True
    assert result.account_role is Role.USER, "stored as a user even though users.info says admin"
    assert result.ephemerals == [], "no refusal is posted: the external member is served"


async def test_an_external_mention_without_a_shared_signal_is_still_held(
    db_session_factory: async_sessionmaker[AsyncSession], fake_slack_web_client: Any
) -> None:
    """Slack's lookup failing (no signal) still holds the turn: the requester is a client."""
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id="T_EXT_NO_SIGNAL",
        user_id=_EXTERNAL,
        users_info=_EXTERNAL_ADMIN,
        is_ext_shared=None,
    )
    assert result.admit_kwargs["channel_shared"] is None
    assert result.admission is not None
    assert result.admission.hold is not None and result.admission.hold.client is True
    assert result.admission.shared_channel is True, "a verified external requester reveals sharing"


async def test_an_internal_member_in_a_shared_channel_is_held_but_not_a_client(
    db_session_factory: async_sessionmaker[AsyncSession], fake_slack_web_client: Any
) -> None:
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id="T_INT_SHARED",
        user_id="U_INTERNAL",
        users_info=None,
        is_ext_shared=True,
    )
    assert result.admit_kwargs["requester_org"] is RequesterOrg.HOST
    assert result.admit_kwargs["channel_shared"] is True
    assert result.admission is not None
    assert result.admission.hold is not None and result.admission.hold.client is False
    assert result.admission.shared_channel is True


async def test_an_internal_member_in_an_ordinary_channel_is_not_held(
    db_session_factory: async_sessionmaker[AsyncSession], fake_slack_web_client: Any
) -> None:
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id="T_INT_PLAIN",
        user_id="U_INTERNAL",
        users_info=None,
        is_ext_shared=False,
    )
    assert result.admit_kwargs["channel_shared"] is False
    assert result.admission is not None
    assert result.admission.hold is None


async def test_the_envelope_flag_marks_the_channel_shared_without_a_lookup(
    db_session_factory: async_sessionmaker[AsyncSession], fake_slack_web_client: Any
) -> None:
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id="T_INT_ENVELOPE",
        user_id="U_INTERNAL",
        users_info=None,
        event_extra={ENVELOPE_EXT_SHARED_KEY: True},
    )
    assert result.admit_kwargs["channel_shared"] is True
    assert result.admission is not None and result.admission.shared_channel is True
    assert not any(
        url.path == "/api/conversations.info" for (_, url) in fake_slack_web_client.mock.requests
    )


async def test_a_failed_requester_lookup_still_refuses_the_mention(
    db_session_factory: async_sessionmaker[AsyncSession], fake_slack_web_client: Any
) -> None:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id="T_LOOKUP_FAIL")
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T_LOOKUP_FAIL")
    app, _ = make_orchestrate_app(db_session_factory)
    event = {
        "type": "app_mention",
        "ts": "9.1",
        "event_ts": "9.1",
        "channel": _CHANNEL,
        "user": _EXTERNAL,
        "text": "<@U_BOT> hi",
    }
    with (
        patch(
            "daimon.adapters.slack.app.resolve_requester", new_callable=AsyncMock, return_value=None
        ),
        patch("daimon.adapters.slack.app.admit", new_callable=AsyncMock) as admitted,
    ):
        await app._run_thread_turn(  # pyright: ignore[reportPrivateUsage]
            event,
            channel=_CHANNEL,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
            thread_id="9.1",
            team_id="T_LOOKUP_FAIL",
        )
    admitted.assert_not_awaited()
    posts = [
        req.kwargs["json"]
        for (_, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.postMessage")
        for req in reqs
    ]
    assert any("Couldn't check your workspace role" in shown_text(p) for p in posts)


# ---------------------------------------------------------------------------
# Clicks and submissions
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _FakeSocketClient:
    sent_responses: list[SocketModeResponse] = dataclasses.field(default_factory=list)

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.sent_responses.append(response)

    async def close(self) -> None:
        return None


def _make_app() -> SlackApp:
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


def _slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.chat_postMessage = AsyncMock()
    client.auth_test = AsyncMock(return_value={"user_id": "U_BOT", "team": _HOST_NAME})
    return client


def _click_payload(
    action_id: str, *, user_id: str = _EXTERNAL, value: str | None = None
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_id": action_id}
    if value is not None:
        action["value"] = value
    return {
        "type": "block_actions",
        "team": {"id": _HOST},
        "user": {"id": user_id, "team_id": _OTHER},
        "channel": {"id": _CHANNEL},
        "container": {"message_ts": "1700000001.000100", "channel_id": _CHANNEL},
        "message": {"ts": "1700000001.000100", "thread_ts": "1700000000.000001"},
        "trigger_id": "TRIGGER_TEST",
        "actions": [action],
    }


async def _deliver(app: SlackApp, payload: dict[str, Any], client: MagicMock) -> None:
    request = SocketModeRequest(type="interactive", envelope_id="env_ext", payload=payload)
    with patch("daimon.adapters.slack.app.resolve_web_client", new=AsyncMock(return_value=client)):
        await app.on_request(_FakeSocketClient(), request)  # type: ignore[arg-type]
        pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    ("action_id", "handler"),
    [
        pytest.param("routine_action:run", "handle_routine_action", id="routines"),
        pytest.param("billing_topup", "handle_topup_select", id="billing"),
        pytest.param("agent_setup__open", "handle_agent_setup_action", id="agent-setup"),
        pytest.param("privacy_export", "handle_privacy_block_action", id="privacy"),
        pytest.param(ASK_HUMAN_ACTION_ID, "handle_ask_human_click", id="support-escalation"),
        pytest.param("hand_over_thread", "handle_hand_over_click", id="hand-over"),
    ],
)
async def test_an_external_click_outside_the_allowlist_gets_the_host_copy_and_runs_nothing(
    action_id: str, handler: str
) -> None:
    app = _make_app()
    client = _slack_client()
    with patch(f"daimon.adapters.slack.app.{handler}", new_callable=AsyncMock) as handled:
        await _deliver(app, _click_payload(action_id), client)

    handled.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()
    sent = client.chat_postEphemeral.await_args.kwargs
    assert sent["user"] == _EXTERNAL
    assert shown_text(sent) == _HOST_COPY


async def test_an_external_provider_action_click_is_refused() -> None:
    app = _make_app()
    client = _slack_client()
    with patch.object(app._provider_actions, "handle_click", new_callable=AsyncMock) as handled:  # pyright: ignore[reportPrivateUsage]
        await _deliver(app, _click_payload("dpa:browser_sign_in"), client)
    handled.assert_not_awaited()
    assert shown_text(client.chat_postEphemeral.await_args.kwargs) == _HOST_COPY


async def test_the_refusal_falls_back_to_a_thread_reply_naming_the_person() -> None:
    app = _make_app()
    client = _slack_client()
    client.chat_postEphemeral.side_effect = SlackApiError(
        "no", {"ok": False, "error": "user_not_in_channel"}
    )
    await _deliver(app, _click_payload("billing_topup"), client)
    sent = client.chat_postMessage.await_args.kwargs
    assert sent["thread_ts"] == "1700000000.000001"
    assert shown_text(sent) == f"<@{_EXTERNAL}> {_HOST_COPY}"


async def test_a_host_member_click_is_not_refused() -> None:
    app = _make_app()
    client = _slack_client()
    payload = _click_payload("billing_topup")
    payload["user"] = {"id": "U_HOST", "team_id": _HOST}
    with patch("daimon.adapters.slack.app.handle_topup_select", new_callable=AsyncMock) as handled:
        await _deliver(app, payload, client)
    handled.assert_awaited_once()
    client.chat_postEphemeral.assert_not_awaited()


@pytest.mark.parametrize(
    ("action_id", "handler"),
    [
        pytest.param(FEEDBACK_VOTE_UP, "handle_feedback_vote", id="feedback-vote"),
        pytest.param(
            FEEDBACK_DETAILS_ACTION_ID, "handle_feedback_details_click", id="feedback-details"
        ),
    ],
)
async def test_an_external_feedback_click_reaches_its_requester_bound_handler(
    action_id: str, handler: str
) -> None:
    app = _make_app()
    with patch(f"daimon.adapters.slack.app.{handler}", new_callable=AsyncMock) as handled:
        await _deliver(app, _click_payload(action_id), _slack_client())
    handled.assert_awaited_once()


async def test_an_external_confirmation_click_reaches_the_card_handler() -> None:
    app = _make_app()
    with patch.object(app._confirmations, "handle_click", new_callable=AsyncMock) as handled:  # pyright: ignore[reportPrivateUsage]
        await _deliver(app, _click_payload("dcf:token:approve"), _slack_client())
    handled.assert_awaited_once()


def test_the_allowlists_are_exactly_the_agreed_controls() -> None:
    for allowed in (
        "cancel_turn",
        "dcf:t:approve",
        "dcf:t:deny",
        "feedback_vote:down",
        FEEDBACK_DETAILS_ACTION_ID,
    ):
        assert external_action_allowed(allowed), allowed
    for refused in (
        "routines_refresh",
        "billing_topup",
        "dpa:x",
        ASK_HUMAN_ACTION_ID,
        "agent_setup__x",
        "",
    ):
        assert not external_action_allowed(refused), refused
    assert external_submission_allowed(FEEDBACK_TEXT_CALLBACK_ID)
    for callback in (
        "routines__create",
        "privacy_delete",
        "credential_request__env",
        "support",
        "",
    ):
        assert not external_submission_allowed(callback), callback
    assert host_only_notice(None) == "That's only for people at the workspace that added Daimon."


async def test_an_external_stop_on_their_own_turn_works_and_on_someone_elses_is_not_yours() -> None:
    app = _make_app()
    mine, theirs = asyncio.Event(), asyncio.Event()
    app._register_cancel("1.1", mine, _EXTERNAL, team_id=_HOST, channel=_CHANNEL)  # pyright: ignore[reportPrivateUsage]
    app._register_cancel("2.2", theirs, "U_HOST", team_id=_HOST, channel=_CHANNEL)  # pyright: ignore[reportPrivateUsage]
    client = _slack_client()

    own = _click_payload("cancel_turn")
    own["container"]["message_ts"] = "1.1"
    await _deliver(app, own, client)
    assert mine.is_set()
    client.chat_postEphemeral.assert_not_awaited()

    other = _click_payload("cancel_turn")
    other["container"]["message_ts"] = "2.2"
    # An admin of their own workspace is not an admin of the host's.
    client.users_info = AsyncMock(
        return_value={"user": {"id": _EXTERNAL, "team_id": _OTHER, "is_admin": True}}
    )
    await _deliver(app, other, client)
    assert not theirs.is_set()
    assert "person who asked" in shown_text(client.chat_postEphemeral.await_args.kwargs)


def _external_click(action_id: str, user: str) -> dict[str, Any]:
    return {
        "actions": [{"action_id": action_id}],
        "user": {"id": user, "team_id": _OTHER},
        "team": {"id": _HOST},
    }


def _card_prompt() -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id=_EXTERNAL, now=datetime.now(UTC))
    return prompt.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(minutes=10)})


async def _post_card(
    cards: SlackConfirmationCards, client: MagicMock
) -> tuple[asyncio.Task[Any], str]:
    client.chat_postMessage = AsyncMock(return_value={"ts": "1700.1"})
    client.chat_update = AsyncMock()
    waiting = asyncio.create_task(
        cards.hook(
            cast(AsyncWebClient, client), team_id=_HOST, channel=_CHANNEL, thread_ts="1699.9"
        )(_card_prompt())
    )
    while not client.chat_postMessage.await_count:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    kwargs = client.chat_postMessage.await_args.kwargs
    (actions,) = [b for b in kwargs["attachments"][0]["blocks"] if b["type"] == "actions"]
    return waiting, actions["elements"][0]["action_id"]


async def test_an_external_requester_approves_their_own_card_but_not_someone_elses() -> None:
    cards = SlackConfirmationCards()
    client = _slack_client()
    waiting, approve_id = await _post_card(cards, client)

    await cards.handle_click(_external_click(approve_id, "U_OTHER_EXTERNAL"))
    assert not waiting.done()
    assert shown_text(client.chat_postEphemeral.await_args.kwargs) == NOT_YOURS_MESSAGE.format(
        requester=f"<@{_EXTERNAL}>"
    )

    await cards.handle_click(_external_click(approve_id, _EXTERNAL))
    result = await asyncio.wait_for(waiting, timeout=1)
    assert isinstance(result, ApprovedConfirmation) and result.answer == "approved"


async def test_an_external_requesters_deny_works() -> None:
    cards = SlackConfirmationCards()
    client = _slack_client()
    waiting, approve_id = await _post_card(cards, client)
    await cards.handle_click(_external_click(approve_id.replace(":approve", ":deny"), _EXTERNAL))
    assert await asyncio.wait_for(waiting, timeout=1) == "denied"


async def test_an_external_member_never_gets_always_allow_or_someone_elses_details() -> None:
    cards = SlackConfirmationCards()
    client = _slack_client()
    waiting, approve_id = await _post_card(cards, client)
    token = approve_id.split(":")[1]

    await cards.handle_click(_external_click(f"dcf:{token}:always", _EXTERNAL))
    assert not waiting.done()
    assert "only for people at" in shown_text(client.chat_postEphemeral.await_args.kwargs)
    assert "admin" not in shown_text(client.chat_postEphemeral.await_args.kwargs).lower()

    await cards.handle_click(_external_click(f"dcf:{token}:details", "U_OTHER_EXTERNAL"))
    assert shown_text(client.chat_postEphemeral.await_args.kwargs) == NOT_YOURS_MESSAGE.format(
        requester=f"<@{_EXTERNAL}>"
    )
    waiting.cancel()


def _submission(
    callback_id: str, *, user_team: str, metadata: dict[str, Any] | None = None
) -> SocketModeRequest:
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_ext_view",
        payload={
            "type": "view_submission",
            "team": {"id": _HOST},
            "user": {"id": _EXTERNAL, "team_id": user_team},
            "view": {
                "callback_id": callback_id,
                "id": "V_TEST",
                "private_metadata": json.dumps(metadata or {}),
                "state": {
                    "values": {"feedback_text_block": {"feedback_text_input": {"value": "wrong"}}}
                },
            },
        },
    )


@pytest.mark.parametrize(
    "callback_id",
    [
        "routines__create",
        "agent_setup__new_agent",
        "privacy_delete",
        "credential_request__env",
        "support_escalation",
    ],
)
async def test_an_external_view_submission_other_than_feedback_is_refused_with_the_host_copy(
    callback_id: str,
) -> None:
    app = _make_app()
    app._workspace_names[_HOST] = _HOST_NAME  # pyright: ignore[reportPrivateUsage]
    socket = _FakeSocketClient()
    await app.on_request(socket, _submission(callback_id, user_team=_OTHER))  # type: ignore[arg-type]
    payload = socket.sent_responses[0].payload or {}
    assert payload["response_action"] == "update"
    assert payload["view"]["blocks"][0]["text"]["text"] == _HOST_COPY


async def test_an_external_feedback_form_from_their_own_answer_is_accepted() -> None:
    app = _make_app()
    socket = _FakeSocketClient()
    meta = {
        "channel_id": _CHANNEL,
        "message_ts": "1.1",
        "thread_ts": "1.0",
        "requested_by": _EXTERNAL,
    }
    with patch(
        "daimon.adapters.slack.app.run_feedback_text_submission", new_callable=AsyncMock
    ) as run:
        await app.on_request(
            socket, _submission(FEEDBACK_TEXT_CALLBACK_ID, user_team=_OTHER, metadata=meta)
        )  # type: ignore[arg-type]
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    assert socket.sent_responses[0].payload is None
    run.assert_awaited_once()
    assert run.await_args.kwargs["decision"].external is True


async def test_an_external_feedback_form_from_someone_elses_answer_goes_nowhere() -> None:
    app = _make_app()
    socket = _FakeSocketClient()
    meta = {
        "channel_id": _CHANNEL,
        "message_ts": "1.1",
        "thread_ts": "1.0",
        "requested_by": "U_HOST",
    }
    with patch(
        "daimon.adapters.slack.app.run_feedback_text_submission", new_callable=AsyncMock
    ) as run:
        await app.on_request(
            socket, _submission(FEEDBACK_TEXT_CALLBACK_ID, user_team=_OTHER, metadata=meta)
        )  # type: ignore[arg-type]
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Events routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["channel_shared", "channel_unshared"])
async def test_shared_channel_events_are_routed_to_the_registry_handler(event_type: str) -> None:
    app = _make_app()
    request = SocketModeRequest(
        type="events_api",
        envelope_id="env_shared_event",
        payload={"team_id": _HOST, "event": {"type": event_type, "channel": _CHANNEL}},
    )
    with patch(
        "daimon.adapters.slack.app.handle_shared_channel_event", new_callable=AsyncMock
    ) as handled:
        await app.on_request(_FakeSocketClient(), request)  # type: ignore[arg-type]
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    handled.assert_awaited_once()
    assert handled.await_args.args[2]["type"] == event_type


async def test_member_joined_channel_reaches_the_no_op_hook() -> None:
    app = _make_app()
    request = SocketModeRequest(
        type="events_api",
        envelope_id="env_joined",
        payload={
            "team_id": _HOST,
            "event": {"type": "member_joined_channel", "channel": _CHANNEL, "user": "U_BOT"},
        },
    )
    with patch.object(app, "_on_member_joined_channel") as hook:
        await app.on_request(_FakeSocketClient(), request)  # type: ignore[arg-type]
    hook.assert_called_once()


# ---------------------------------------------------------------------------
# Copy for members of another workspace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["balance_depleted", "cap_exceeded", "invoker_not_allowed", "channel_budget_exceeded"]
)
def test_admission_refusals_never_send_an_external_member_to_commands_or_admins(
    reason: Any,
) -> None:
    from daimon.adapters.slack.runtime import admission_refusal_message

    settings = MagicMock()
    settings.slack.bot_display_name = "daimon"
    text = admission_refusal_message(reason, settings, foreign=True)
    for word in ("/billing", "/agent-setup", "an admin"):
        assert word not in text, (reason, text)


def test_an_external_member_is_told_daimon_has_no_credit_yet_when_it_never_had_any() -> None:
    from daimon.adapters.slack.runtime import admission_refusal_message

    settings = MagicMock()
    settings.slack.bot_display_name = "daimon"
    text = admission_refusal_message(
        "balance_depleted", settings, foreign=True, cause="no_credit_yet"
    )
    assert text == (
        "Daimon has no credit yet.\n\nAsk someone in the workspace that added Daimon to add some."
    )
    member = admission_refusal_message("balance_depleted", settings, cause="no_credit_yet")
    assert member.startswith("Your team has no Daimon credit yet.")


def test_setup_notices_for_an_external_member_name_the_host_workspace_not_a_command() -> None:
    from daimon.adapters.slack.errors import not_set_up_notice, setup_out_of_date_notice

    for text in (not_set_up_notice(foreign=True), setup_out_of_date_notice(foreign=True)):
        assert "/agent-setup" not in text and "admin" not in text
        assert "workspace that added Daimon" in text
    assert "/agent-setup" in not_set_up_notice()
    assert "/agent-setup" in setup_out_of_date_notice()


async def test_the_answer_of_an_external_members_turn_offers_no_ask_a_human_button(
    fake_slack_web_client: Any,
) -> None:
    from daimon.adapters.slack.lifecycle import SlackTurnLifecycle

    lifecycle = SlackTurnLifecycle(
        client=fake_slack_web_client.client,
        channel=_CHANNEL,
        thread_ts="1.0",
        cancel=asyncio.Event(),
        author_id=_EXTERNAL,
        requester_foreign=True,
        ask_human=True,
        agent_name="agent",
        model_id="m",
        register=lambda *_: None,
        deregister=lambda *_: None,
    )
    block = lifecycle._feedback_block()  # pyright: ignore[reportPrivateUsage]
    assert [e["value"] for e in block["elements"]] == [_EXTERNAL, _EXTERNAL]
    assert len(block["elements"]) == 2, "no Ask a human button for an external member"


@pytest.mark.parametrize("cause", ["channel_pin", "provider_not_allowed", "agent_backend"])
def test_an_external_members_provider_refusal_keeps_its_cause_and_fix_together(cause: Any) -> None:
    from daimon.adapters.slack.runtime import SLACK_FOREIGN_REFUSAL_NOUNS, admission_refusal_message
    from daimon.core.turn.notices import admission_refusal_text

    settings = MagicMock()
    settings.slack.bot_display_name = "daimon"
    text = admission_refusal_message(
        "backend_unsupported", settings, foreign=True, cause=cause, provider="openai"
    )
    expected = admission_refusal_text(
        "backend_unsupported",
        SLACK_FOREIGN_REFUSAL_NOUNS,
        bot_name="daimon",
        cause=cause,
        provider="openai",
    )
    assert text == expected
    assert "an admin" not in text and "workspace that added Daimon" in text
