"""Fixed native session operations for M0 tools that consume SDK projections."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.pagination import AsyncPageCursor
from anthropic.types.beta.session_list_params import SessionListParams
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from anthropic.types.beta.sessions.event_list_params import EventListParams
from anthropic.types.beta.sessions.event_send_params import EventSendParams
from pydantic import PrivateAttr

from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_record,
    check_ref,
    visible,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._native import NativeSnapshot, native_snapshot
from mux.drivers.anthropic.schemas import NativeConfig
from mux.drivers.anthropic.transport import mutation_client


class MessageText(NativeConfig):
    type: Literal["text"]
    text: str


class UserMessage(NativeConfig):
    type: Literal["user.message"]
    content: list[MessageText]


class UserInterrupt(NativeConfig):
    type: Literal["user.interrupt"]


class SessionSend(NativeConfig):
    events: list[UserMessage | UserInterrupt]


class EventQuery(NativeConfig):
    page: str | None = None
    limit: int | None = None
    order: Literal["asc", "desc"] | None = None
    created_at_gte: str | datetime | None = None
    created_at_lt: str | datetime | None = None
    types: list[str] | None = None


class EventPage(NativeConfig):
    data: tuple[NativeSnapshot, ...]
    next_page: str | None
    _continue: Callable[[], Awaitable[EventPage]] | None = PrivateAttr(default=None)

    async def continue_page(self) -> EventPage:
        """Continue this authorized native SDK page; never reconstruct its query."""
        if not self.data or not self.next_page or self._continue is None:
            raise RuntimeError("no authorized native event continuation")
        return await self._continue()


class SessionTools(Protocol):
    async def retrieve(self, scope: Scope, session: ResourceRef) -> NativeSnapshot: ...
    def walk(
        self, scope: Scope, agent: ResourceRef, *, page: str | None = None
    ) -> AsyncIterator[NativeSnapshot]: ...
    async def send(
        self, scope: Scope, session: ResourceRef, config: SessionSend, *, key: str
    ) -> NativeSnapshot: ...
    async def list_events(
        self, scope: Scope, session: ResourceRef, query: EventQuery
    ) -> EventPage: ...


class AnthropicSessionTools:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    def _check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        authorize(self._authorization, scope, kind, ref.id)
        check_ref(scope, ref, self._account_scope_id, kind)

    async def retrieve(self, scope: Scope, session: ResourceRef) -> NativeSnapshot:
        self._check(scope, session, "session")
        item = await provider_call(self._client.beta.sessions.retrieve(session.id))
        check_record(scope, session.id, item.metadata)
        # MCP reads the SDK projection, not a durable provider binding. Native
        # location metadata can be absent/null and must remain untouched.
        return native_snapshot(item)

    async def walk(
        self, scope: Scope, agent: ResourceRef, *, page: str | None = None
    ) -> AsyncIterator[NativeSnapshot]:
        self._check(scope, agent, "agent")
        kwargs: SessionListParams = {"agent_id": agent.id}
        if page is not None:
            kwargs["page"] = page
        # Match the old AsyncPaginator loop, including its empty-page stops.
        async for item in provider_iter(self._client.beta.sessions.list(**kwargs)):
            if visible(scope, item.metadata):
                yield native_snapshot(item)

    async def send(
        self, scope: Scope, session: ResourceRef, config: SessionSend, *, key: str
    ) -> NativeSnapshot:
        self._check(scope, session, "session")
        # M0 keys pass through; preserve SDK error/echo behavior, without replay.
        result = await provider_call(
            mutation_client(self._client, single_attempt=True).beta.sessions.events.send(
                session.id, **cast(EventSendParams, config.model_dump(exclude_unset=True))
            )
        )
        return native_snapshot(result)

    async def list_events(self, scope: Scope, session: ResourceRef, query: EventQuery) -> EventPage:
        self._check(scope, session, "session")
        result = await provider_call(
            self._client.beta.sessions.events.list(
                session.id, **cast(EventListParams, query.model_dump(exclude_unset=True))
            )
        )
        return self._event_page(scope, session, result)

    def _event_page(
        self,
        scope: Scope,
        session: ResourceRef,
        result: AsyncPageCursor[BetaManagedAgentsSessionEvent],
    ) -> EventPage:
        # No eager paginator walk. The SDK owns continuation params/header order.
        page = EventPage(
            data=tuple(native_snapshot(event) for event in result.data),
            # The SDK's own value; a page object without the field (an empty
            # stand-in) reads as no cursor, as `has_next_page()` already says.
            next_page=getattr(result, "next_page", None),
        )
        if result.has_next_page():

            async def continue_page() -> EventPage:
                self._check(scope, session, "session")
                following = await provider_call(result.get_next_page())
                return self._event_page(scope, session, following)

            page._continue = continue_page  # pyright: ignore[reportPrivateUsage]  # driver-owned cursor
        return page
