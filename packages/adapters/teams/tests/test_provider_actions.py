"""Typed provider controls using the Teams SDK and schema-validating sender."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.teams import provider_actions as actions
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
from microsoft_teams.api import AdaptiveCardInvokeActivity
from microsoft_teams.apps import ActivityContext
from mux import profiles
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.contracts.ids import ResourceRef, Scope

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    FakeSender,
    make_card_action,
)

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
        ProviderActionRequester("teams", "thread", AAD_OBJECT_ID),
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


async def pending(host: actions.TeamsProviderActions) -> tuple[str, actions.PendingAction]:
    async with asyncio.timeout(5):
        while not host.pending or next(iter(host.pending.values())).message_id is None:
            await asyncio.sleep(0)
    return next(iter(host.pending.items()))


def click(
    token: str,
    index: int,
    *,
    user: str = AAD_OBJECT_ID,
    conversation: str = CONVERSATION_ID,
    service: str = SERVICE_URL,
) -> ActivityContext[AdaptiveCardInvokeActivity]:
    payload = make_card_action(actions.VERB, str(index), user=user, token=token)
    payload["conversation"] = {"id": conversation, "conversationType": "personal"}
    payload["serviceUrl"] = service
    return cast(
        ActivityContext[AdaptiveCardInvokeActivity],
        SimpleNamespace(activity=AdaptiveCardInvokeActivity.model_validate(payload)),
    )


def labels(sender: FakeSender, index: int) -> list[str]:
    body = sender.activities[index].model_dump(mode="json", by_alias=True, exclude_none=True)
    card = body["attachments"][0]["content"]
    return [
        action["title"]
        for block in card["body"]
        if block["type"] == "ActionSet"
        for action in block["actions"]
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
async def test_origin_exact_offered_response_and_schema_valid_card_retirement(index: int) -> None:
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    token, _ = await pending(host)
    assert labels(sender, 0) == ["Approve", "Deny", "Cancel"]
    body = sender.activities[0].model_dump_json(by_alias=True)
    assert (
        "https://example.org" in body
        and "private-action-id" not in body
        and "never-render-this-secret" not in body
        and "native_fixture_choice" not in body
    )
    await host.on_action(click(token, index))
    assert await waiting is (surface.approve, surface.deny, surface.cancel)[index]
    assert not host.pending and labels(sender, -1) == [] and sender.activities[-1].id == "m-1"


@pytest.mark.parametrize("submit", [False, True])
async def test_authentication_only_offered_completion_or_cancel(submit: bool) -> None:
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    value = prompt(authentication=True, submit=submit)
    surface = value.surface
    assert isinstance(surface, BrowserAuthentication)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    token, _ = await pending(host)
    assert labels(sender, 0) == (["Sign-in complete", "Cancel"] if submit else ["Cancel"])
    await host.on_action(click(token, 0))
    assert await waiting is (surface.submit if submit else surface.cancel)


@pytest.mark.parametrize("wrong", ["user", "conversation", "service"])
async def test_wrong_requester_or_destination_cannot_answer(wrong: str) -> None:
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    token, _ = await pending(host)
    kwargs = {"user": OTHER_AAD_OBJECT_ID} if wrong == "user" else {wrong: "wrong"}
    refused = await host.on_action(click(token, 0, **kwargs))
    assert refused.value == "Only the requester can answer this request." and not waiting.done()
    await host.on_action(click(token, 1, user=AAD_OBJECT_ID.upper()))
    assert await waiting is surface.deny


async def test_duplicate_cannot_replace_first_response() -> None:
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    token, _ = await pending(host)
    await host.on_action(click(token, 1))
    await host.on_action(click(token, 0))
    assert await waiting is surface.deny
    late = await host.on_action(click(token, 0))
    assert late.value == "This request is no longer pending." and len(sender.sent) == 2


@pytest.mark.parametrize("mode", ["stop", "click-expiry", "timeout", "host-expiry"])
async def test_stop_or_expiry_retire_without_permission(mode: str) -> None:
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    value = prompt()
    if mode == "timeout":
        value = replace(
            value, context=replace(value.context, expires_at=NOW + timedelta(milliseconds=100))
        )
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
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
            await host.on_action(click(token, 0))
        assert await asyncio.wait_for(waiting, 5) is None
    body = sender.activities[-1].model_dump_json(by_alias=True)
    assert (
        ("Stopped" if mode == "stop" else "Expired") in body
        and labels(sender, -1) == []
        and not host.pending
    )


@pytest.mark.parametrize("invalid", ["platform", "thread", "expired"])
async def test_unbound_or_expired_posts_nothing(invalid: str) -> None:
    value = prompt()
    context = value.context
    if invalid == "expired":
        context = replace(context, expires_at=NOW)
    else:
        context = replace(
            context,
            requester=replace(
                context.requester,
                **({"platform": "slack"} if invalid == "platform" else {"thread_id": "other"}),
            ),
        )
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    assert (
        await host.hook(
            conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread"
        )(replace(value, context=context))
        is None
    )
    assert not sender.sent and not host.pending


@pytest.mark.parametrize("still_ours", [True, False])
async def test_a_closed_group_chat_gets_no_question_card(still_ours: bool) -> None:
    """A group chat's question waits on the turn's cached roster check: a closed chat sees nothing."""
    checks: list[bool] = []

    async def roster_open(fresh: bool) -> bool:
        checks.append(fresh)
        return still_ours

    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    hook = host.hook(
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        thread_id="thread",
        roster_open=roster_open,
    )
    waiting = asyncio.ensure_future(hook(prompt()))
    if still_ours:
        await pending(host)
        waiting.cancel()
    answer = await asyncio.gather(waiting, return_exceptions=True)
    assert checks == [False], "one cached check: a join evicts the roster"
    assert bool(sender.sent) is still_ours, "only a chat still ours sees the card"
    if not still_ours:
        assert answer == [None] and not host.pending, "unanswered, as on a timeout"


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
async def test_bound_core_consumes_exact_offered_response_and_caches_card(index: int) -> None:
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
    sender = FakeSender()
    host = actions.TeamsProviderActions(sender)
    bound = ProviderActionApproval(
        value.context.requester,
        value.context.expires_at,
        asyncio.Event(),
        hook=host.hook(
            conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread"
        ),
        now=Clock.now,
    ).bind(SCOPE, SESSION)
    waiting = asyncio.create_task(bound.request(event, value.action, surface, root_turn_id="root"))
    token, _ = await pending(host)
    await host.on_action(click(token, index))
    offered = (surface.approve, surface.deny, surface.cancel)[index]
    assert await waiting is offered
    assert await bound.request(event, value.action, surface, root_turn_id="root") is offered
    assert len(sender.sent) == 2


@pytest.mark.parametrize("mode", ["send-error", "send-timeout", "edit-error", "edit-timeout"])
async def test_transport_failure_never_fabricates_response_or_changes_selected_identity(
    mode: str,
) -> None:
    sender = FakeSender(
        fail_on={0} if mode == "send-error" else {1} if mode == "edit-error" else set(),
        timeout_on={0} if mode == "send-timeout" else {1} if mode == "edit-timeout" else set(),
    )
    host = actions.TeamsProviderActions(sender)
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    if mode.startswith("send"):
        assert await waiting is None and not host.pending
    else:
        token, _ = await pending(host)
        await host.on_action(click(token, 1))
        assert await waiting is surface.deny and not host.pending


async def test_http_service_registers_provider_action_with_real_sdk_router() -> None:
    from daimon.adapters.teams.http_service import create_teams_http_service
    from microsoft_teams.api import ActivityBase, AdaptiveCardInvokeResponse
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .conftest import TeamsApiFake, build_teams_client, build_teams_runtime, teams_settings

    settings = teams_settings()
    service = create_teams_http_service(
        settings=settings,
        runtime=build_teams_runtime(async_sessionmaker(), teams=settings),
        client=build_teams_client(TeamsApiFake()),
    )
    sender = FakeSender()
    host = service.turns.provider_actions
    host.sender = sender
    value = prompt()
    surface = value.surface
    assert isinstance(surface, BrowserOriginAccess)
    waiting = asyncio.ensure_future(
        host.hook(conversation_id=CONVERSATION_ID, service_url=SERVICE_URL, thread_id="thread")(
            value
        )
    )
    token, _ = await pending(host)
    ctx = click(token, 0)
    handlers = service.teams_app.router.select_handlers(ctx.activity)
    assert len(handlers) == 1, "the actual SDK router must select the registered provider handler"
    result = cast(
        AdaptiveCardInvokeResponse, await handlers[0](cast(ActivityContext[ActivityBase], ctx))
    )
    assert result.value == "Response recorded." and await waiting is surface.approve


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
