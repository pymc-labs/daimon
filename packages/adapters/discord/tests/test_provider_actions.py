"""Typed provider controls select only offered responses and retire their views."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import discord
import mux.profiles as profiles
import pytest
from daimon.adapters.discord import provider_actions as actions
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
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.contracts.ids import Provider, ResourceRef, Scope

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
        ProviderActionRequester("discord", "thread", "111"),
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


class Response:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.deferred = 0

    async def send_message(self, content: str, **kwargs: object) -> None:
        assert kwargs["ephemeral"] is True
        mentions = cast(discord.AllowedMentions, kwargs["allowed_mentions"])
        assert mentions.to_dict() == {"parse": []}
        self.messages.append(content)

    async def defer(self) -> None:
        self.deferred += 1


class Interaction:
    def __init__(self, user: int = 111, channel: int = 42) -> None:
        self.user = SimpleNamespace(id=user)
        self.channel_id = channel
        self.response = Response()


class Message:
    id = 77

    def __init__(self) -> None:
        self.views: list[discord.ui.LayoutView] = []

    async def edit(self, **kwargs: object) -> None:
        self.views.append(cast(discord.ui.LayoutView, kwargs["view"]))


class Channel:
    def __init__(self) -> None:
        self.message = Message()
        self.view: actions.ProviderActionView | None = None
        self.posted = asyncio.Event()
        self.sends = 0

    async def send(self, **kwargs: object) -> discord.Message:
        self.sends += 1
        self.view = cast(actions.ProviderActionView, kwargs["view"])
        # The real Discord component serializer runs over the actual UI objects.
        serialized = str(self.view.to_components())
        assert "private-action-id" not in serialized
        assert "never-render-this-secret" not in serialized
        assert "native_fixture_choice" not in serialized
        self.posted.set()
        return cast(discord.Message, self.message)

    async def pending_view(self) -> actions.ProviderActionView:
        await asyncio.wait_for(self.posted.wait(), timeout=5)
        assert self.view is not None
        return self.view


def labels(view: discord.ui.LayoutView) -> list[str | None]:
    return [
        button.label for button in view.walk_children() if isinstance(button, discord.ui.Button)
    ]


async def test_sdk_components_never_contain_raw_action_or_response_payloads() -> None:
    answer: asyncio.Future[NativeActionResponse | None] = asyncio.get_running_loop().create_future()
    view = actions.ProviderActionView(prompt(), channel_id=42, answer=answer)
    serialized = str(view.to_components())
    assert "https://example.org" in serialized
    assert "private-action-id" not in serialized
    assert "never-render-this-secret" not in serialized
    assert "native_fixture_choice" not in serialized
    view.stop()
    answer.cancel()


@pytest.mark.parametrize("choice", ["approve", "deny", "cancel"])
async def test_origin_buttons_return_exact_offered_response_and_retire(choice: str) -> None:
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    offered = {"approve": surface.approve, "deny": surface.deny, "cancel": surface.cancel}[choice]
    channel = Channel()
    hook = actions.discord_provider_action_hook(
        cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
    )
    task = asyncio.ensure_future(hook(value))
    view = await channel.pending_view()
    assert labels(view) == ["Approve", "Deny", "Cancel"]
    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    await buttons[["approve", "deny", "cancel"].index(choice)].callback(
        cast(discord.Interaction, Interaction())
    )
    assert await task is offered
    assert channel.message.views and labels(channel.message.views[-1]) == []
    assert view.is_finished()


@pytest.mark.parametrize(
    ("authentication", "submit", "expected"),
    [(True, False, ["Cancel"]), (True, True, ["Sign-in complete", "Cancel"])],
)
async def test_authentication_has_no_approval_or_unoffered_completion_button(
    authentication: bool, submit: bool, expected: list[str]
) -> None:
    value = prompt(authentication=authentication, submit=submit)
    surface = value.surface
    assert isinstance(surface, BrowserAuthentication)
    channel = Channel()
    task = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    view = await channel.pending_view()
    assert labels(view) == expected
    button = next(item for item in view.walk_children() if isinstance(item, discord.ui.Button))
    await button.callback(cast(discord.Interaction, Interaction()))
    assert await task is (surface.submit if submit else surface.cancel)
    assert labels(channel.message.views[-1]) == []


@pytest.mark.parametrize(("user", "channel_id"), [(999, 42), (111, 999)])
async def test_wrong_requester_or_destination_cannot_answer(user: int, channel_id: int) -> None:
    value = prompt()
    channel = Channel()
    task = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    view = await channel.pending_view()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    wrong = Interaction(user, channel_id)
    await view.settle(cast(discord.Interaction, wrong), surface.approve, "Approve")
    assert not task.done() and wrong.response.messages
    await view.settle(cast(discord.Interaction, Interaction()), surface.deny, "Deny")
    assert await task is surface.deny


async def test_duplicate_click_cannot_replace_first_response() -> None:
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    channel = Channel()
    task = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    view = await channel.pending_view()
    await view.settle(cast(discord.Interaction, Interaction()), surface.deny, "Deny")
    duplicate = Interaction()
    await view.settle(cast(discord.Interaction, duplicate), surface.approve, "Approve")
    assert duplicate.response.messages and await task is surface.deny


@pytest.mark.parametrize("stopped", [True, False])
async def test_stop_and_expiry_retire_controls_without_native_permission(stopped: bool) -> None:
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    channel = Channel()
    task = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    view = await channel.pending_view()
    if stopped:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        Clock.now_at = value.context.expires_at
        await view.settle(cast(discord.Interaction, Interaction()), surface.approve, "Approve")
        assert await task is None
    assert view.is_finished() and labels(channel.message.views[-1]) == []


async def test_unanswered_timeout_retires_as_expired() -> None:
    value = prompt()
    value = replace(
        value, context=replace(value.context, expires_at=NOW + timedelta(milliseconds=100))
    )
    channel = Channel()
    waiting = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    view = await channel.pending_view()
    assert await asyncio.wait_for(waiting, timeout=5) is None
    assert view.is_finished() and labels(channel.message.views[-1]) == []
    assert "Expired" in str(channel.message.views[-1].to_components())


async def test_outer_expiry_cancellation_is_expired_instead_of_stopped() -> None:
    value = prompt()
    channel = Channel()
    waiting = asyncio.ensure_future(
        actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        )(value)
    )
    await channel.pending_view()
    Clock.now_at = value.context.expires_at
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert labels(channel.message.views[-1]) == []
    assert "Expired" in str(channel.message.views[-1].to_components())


@pytest.mark.parametrize("invalid", ["platform", "thread", "expired"])
async def test_unbound_or_expired_prompt_posts_nothing(invalid: str) -> None:
    value = prompt()
    context = value.context
    if invalid == "expired":
        context = replace(context, expires_at=NOW)
    else:
        requester = replace(
            context.requester,
            **({"platform": "slack"} if invalid == "platform" else {"thread_id": "other"}),
        )
        context = replace(context, requester=requester)
    channel = Channel()
    result = await actions.discord_provider_action_hook(
        cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
    )(replace(value, context=context))
    assert result is None and channel.sends == 0


@pytest.mark.parametrize("provider", [None, "anthropic", "openai", "gemini"])
def test_only_foreign_prepared_sessions_attach_action_callback(provider: str | None) -> None:
    ref = SESSION.model_copy(update={"provider": provider}) if provider is not None else None
    prepared = cast(PreparedTurn, SimpleNamespace(session_ref=ref))
    called = 0

    def factory():
        nonlocal called
        called += 1

        async def hook(value: ProviderActionPrompt) -> NativeActionResponse | None:
            return None

        return hook

    options = actions.provider_action_options(prepared, factory)
    assert called == int(provider in {"openai", "gemini"})
    assert bool(options) == bool(called)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("emits_legacy_events", [False, True])
def test_action_callback_follows_declared_profile_rendering_policy(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, emits_legacy_events: bool
) -> None:
    monkeypatch.setattr(
        profiles,
        "PROFILES",
        {
            key: profile.model_copy(update={"emits_legacy_events": emits_legacy_events})
            for key, profile in profiles.PROFILES.items()
        },
    )
    prepared = cast(
        PreparedTurn, SimpleNamespace(session_ref=SESSION.model_copy(update={"provider": provider}))
    )
    calls = 0

    async def hook(value: ProviderActionPrompt) -> NativeActionResponse | None:
        return None

    def factory():
        nonlocal calls
        calls += 1
        return hook

    options = actions.provider_action_options(prepared, factory)
    assert calls == int(not emits_legacy_events)
    assert options == ({} if emits_legacy_events else {"provider_action": hook})


@pytest.mark.parametrize("choice", ["approve", "deny", "cancel"])
async def test_real_bound_core_action_uses_discord_surface_and_exact_response(choice: str) -> None:
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
    channel = Channel()
    approval = ProviderActionApproval(
        value.context.requester,
        value.context.expires_at,
        asyncio.Event(),
        hook=actions.discord_provider_action_hook(
            cast(discord.abc.Messageable, channel), thread_id="thread", channel_id=42
        ),
        now=Clock.now,
    )
    bound = approval.bind(SCOPE, SESSION)
    waiting = asyncio.create_task(bound.request(event, value.action, surface, root_turn_id="root"))
    view = await channel.pending_view()
    offered = {"approve": surface.approve, "deny": surface.deny, "cancel": surface.cancel}[choice]
    await view.settle(cast(discord.Interaction, Interaction()), offered, choice.capitalize())
    assert await waiting is offered
    assert await bound.request(event, value.action, surface, root_turn_id="root") is offered
    assert channel.sends == 1, "the host cache cannot post a second card for the same action"
    assert labels(channel.message.views[-1]) == []
