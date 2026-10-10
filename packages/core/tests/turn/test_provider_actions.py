"""Provider cards select native responses only within an admitted root."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.turn.provider_actions import (
    BoundProviderActions,
    BrowserAuthentication,
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionApproval,
    ProviderActionHook,
    ProviderActionPrompt,
    ProviderActionRequester,
    no_provider_action_surface,
)
from mux.contracts.events import Event, NativeProvenance, RequiredAction, RequiresActionPayload
from mux.contracts.ids import ResourceRef, Scope
from mux.errors import ScopeViolation, UnsupportedCapability
from pydantic import JsonValue

NOW = datetime(2026, 10, 10, tzinfo=UTC)
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="turn"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)
REQUESTER = ProviderActionRequester("discord", "thread", "authorized-user")


def native(decision: str) -> NativeActionResponse:
    return NativeActionResponse.from_payload(
        {
            "type": "agent.session.input.computer_use_approval_request_result",
            "request_id": "action",
            "response": {"type": "browser_origin_access", "decision": decision},
        }
    )


def origin() -> BrowserOriginAccess:
    return BrowserOriginAccess(
        "https://example.org", native("approve"), native("deny"), native("cancel")
    )


def action_event(*, authentication: bool = False) -> tuple[RequiredAction, Event]:
    action = RequiredAction(
        id="action",
        kind="native",
        native_type="computer_use_approval_request",
        payload={
            "type": "computer_use_approval_request",
            "request_id": "action",
            "turn_id": "root",
            "request": {
                "type": "browser_authentication" if authentication else "browser_origin_access",
                "origin": "https://example.org",
                "reason": "Authentication approval cancel",
            },
        },
    )
    event = Event(
        id="event",
        session_id=SESSION.id,
        sequence=1,
        type="session.requires_action",
        turn_id="root",
        observed_at=NOW,
        authority="record",
        payload=RequiresActionPayload(actions=(action,)).model_dump(mode="json"),
        native=NativeProvenance(
            provider="openai", api_revision="offline", event_type="requires_action"
        ),
    )
    return action, event


def bound(
    hook: ProviderActionHook | None = None,
    *,
    cancel: asyncio.Event | None = None,
    expiry: datetime = NOW + timedelta(minutes=10),
) -> BoundProviderActions:
    approval = ProviderActionApproval(
        REQUESTER,
        expiry,
        cancel or asyncio.Event(),
        now=lambda: NOW,
        hook=hook if hook is not None else no_provider_action_surface,
    )
    return approval.bind(SCOPE, SESSION)


@pytest.mark.parametrize("choice", ["approve", "deny", "cancel"])
async def test_origin_returns_exact_native_body_with_trusted_context(choice: str) -> None:
    surface = origin()
    action, event = action_event()
    offered = {"approve": surface.approve, "deny": surface.deny, "cancel": surface.cancel}[choice]
    prompts: list[ProviderActionPrompt] = []

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        prompts.append(prompt)
        return offered

    result = await bound(hook).request(event, action, surface, root_turn_id="root")
    assert result is offered
    assert result is not None and result.payload == native(choice).payload
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.action is action and prompt.surface is surface
    assert prompt.context.scope is SCOPE and prompt.context.session is SESSION
    assert prompt.context.requester is REQUESTER and prompt.context.root_turn_id == "root"
    assert prompt.context.expires_at == NOW + timedelta(minutes=10)
    assert "Authentication approval cancel" not in repr(prompt)


async def test_authentication_is_cancel_only_until_provider_verifies_completion() -> None:
    action, event = action_event(authentication=True)
    cancel = NativeActionResponse.from_payload(
        {"request_id": "action", "response": {"type": "browser_authentication", "action": "cancel"}}
    )
    surface = BrowserAuthentication(None, cancel)

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        assert isinstance(prompt.surface, BrowserAuthentication)
        assert prompt.surface.submit is None and not hasattr(prompt.surface, "approve")
        return prompt.surface.cancel

    assert await bound(hook).request(event, action, surface, root_turn_id="root") is cancel


async def test_missing_surface_and_expired_or_stopped_turn_never_call_hook() -> None:
    action, event = action_event()
    assert await bound().request(event, action, origin(), root_turn_id="root") is None

    async def forbidden(prompt: ProviderActionPrompt) -> NativeActionResponse:
        raise AssertionError("expired/stopped cards must not open")

    cancel = asyncio.Event()
    cancel.set()
    for handler in (bound(forbidden, cancel=cancel), bound(forbidden, expiry=NOW)):
        assert await handler.request(event, action, origin(), root_turn_id="root") is None


@pytest.mark.parametrize("stop", ["cancel", "expiry", "outer_cancel"])
async def test_waiting_card_is_cancelled_and_drained(stop: str) -> None:
    action, event = action_event()
    started, retired, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        started.set()
        try:
            await asyncio.Future[None]()
        finally:
            retired.set()
        raise AssertionError("never answers")

    handler = bound(
        hook,
        cancel=cancel,
        expiry=NOW + timedelta(seconds=0.02) if stop == "expiry" else NOW + timedelta(minutes=10),
    )
    work = asyncio.create_task(handler.request(event, action, origin(), root_turn_id="root"))
    await started.wait()
    if stop == "cancel":
        cancel.set()
    elif stop == "outer_cancel":
        work.cancel()
    if stop == "outer_cancel":
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        assert await work is None
    assert retired.is_set()


async def test_stop_wins_simultaneous_approve_and_no_cached_approval_after_stop() -> None:
    action, event, surface, cancel = *action_event(), origin(), asyncio.Event()

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        cancel.set()
        return surface.approve

    assert (
        await bound(hook, cancel=cancel).request(event, action, surface, root_turn_id="root")
        is None
    )


async def test_duplicate_root_action_opens_one_card_and_changed_choices_refuse() -> None:
    action, event, surface = *action_event(), origin()
    calls = 0

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return surface.approve

    handler = bound(hook)
    results = await asyncio.gather(
        *(handler.request(event, action, surface, root_turn_id="root") for _ in range(2))
    )
    assert calls == 1 and all(result is surface.approve for result in results)
    with pytest.raises(ScopeViolation, match="changed"):
        await handler.request(
            event, action, replace(surface, approve=native("new")), root_turn_id="root"
        )
    handler.approval.cancel.set()
    assert await handler.request(event, action, surface, root_turn_id="root") is None


@pytest.mark.parametrize(
    "change", ["session", "provider", "root", "child", "preview", "blank_root", "absent"]
)
async def test_foreign_or_non_authoritative_actions_refuse_before_display(change: str) -> None:
    action, event = action_event()
    all_updates: dict[str, dict[str, object]] = {
        "session": {"session_id": "foreign"},
        "provider": {"native": event.native.model_copy(update={"provider": "gemini"})},
        "root": {"turn_id": "old"},
        "child": {"thread_id": "child"},
        "preview": {"authority": "preview"},
        "blank_root": {},
        "absent": {"payload": RequiresActionPayload(actions=()).model_dump(mode="json")},
    }
    updates = all_updates[change]

    async def forbidden(prompt: ProviderActionPrompt) -> None:
        raise AssertionError("foreign action must not render")

    with pytest.raises(ScopeViolation):
        await bound(forbidden).request(
            event.model_copy(update=updates),
            action,
            origin(),
            root_turn_id=" " if change == "blank_root" else "root",
        )


@pytest.mark.parametrize("change", ["request_kind", "origin", "native_root", "function"])
async def test_prose_never_selects_authentication_or_tool_approval(change: str) -> None:
    action, event = action_event()
    payload = dict(action.payload)
    request: dict[str, JsonValue] = {
        "type": "browser_origin_access",
        "origin": "https://example.org",
        "reason": "approve authentication",
    }
    if change == "request_kind":
        request["type"] = "browser_authentication"
    if change == "origin":
        request["origin"] = "https://foreign.org"
    payload["request"] = request
    if change == "native_root":
        payload["turn_id"] = "foreign"
    action = action.model_copy(
        update={"payload": payload, "kind": "function_result" if change == "function" else "native"}
    )
    event = event.model_copy(
        update={"payload": RequiresActionPayload(actions=(action,)).model_dump(mode="json")}
    )
    with pytest.raises((ScopeViolation, UnsupportedCapability)):
        await bound().request(event, action, origin(), root_turn_id="root")


@pytest.mark.parametrize(
    "scope",
    [
        SCOPE.model_copy(update={"tenant_id": "other"}),
        SCOPE.model_copy(update={"account_id": "other"}),
        Scope.platform(reason="operator"),
        Scope.legacy_host_authorized(call_site="test"),
    ],
)
def test_action_binding_requires_real_admitted_scope(scope: Scope) -> None:
    with pytest.raises(ScopeViolation):
        ProviderActionApproval(REQUESTER, NOW, asyncio.Event()).bind(scope, SESSION)


async def test_an_unoffered_native_response_is_never_accepted() -> None:
    action, event = action_event()
    surface = origin()

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        # Even an equal reconstruction cannot inject a body into this callback.
        return NativeActionResponse.from_payload(surface.approve.payload)

    with pytest.raises(ScopeViolation, match="unoffered"):
        await bound(hook).request(event, action, surface, root_turn_id="root")


def test_native_payload_is_immutable_even_below_top_level_and_hidden_from_repr() -> None:
    original: dict[str, JsonValue] = {
        "request_id": "action",
        "nested": {"value": "private-native-data"},
    }
    response = NativeActionResponse.from_payload(original)
    original["nested"] = {"value": "changed"}
    copy = response.payload
    assert isinstance(copy["nested"], dict)
    copy["nested"]["value"] = "changed"
    assert response.payload == {"request_id": "action", "nested": {"value": "private-native-data"}}
    assert "private-native-data" not in repr(response)


@pytest.mark.parametrize(
    "origin_value",
    [
        "https://u:secret@example.org",
        "https://example.org/path",
        "https://example.org?secret=x",
        "file:///etc/passwd",
    ],
)
def test_public_origin_never_carries_credentials_or_other_url_fields(origin_value: str) -> None:
    with pytest.raises(ValueError, match="plain HTTP origin"):
        replace(origin(), origin=origin_value)


def test_naive_expiry_and_collapsed_controls_refuse() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ProviderActionApproval(REQUESTER, NOW.replace(tzinfo=None), asyncio.Event())
    with pytest.raises(ValueError, match="distinct"):
        replace(origin(), deny=native("approve"))


async def test_expiry_during_answer_refuses_the_native_approval() -> None:
    action, event, surface = *action_event(), origin()
    clock = [NOW]
    approval = ProviderActionApproval(
        REQUESTER, NOW + timedelta(minutes=10), asyncio.Event(), now=lambda: clock[0]
    )

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        clock[0] = prompt.context.expires_at
        return surface.approve

    handler = replace(approval, hook=hook).bind(SCOPE, SESSION)
    assert await handler.request(event, action, surface, root_turn_id="root") is None


async def test_authentication_never_accepts_generic_tool_confirmation() -> None:
    action, event = action_event(authentication=True)
    action = action.model_copy(update={"kind": "tool_confirmation"})
    event = event.model_copy(
        update={"payload": RequiresActionPayload(actions=(action,)).model_dump(mode="json")}
    )
    # Opaque offline response fixture: this is not an asserted provider wire schema.
    cancel = NativeActionResponse.from_payload({"opaque_fixture": "cancel"})
    with pytest.raises(UnsupportedCapability, match="native_authentication_action"):
        await bound().request(
            event, action, BrowserAuthentication(None, cancel), root_turn_id="root"
        )


async def test_a_late_action_gets_a_fresh_prompt_window_capped_by_turn_deadline() -> None:
    clock = [NOW + timedelta(minutes=11)]
    prompts: list[ProviderActionPrompt] = []
    surface = origin()

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        prompts.append(prompt)
        return surface.approve

    approval = ProviderActionApproval(
        REQUESTER,
        NOW + timedelta(minutes=25),
        asyncio.Event(),
        timeout=timedelta(minutes=10),
        hook=hook,
        now=lambda: clock[0],
    )
    handler = approval.bind(SCOPE, SESSION)
    action, event = action_event()
    assert await handler.request(event, action, surface, root_turn_id="root") is surface.approve
    assert prompts[0].context.expires_at == NOW + timedelta(minutes=21)
    clock[0] = NOW + timedelta(minutes=22)
    action = action.model_copy(update={"id": "second"})
    event = event.model_copy(update={"payload": {"actions": [action.model_dump(mode="json")]}})
    assert await handler.request(event, action, surface, root_turn_id="root") is surface.approve
    assert prompts[1].context.expires_at == approval.deadline


async def test_a_raising_hook_refuses_without_logging_native_action_content() -> None:
    from structlog.testing import capture_logs

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse:
        raise RuntimeError("secret-hook-error-and-native-payload")

    action, event = action_event()
    with capture_logs() as records:
        assert await bound(hook).request(event, action, origin(), root_turn_id="root") is None
    assert records == [{"event": "provider_action.hook_failed", "log_level": "warning"}]
