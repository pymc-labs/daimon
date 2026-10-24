"""Interactive payloads and slash commands that do not prove their sender is a host member.

Slack's `user.team_id` is optional and slash payloads carry no sender workspace, so
anything outside the own-turn allowlist is checked with `users.info` before it runs.
A lookup that fails or cannot place the person counts as external.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.slack.admin import SlackRequester
from daimon.adapters.slack.requester_org import decide_requester_org
from daimon.core.turn_hold import RequesterOrg
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.request import SocketModeRequest

from .test_shared_channels import (
    _CHANNEL,
    _HOST,
    _HOST_COPY,
    _HOST_NAME,
    _OTHER,
    _click_payload,
    _deliver,
    _FakeSocketClient,
    _make_app,
    _run_mention,
    _slack_client,
)


@pytest.fixture(autouse=True)
def _host_member_verified() -> None:
    """Override the conftest shortcut: these tests exercise the real check."""


def _requester(team_id: str | None, *, is_stranger: bool = False) -> SlackRequester:
    return SlackRequester(
        is_admin=False,
        display_name=None,
        team_id=team_id,
        is_stranger=is_stranger,
        host_team_id=_HOST,
    )


_VERDICTS = [
    pytest.param(_requester(_HOST), True, id="host-member"),
    pytest.param(_requester(_OTHER), False, id="other-workspace"),
    pytest.param(_requester(None), False, id="unplaceable"),
    pytest.param(_requester(_HOST, is_stranger=True), False, id="stranger"),
    pytest.param(None, False, id="lookup-failed"),
]


def _lookup(verdict: SlackRequester | None) -> Any:
    return patch(
        "daimon.adapters.slack.app.resolve_requester",
        new=AsyncMock(return_value=verdict),
    )


def _unproven_click(action_id: str) -> dict[str, Any]:
    payload = _click_payload(action_id, user_id="U_UNPLACED")
    del payload["user"]["team_id"]
    return payload


@pytest.mark.parametrize(("verdict", "allowed"), _VERDICTS)
async def test_a_click_that_does_not_prove_membership_is_checked_with_users_info(
    verdict: SlackRequester | None, allowed: bool
) -> None:
    app = _make_app()
    client = _slack_client()
    with (
        _lookup(verdict),
        patch("daimon.adapters.slack.app.handle_agent_setup_action", new_callable=AsyncMock) as ran,
    ):
        await _deliver(app, _unproven_click("agent_setup__open"), client)
    assert ran.await_count == int(allowed)
    assert client.chat_postEphemeral.await_count == int(not allowed)
    if not allowed:
        assert client.chat_postEphemeral.await_args.kwargs["text"].startswith(
            "That's only for people at"
        )


async def test_a_click_whose_payload_proves_host_membership_needs_no_lookup() -> None:
    app = _make_app()
    payload = _click_payload("agent_setup__open", user_id="U_HOST")
    payload["user"]["team_id"] = _HOST
    with (
        _lookup(None) as lookup,
        patch("daimon.adapters.slack.app.handle_agent_setup_action", new_callable=AsyncMock) as ran,
    ):
        await _deliver(app, payload, _slack_client())
    ran.assert_awaited_once()
    lookup.assert_not_awaited()


async def test_own_turn_controls_still_work_without_proof_or_lookup() -> None:
    app = _make_app()
    mine = asyncio.Event()
    app._register_cancel("1.1", mine, "U_UNPLACED", team_id=_HOST, channel=_CHANNEL)  # pyright: ignore[reportPrivateUsage]
    payload = _unproven_click("cancel_turn")
    payload["container"]["message_ts"] = "1.1"
    with _lookup(None) as lookup:
        await _deliver(app, payload, _slack_client())
    assert mine.is_set()
    lookup.assert_not_awaited()


def _submission(callback_id: str, *, user_team: str | None) -> SocketModeRequest:
    user: dict[str, Any] = {"id": "U_UNPLACED"}
    if user_team is not None:
        user["team_id"] = user_team
    return SocketModeRequest(
        type="interactive",
        envelope_id="env_unproven_view",
        payload={
            "type": "view_submission",
            "team": {"id": _HOST},
            "user": user,
            "view": {
                "callback_id": callback_id,
                "id": "V_TEST",
                "private_metadata": json.dumps({"channel_id": _CHANNEL, "routine_id": "r"}),
                "state": {"values": {}},
            },
        },
    )


@pytest.mark.parametrize(("verdict", "allowed"), _VERDICTS)
async def test_a_submission_that_does_not_prove_membership_is_checked_before_it_runs(
    verdict: SlackRequester | None, allowed: bool
) -> None:
    app = _make_app()
    app._workspace_names[_HOST] = _HOST_NAME  # pyright: ignore[reportPrivateUsage]
    socket = _FakeSocketClient()
    with (
        _lookup(verdict),
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=AsyncMock(return_value=_slack_client()),
        ),
        patch(
            "daimon.adapters.slack.app.run_routines_delete_submission", new_callable=AsyncMock
        ) as ran,
    ):
        await app.on_request(socket, _submission("routines__delete_confirm", user_team=None))  # type: ignore[arg-type]
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    assert ran.await_count == int(allowed)
    if not allowed:
        view = (socket.sent_responses[0].payload or {})["view"]
        assert view["blocks"][0]["text"]["text"] == _HOST_COPY


async def test_an_unplaced_user_cannot_create_an_agent_from_an_open_form() -> None:
    app = _make_app()
    socket = _FakeSocketClient()
    with (
        _lookup(None),
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=AsyncMock(return_value=_slack_client()),
        ),
        patch(
            "daimon.adapters.slack.app.run_new_agent_submission", new_callable=AsyncMock
        ) as create,
    ):
        await app.on_request(socket, _submission("agent_setup__new_agent", user_team=None))  # type: ignore[arg-type]
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]
    create.assert_not_awaited()
    assert (socket.sent_responses[0].payload or {})["response_action"] == "update"


def _slash(command: str, text: str = "") -> SocketModeRequest:
    return SocketModeRequest(
        type="slash_commands",
        envelope_id="env_slash",
        payload={
            "command": command,
            "text": text,
            "team_id": _HOST,
            "user_id": "U_UNPLACED",
            "channel_id": _CHANNEL,
        },
    )


async def _run_slash(app: Any, request: SocketModeRequest, client: Any) -> None:
    with patch("daimon.adapters.slack.app.resolve_web_client", new=AsyncMock(return_value=client)):
        await app.on_request(_FakeSocketClient(), request)
        await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("/billing", "handle_billing_command"),
        ("/routines", "handle_routines_command"),
        ("/privacy", "handle_privacy_command"),
        ("/memory", "handle_memory_command"),
        ("/here", "handle_here_command"),
        ("/dm-me", "handle_dm_command"),
        ("/agent-setup", "handle_agent_setup_command"),
    ],
)
@pytest.mark.parametrize(("verdict", "allowed"), _VERDICTS)
async def test_a_slash_command_needs_a_verified_host_member(
    command: str, handler: str, verdict: SlackRequester | None, allowed: bool
) -> None:
    app = _make_app()
    client = _slack_client()
    with (
        _lookup(verdict),
        patch(f"daimon.adapters.slack.app.{handler}", new_callable=AsyncMock) as ran,
    ):
        await _run_slash(app, _slash(command), client)
    assert ran.await_count == int(allowed)
    assert client.chat_postEphemeral.await_count == int(not allowed)
    if not allowed:
        kwargs = client.chat_postEphemeral.await_args.kwargs
        assert kwargs["user"] == "U_UNPLACED" and kwargs["channel"] == _CHANNEL
        assert kwargs["text"].startswith("That's only for people at")


async def test_help_stays_open_to_everyone_without_a_lookup() -> None:
    app = _make_app()
    with (
        _lookup(None) as lookup,
        patch("daimon.adapters.slack.app.handle_help_command", new_callable=AsyncMock) as ran,
    ):
        await _run_slash(app, _slash("/help"), _slack_client())
    ran.assert_awaited_once()
    lookup.assert_not_awaited()


async def test_a_slash_lookup_that_raises_counts_as_external() -> None:
    app = _make_app()
    client = _slack_client()
    boom = AsyncMock(side_effect=SlackApiError("down", {"ok": False, "error": "ratelimited"}))
    with (
        patch("daimon.adapters.slack.app.resolve_requester", new=boom),
        patch("daimon.adapters.slack.app.handle_billing_command", new_callable=AsyncMock) as ran,
    ):
        await _run_slash(app, _slash("/billing"), client)
    ran.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()


# --- an empty or unreadable event team hint -------------------------------------


@pytest.mark.parametrize("hint", ["", None])
def test_an_empty_event_team_hint_does_not_make_a_host_member_foreign(hint: str | None) -> None:
    standing = decide_requester_org(
        _requester(_HOST), event={"user_team": hint, "user_team_id": hint}
    )
    assert (standing.org, standing.home_id) == (RequesterOrg.HOST, None)


def test_an_unreadable_event_team_hint_is_unknown_and_never_other() -> None:
    standing = decide_requester_org(_requester(_HOST), event={"user_team": 12345})
    assert (standing.org, standing.home_id) == (RequesterOrg.UNKNOWN, None)


async def test_a_mention_with_an_empty_user_team_writes_no_foreign_marker(
    db_session_factory: Any, fake_slack_web_client: Any
) -> None:
    result = await _run_mention(
        db_session_factory,
        fake_slack_web_client,
        team_id="T_EMPTY_HINT",
        user_id="U_INTERNAL",
        users_info=None,
        event_extra={"user_team": ""},
        is_ext_shared=False,
    )
    assert result.admit_kwargs["requester_org"] is RequesterOrg.HOST
    assert result.admit_kwargs["requester_home_id"] is None
    assert result.admission is not None and result.admission.hold is None
    assert result.admission.shared_channel is False


# --- the lookup never holds an acknowledgement ------------------------------------

_BUDGET_S = 0.05
_SLOW_S = 0.3


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.adapters.slack import app as app_module

    monkeypatch.setattr(app_module, "_HOST_CHECK_TIMEOUT_S", _BUDGET_S)


def _slow_lookup(verdict: SlackRequester | None, calls: list[str]) -> Any:
    async def lookup(*args: Any, **kwargs: Any) -> SlackRequester | None:
        calls.append(kwargs["user_id"])
        await asyncio.sleep(_SLOW_S)
        return verdict

    return patch("daimon.adapters.slack.app.resolve_requester", new=lookup)


async def _settle(app: Any) -> None:
    await asyncio.sleep(_SLOW_S + 0.1)
    await asyncio.gather(*list(app._bg_tasks), return_exceptions=True)  # pyright: ignore[reportPrivateUsage]


async def _submit(app: Any, socket: _FakeSocketClient) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    with patch(
        "daimon.adapters.slack.app.resolve_web_client", new=AsyncMock(return_value=_slack_client())
    ):
        await app.on_request(socket, _submission("routines__delete_confirm", user_team=None))
    return loop.time() - started


@pytest.mark.parametrize(
    "verdict", [_requester(_HOST), _requester(_OTHER)], ids=["member", "other"]
)
async def test_a_slow_lookup_still_acks_within_budget_and_runs_nothing(
    short_budget: None, verdict: SlackRequester
) -> None:
    from daimon.adapters.slack.external_access import TRY_AGAIN_NOTICE

    app = _make_app()
    socket = _FakeSocketClient()
    calls: list[str] = []
    with (
        _slow_lookup(verdict, calls),
        patch(
            "daimon.adapters.slack.app.run_routines_delete_submission", new_callable=AsyncMock
        ) as ran,
    ):
        took = await _submit(app, socket)
        await _settle(app)
    assert took < _SLOW_S, "the ack must not wait for Slack"
    view = (socket.sent_responses[0].payload or {})["view"]
    assert view["blocks"][0]["text"]["text"] == TRY_AGAIN_NOTICE
    ran.assert_not_awaited()


async def test_after_a_timeout_a_member_retry_succeeds_from_the_finished_lookup() -> None:
    app = _make_app()
    calls: list[str] = []
    with (
        _slow_lookup(_requester(_HOST), calls),
        patch(
            "daimon.adapters.slack.app.run_routines_delete_submission", new_callable=AsyncMock
        ) as ran,
        patch("daimon.adapters.slack.app._HOST_CHECK_TIMEOUT_S", _BUDGET_S),
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=AsyncMock(return_value=_slack_client()),
        ),
    ):
        await _submit(app, _FakeSocketClient())
        await _settle(app)
        socket = _FakeSocketClient()
        took = await _submit(app, socket)
        await _settle(app)
    assert calls == ["U_UNPLACED"], "the retry reuses the verdict the first lookup finished"
    assert took < _BUDGET_S
    assert socket.sent_responses[0].payload is None
    ran.assert_awaited_once()


async def test_a_non_member_is_refused_after_a_timeout_and_is_never_cached() -> None:
    app = _make_app()
    calls: list[str] = []
    with (
        _slow_lookup(_requester(_OTHER), calls),
        patch(
            "daimon.adapters.slack.app.run_routines_delete_submission", new_callable=AsyncMock
        ) as ran,
        patch("daimon.adapters.slack.app._HOST_CHECK_TIMEOUT_S", 1.0),
    ):
        socket = _FakeSocketClient()
        await _submit(app, socket)
    view = (socket.sent_responses[0].payload or {})["view"]
    assert view["blocks"][0]["text"]["text"].startswith("That's only for people at")
    ran.assert_not_awaited()
    assert app._host_members == {}  # pyright: ignore[reportPrivateUsage]


async def test_a_failed_lookup_is_not_cached() -> None:
    app = _make_app()
    with (
        _lookup(None),
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=AsyncMock(return_value=_slack_client()),
        ),
    ):
        assert await app._host_check(_HOST, "U_UNPLACED") == "not_member"  # pyright: ignore[reportPrivateUsage]
    assert app._host_members == {}  # pyright: ignore[reportPrivateUsage]


async def test_a_cached_member_skips_the_lookup_until_the_verdict_expires() -> None:
    app = _make_app()
    now = [0.0]
    app._host_member_clock = lambda: now[0]  # pyright: ignore[reportPrivateUsage]
    calls: list[str] = []

    async def fast(*args: Any, **kwargs: Any) -> SlackRequester:
        calls.append(kwargs["user_id"])
        return _requester(_HOST)

    with (
        patch("daimon.adapters.slack.app.resolve_requester", new=fast),
        patch(
            "daimon.adapters.slack.app.resolve_web_client",
            new=AsyncMock(return_value=_slack_client()),
        ),
    ):
        check = app._host_check  # pyright: ignore[reportPrivateUsage]
        assert await check(_HOST, "U1") == "member"
        now[0] = 599.0
        assert await check(_HOST, "U1") == "member"
        assert calls == ["U1"]
        now[0] = 601.0
        assert await check(_HOST, "U1") == "member"
        assert calls == ["U1", "U1"]


async def test_a_slow_lookup_gets_a_try_again_for_a_slash_command_and_opens_nothing(
    short_budget: None,
) -> None:
    from daimon.adapters.slack.external_access import TRY_AGAIN_NOTICE

    app = _make_app()
    client = _slack_client()
    calls: list[str] = []
    with (
        _slow_lookup(_requester(_HOST), calls),
        patch(
            "daimon.adapters.slack.app.handle_agent_setup_command", new_callable=AsyncMock
        ) as ran,
    ):
        await _run_slash(app, _slash("/agent-setup"), client)
        await _settle(app)
    ran.assert_not_awaited()
    assert client.chat_postEphemeral.await_args.kwargs["text"] == TRY_AGAIN_NOTICE


async def test_a_slow_click_lookup_gets_a_try_again() -> None:
    from daimon.adapters.slack.external_access import TRY_AGAIN_NOTICE

    app = _make_app()
    client = _slack_client()
    calls: list[str] = []
    with (
        _slow_lookup(_requester(_HOST), calls),
        patch("daimon.adapters.slack.app.handle_agent_setup_action", new_callable=AsyncMock) as ran,
        patch("daimon.adapters.slack.app._HOST_CHECK_TIMEOUT_S", _BUDGET_S),
    ):
        await _deliver(app, _unproven_click("agent_setup__open"), client)
        await _settle(app)
    ran.assert_not_awaited()
    assert client.chat_postEphemeral.await_args.kwargs["text"] == TRY_AGAIN_NOTICE
