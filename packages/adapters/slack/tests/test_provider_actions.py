"""Typed Slack controls through real Web API request serialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Protocol, cast

import pytest
from daimon.adapters.slack import provider_actions as actions
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.provider_actions import (
    BrowserAuthentication,
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionApproval,
    ProviderActionContext,
    ProviderActionPrompt,
    ProviderActionRequester,
)
from mux import profiles
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.contracts.ids import ResourceRef, Scope

from .conftest import FakeSlackWebClient

NOW = datetime(2026, 10, 10, tzinfo=UTC)
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="turn"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id="tenant",
    account_id="account",
)


def native(choice: str) -> NativeActionResponse:
    # Opaque provider-owned fixture bodies; the adapter must never inspect them.
    return NativeActionResponse.from_payload({"native_fixture_choice": choice})


def prompt(*, authentication: bool = False, submit: bool = False) -> ProviderActionPrompt:
    action = RequiredAction(
        id="private-action-id",
        kind="native",
        native_type="computer_use_approval_request",
        payload={
            "password": "never-render-this-secret",
            "instructions": "Approve authentication",
            "turn_id": "root",
            "request": {
                "type": "browser_authentication" if authentication else "browser_origin_access",
                "origin": "https://example.org",
            },
        },
    )
    context = ProviderActionContext(
        SCOPE,
        SESSION,
        "root",
        ProviderActionRequester("slack", "thread", "U_TEST"),
        NOW + timedelta(minutes=10),
    )
    surface = (
        BrowserAuthentication(native("submit") if submit else None, native("cancel"))
        if authentication
        else BrowserOriginAccess(
            "https://example.org", native("approve"), native("deny"), native("cancel")
        )
    )
    return ProviderActionPrompt(action, context, surface)


class Clock:
    now_at = NOW

    @classmethod
    def now(cls, *args: object) -> datetime:
        return cls.now_at


@pytest.fixture(autouse=True)
def frozen_action_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock.now_at = NOW
    monkeypatch.setattr(actions, "datetime", Clock)


@pytest.fixture(autouse=True)
async def drain_action_tasks(fake_slack_web_client: FakeSlackWebClient) -> AsyncIterator[None]:
    # Drain hooks while their real SDK transport fake is still active, even on failed assertions.
    existing = set(asyncio.all_tasks())
    yield
    pending = set(asyncio.all_tasks()) - existing - {asyncio.current_task()}
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class RecordedCall(Protocol):
    kwargs: dict[str, Any]


def bodies(fake: FakeSlackWebClient, method: str) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], cast(RecordedCall, call).kwargs["json"])
        for (_, url), calls in fake.mock.requests.items()
        if str(url).endswith("/" + method)
        for call in calls
    ]


async def pending(host: actions.SlackProviderActions) -> tuple[str, actions.PendingAction]:
    async with asyncio.timeout(5):
        while not host.pending or next(iter(host.pending.values())).ts is None:
            await asyncio.sleep(0)
    return next(iter(host.pending.items()))


def click(
    token: str, index: int, *, user: str = "U_TEST", channel: str = "C_TEST", thread: str = "thread"
) -> dict[str, Any]:
    return {
        "actions": [{"action_id": f"dpa:{token}:{index}"}],
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"thread_ts": thread},
    }


def labels(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        element["text"]["text"]
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_origin_returns_offered_identity_and_retires_sdk_card(
    fake_slack_web_client: FakeSlackWebClient, index: int
) -> None:
    fake = fake_slack_web_client
    host = actions.SlackProviderActions()
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    posted = bodies(fake, "chat.postMessage")[0]
    assert labels(posted["blocks"]) == ["Approve", "Deny", "Cancel"]
    assert posted["mrkdwn"] is False and posted["parse"] == "none"
    assert (
        "private-action-id" not in str(posted)
        and "never-render-this-secret" not in str(posted)
        and "native_fixture_choice" not in str(posted)
    )
    await host.handle_click(click(token, index))
    assert await waiting is (surface.approve, surface.deny, surface.cancel)[index]
    assert not host.pending and labels(bodies(fake, "chat.update")[-1]["blocks"]) == []


@pytest.mark.parametrize("submit", [False, True])
async def test_authentication_only_offered_completion_or_cancel(
    fake_slack_web_client: FakeSlackWebClient, submit: bool
) -> None:
    fake = fake_slack_web_client
    host = actions.SlackProviderActions()
    value = prompt(authentication=True, submit=submit)
    surface = value.surface
    assert isinstance(surface, BrowserAuthentication)
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    assert labels(bodies(fake, "chat.postMessage")[0]["blocks"]) == (
        ["Sign-in complete", "Cancel"] if submit else ["Cancel"]
    )
    await host.handle_click(click(token, 0))
    assert await waiting is (surface.submit if submit else surface.cancel)


@pytest.mark.parametrize("wrong", ["user", "channel", "thread"])
async def test_requester_destination_and_thread_are_bound(
    fake_slack_web_client: FakeSlackWebClient, wrong: str
) -> None:
    fake = fake_slack_web_client
    host = actions.SlackProviderActions()
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    await host.handle_click(click(token, 0, **{wrong: "wrong"}))
    assert not waiting.done() and bodies(fake, "chat.postEphemeral")
    await host.handle_click(click(token, 1))
    assert await waiting is surface.deny


async def test_duplicate_cannot_replace_first_answer(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    fake = fake_slack_web_client
    host = actions.SlackProviderActions()
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    await host.handle_click(click(token, 1))
    await host.handle_click(click(token, 0))
    assert await waiting is surface.deny
    await host.handle_click(click(token, 0))
    assert not host.pending


@pytest.mark.parametrize("mode", ["stop", "click-expiry", "timeout", "host-expiry"])
async def test_stop_or_expiry_retires_without_response(
    fake_slack_web_client: FakeSlackWebClient, mode: str
) -> None:
    fake = fake_slack_web_client
    host = actions.SlackProviderActions()
    value = prompt()
    if mode == "timeout":
        value = replace(
            value, context=replace(value.context, expires_at=NOW + timedelta(milliseconds=100))
        )
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    if mode in {"stop", "host-expiry"}:
        if mode == "host-expiry":
            Clock.now_at = value.context.expires_at
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    else:
        if mode == "click-expiry":
            Clock.now_at = value.context.expires_at
            await host.handle_click(click(token, 0))
        assert await asyncio.wait_for(waiting, 5) is None
    final = bodies(fake, "chat.update")[-1]
    assert labels(final["blocks"]) == [] and ("Stopped" if mode == "stop" else "Expired") in str(
        final
    )
    assert not host.pending


@pytest.mark.parametrize("invalid", ["platform", "thread", "expired"])
async def test_unbound_prompt_posts_nothing(
    fake_slack_web_client: FakeSlackWebClient, invalid: str
) -> None:
    value = prompt()
    context = value.context
    if invalid == "expired":
        context = replace(context, expires_at=NOW)
    else:
        context = replace(
            context,
            requester=replace(
                context.requester,
                **({"platform": "teams"} if invalid == "platform" else {"thread_id": "other"}),
            ),
        )
    host = actions.SlackProviderActions()
    assert (
        await host.hook(fake_slack_web_client.client, channel="C_TEST", thread_ts="thread")(
            replace(value, context=context)
        )
        is None
    )
    assert not bodies(fake_slack_web_client, "chat.postMessage") and not host.pending


@pytest.mark.parametrize("provider", [None, "missing", "anthropic", "openai", "gemini"])
def test_default_path_attaches_no_new_callback(provider: str | None) -> None:
    ref = SESSION.model_copy(update={"provider": provider}) if provider is not None else None
    prepared = cast(
        PreparedTurn,
        SimpleNamespace() if provider == "missing" else SimpleNamespace(session_ref=ref),
    )
    called = 0

    def factory():
        nonlocal called
        called += 1

        async def hook(value: ProviderActionPrompt) -> NativeActionResponse | None:
            return None

        return hook

    assert bool(actions.provider_action_options(prepared, factory)) == (
        provider in {"openai", "gemini"}
    )
    assert called == int(provider in {"openai", "gemini"})


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_bound_core_uses_sdk_card_and_caches_exact_response(
    fake_slack_web_client: FakeSlackWebClient, index: int
) -> None:
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    event = Event(
        id="event",
        session_id=SESSION.id,
        sequence=1,
        turn_id="root",
        type="session.requires_action",
        observed_at=NOW,
        authority="record",
        payload=RequiresActionPayload(actions=(value.action,)).model_dump(mode="json"),
        native=NativeProvenance(provider="openai", api_revision="offline"),
    )
    host = actions.SlackProviderActions()
    bound = ProviderActionApproval(
        value.context.requester,
        value.context.expires_at,
        asyncio.Event(),
        hook=host.hook(fake_slack_web_client.client, channel="C_TEST", thread_ts="thread"),
        now=Clock.now,
    ).bind(SCOPE, SESSION)
    waiting = asyncio.create_task(bound.request(event, value.action, surface, root_turn_id="root"))
    token, _ = await pending(host)
    await host.handle_click(click(token, index))
    offered = (surface.approve, surface.deny, surface.cancel)[index]
    assert await waiting is offered
    assert await bound.request(event, value.action, surface, root_turn_id="root") is offered
    assert len(bodies(fake_slack_web_client, "chat.postMessage")) == 1


async def test_sdk_send_failure_refuses_and_clears_pending(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    fake = fake_slack_web_client
    fake.mock.clear()
    fake.mock.post(  # pyright: ignore[reportUnknownMemberType]  # SDK fixture URL pattern type is Unknown
        "https://slack.com/api/chat.postMessage",
        payload={"ok": False, "error": "channel_not_found"},
    )  # pyright: ignore[reportUnknownMemberType]
    host = actions.SlackProviderActions()
    assert await host.hook(fake.client, channel="C_TEST", thread_ts="thread")(prompt()) is None
    assert not host.pending and not bodies(fake, "chat.update")


async def test_sdk_edit_failure_preserves_exact_selected_identity(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    fake = fake_slack_web_client
    fake.mock.clear()
    fake.mock.post("https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "posted"})  # pyright: ignore[reportUnknownMemberType]
    fake.mock.post(  # pyright: ignore[reportUnknownMemberType]  # SDK fixture URL pattern type is Unknown
        "https://slack.com/api/chat.update", payload={"ok": False, "error": "message_not_found"}
    )  # pyright: ignore[reportUnknownMemberType]
    host = actions.SlackProviderActions()
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(fake.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, _ = await pending(host)
    await host.handle_click(click(token, 1))
    assert await waiting is surface.deny and not host.pending


@pytest.mark.parametrize("external", [False, True])
async def test_socket_dispatch_acks_before_provider_click_and_retains_connect_gate(
    fake_slack_web_client: FakeSlackWebClient, external: bool
) -> None:
    from daimon.adapters.slack.app import SlackApp
    from daimon.adapters.slack.runtime import SlackRuntime
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from sqlalchemy.ext.asyncio import async_sessionmaker

    host = actions.SlackProviderActions()
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(fake_slack_web_client.client, channel="C_TEST", thread_ts="thread")(value)
    )
    token, posted = await pending(host)
    app = SlackApp.__new__(SlackApp)
    app.runtime = cast(SlackRuntime, SimpleNamespace(sessionmaker=async_sessionmaker()))
    app._provider_actions = host  # pyright: ignore[reportPrivateUsage]
    app._bg_tasks = set()  # pyright: ignore[reportPrivateUsage]
    acknowledged: list[str] = []

    class Socket:
        async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
            assert not posted.answer.done(), "Socket ack must precede granting the action"
            acknowledged.append(response.envelope_id)

    payload = click(token, 0)
    payload["type"] = "block_actions"
    payload["team"] = {"id": "T_HOST"}
    payload["user"]["team_id"] = "T_OTHER" if external else "T_HOST"
    await app.on_request(
        cast(AsyncBaseSocketModeClient, Socket()),
        SocketModeRequest(type="interactive", envelope_id="envelope", payload=payload),
    )
    assert acknowledged == ["envelope"]
    tasks = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if tasks:
        await asyncio.gather(*tasks)
    if external:
        assert not waiting.done()
        await host.handle_click(click(token, 1))
        assert await waiting is surface.deny
    else:
        assert await waiting is surface.approve


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_callback_registration_follows_declared_capability(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    ref = SESSION.model_copy(update={"provider": provider})
    prepared = cast(PreparedTurn, SimpleNamespace(session_ref=ref))

    async def hook(value: ProviderActionPrompt) -> NativeActionResponse | None:
        return None

    called = 0

    def factory():
        nonlocal called
        called += 1
        return hook

    for emits_legacy in (True, False):
        monkeypatch.setattr(
            profiles,
            "PROFILES",
            {
                pid: profile.model_copy(update={"emits_legacy_events": emits_legacy})
                if profile.provider == provider
                else profile
                for pid, profile in profiles.PROFILES.items()
            },
        )
        assert actions.provider_action_options(prepared, factory) == (
            {} if emits_legacy else {"provider_action": hook}
        )
    assert called == 1
