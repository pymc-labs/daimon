"""Typed human responses to provider-native actions, separate from tool approvals.

The provider supplies its exact response bodies. Adapters choose a supplied
response and authorize the trusted requester; core never derives permission
from prose or turns authentication into a tool confirmation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from mux.contracts.events import Event, RequiredAction, RequiresActionPayload
from mux.contracts.ids import ResourceRef, Scope
from mux.errors import ScopeViolation, UnsupportedCapability
from pydantic import JsonValue, TypeAdapter

_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class NativeActionResponse:
    """An immutable provider-encoded object; payload returns an isolated copy.

    Providers validate their native schema before offering a response. Native
    JSON is excluded from repr and must never be put in a card or button token.
    """

    payload_json: str = field(repr=False)

    def __post_init__(self) -> None:
        _OBJECT.validate_json(self.payload_json)

    @classmethod
    def from_payload(cls, payload: Mapping[str, JsonValue]) -> NativeActionResponse:
        value = _OBJECT.validate_python(dict(payload))
        return cls(json.dumps(value, sort_keys=True, separators=(",", ":")))

    @property
    def payload(self) -> dict[str, JsonValue]:
        return _OBJECT.validate_json(self.payload_json)


@dataclass(frozen=True)
class BrowserOriginAccess:
    origin: str
    approve: NativeActionResponse = field(repr=False)
    deny: NativeActionResponse = field(repr=False)
    cancel: NativeActionResponse = field(repr=False)
    type: Literal["browser_origin_access"] = field(default="browser_origin_access", init=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("browser access requires a plain HTTP origin")
        _distinct((self.approve, self.deny, self.cancel))


@dataclass(frozen=True)
class BrowserAuthentication:
    """Human authentication completion; never a secret-field collection form."""

    submit: NativeActionResponse | None = field(repr=False)
    cancel: NativeActionResponse = field(repr=False)
    type: Literal["browser_authentication"] = field(default="browser_authentication", init=False)

    def __post_init__(self) -> None:
        _distinct((self.submit, self.cancel) if self.submit is not None else (self.cancel,))


def _distinct(responses: tuple[NativeActionResponse, ...]) -> None:
    if len({response.payload_json for response in responses}) != len(responses):
        raise ValueError("provider action controls require distinct native responses")


ProviderActionSurface = BrowserOriginAccess | BrowserAuthentication


@dataclass(frozen=True)
class ProviderActionRequester:
    platform: str
    thread_id: str
    platform_user_id: str

    def __post_init__(self) -> None:
        if any(
            not value.strip() for value in (self.platform, self.thread_id, self.platform_user_id)
        ):
            raise ValueError("provider actions require a trusted requester and thread")


@dataclass(frozen=True)
class ProviderActionContext:
    scope: Scope
    session: ResourceRef
    root_turn_id: str
    requester: ProviderActionRequester
    expires_at: datetime


@dataclass(frozen=True)
class ProviderActionPrompt:
    action: RequiredAction = field(repr=False)
    context: ProviderActionContext
    surface: ProviderActionSurface


ProviderActionHook = Callable[[ProviderActionPrompt], Awaitable[NativeActionResponse | None]]


async def no_provider_action_surface(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
    """Missing adapter support leaves the native action unanswered."""
    del prompt
    return None


@dataclass(frozen=True)
class ProviderActionApproval:
    requester: ProviderActionRequester
    expires_at: datetime
    cancel: asyncio.Event = field(repr=False)
    hook: ProviderActionHook = field(default=no_provider_action_surface, repr=False)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        if self.expires_at.utcoffset() is None:
            raise ValueError("provider action expiry must be timezone-aware")

    def bind(self, scope: Scope, session: ResourceRef) -> BoundProviderActions:
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or session.kind != "session"
            or session.tenant_id != scope.tenant_id
            or session.account_id != scope.account_id
        ):
            raise ScopeViolation(session.id, "provider actions require the admitted tenant binding")
        return BoundProviderActions(self, scope, session)


@dataclass(frozen=True)
class BoundProviderActions:
    approval: ProviderActionApproval = field(repr=False)
    scope: Scope
    session: ResourceRef
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _answered: dict[tuple[str, str], tuple[str, NativeActionResponse | None]] = field(
        default_factory=lambda: dict[tuple[str, str], tuple[str, NativeActionResponse | None]](),
        repr=False,
        compare=False,
    )

    async def request(
        self,
        event: Event,
        action: RequiredAction,
        surface: ProviderActionSurface,
        *,
        root_turn_id: str,
    ) -> NativeActionResponse | None:
        """Ask only for an authoritative current-root action; never send it.

        The codec validates native request/response schemas and sends the
        returned payload through its claimed mutation path. None is a refusal,
        expiration or cancellation, never an implicit native approval.
        """
        if (
            not root_turn_id.strip()
            or event.session_id != self.session.id
            or event.native.provider != self.session.provider
            or event.turn_id != root_turn_id
            or event.thread_id is not None
            or event.authority not in ("record", "reconciled")
        ):
            raise ScopeViolation(self.session.id, "provider action is outside the admitted root")
        payload = event.typed_payload()
        if not isinstance(payload, RequiresActionPayload) or action not in payload.actions:
            raise ScopeViolation(self.session.id, "provider action is absent from the root event")
        native_request = action.payload.get("request")
        if (
            action.kind not in ("native", "tool_confirmation")
            or not isinstance(native_request, Mapping)
            or native_request.get("type") != surface.type
        ):
            raise UnsupportedCapability(("typed_provider_action_surface",), self.session.provider)
        if isinstance(surface, BrowserAuthentication) and action.kind != "native":
            raise UnsupportedCapability(("native_authentication_action",), self.session.provider)
        if surface.type == "browser_origin_access" and (
            native_request.get("origin") != surface.origin
        ):
            raise ScopeViolation(self.session.id, "provider action origin differs from its request")
        if action.payload.get("turn_id", root_turn_id) != root_turn_id:
            raise ScopeViolation(self.session.id, "native action belongs to another root")
        choices = (
            (surface.approve, surface.deny, surface.cancel)
            if isinstance(surface, BrowserOriginAccess)
            else (
                (surface.submit, surface.cancel)
                if surface.submit is not None
                else (surface.cancel,)
            )
        )
        fingerprint = action.model_dump_json() + json.dumps(
            [choice.payload_json for choice in choices]
        )
        key = (root_turn_id, action.id)
        async with self._lock:
            if self.approval.cancel.is_set() or self.approval.now() >= self.approval.expires_at:
                return None
            previous = self._answered.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ScopeViolation(
                        self.session.id, "provider action changed after its response"
                    )
                return previous[1]
            response = await self._ask(action, surface, root_turn_id, choices)
            self._answered[key] = (fingerprint, response)
            return response

    async def _ask(
        self,
        action: RequiredAction,
        surface: ProviderActionSurface,
        root_turn_id: str,
        choices: tuple[NativeActionResponse, ...],
    ) -> NativeActionResponse | None:
        approval = self.approval
        if approval.cancel.is_set() or approval.now() >= approval.expires_at:
            return None
        prompt = ProviderActionPrompt(
            action,
            ProviderActionContext(
                self.scope, self.session, root_turn_id, approval.requester, approval.expires_at
            ),
            surface,
        )

        async def invoke_hook() -> NativeActionResponse | None:
            return await approval.hook(prompt)

        response_task = asyncio.create_task(invoke_hook())
        cancel_task = asyncio.create_task(approval.cancel.wait())
        try:
            remaining = max(0.0, (approval.expires_at - approval.now()).total_seconds())
            done, _ = await asyncio.wait(
                (response_task, cancel_task), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if (
                response_task not in done
                or approval.cancel.is_set()
                or approval.now() >= approval.expires_at
            ):
                return None
            response = response_task.result()
            if response is not None and not any(response is choice for choice in choices):
                raise ValueError("provider action hook returned an unoffered native response")
            return response
        finally:
            for task in (response_task, cancel_task):
                task.cancel()
            await asyncio.gather(response_task, cancel_task, return_exceptions=True)
