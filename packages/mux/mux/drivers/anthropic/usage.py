"""Anthropic model-span usage ports and their shared pure translation."""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.event_list_params import EventListParams
from pydantic import JsonValue

from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter


def observation_from_event(
    raw: Mapping[str, JsonValue],
    session: ResourceRef,
    *,
    observed_at: datetime,
    turn_id: str | None = None,
    thread_id: str | None = None,
    model_id: str | None = None,
) -> UsageObservation:
    """Immutable request-span observation; unknown counts remain unknown."""
    if raw.get("type") != "span.model_request_end":
        raise ValueError("usage requires a model_request_end record")
    meter_value = raw["model_usage"]
    if not isinstance(meter_value, dict):
        raise ValueError("expected a native usage object")
    meter = meter_value

    def count(name: str) -> int | None:
        value = meter.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid token count: {name}")
        return value

    uncached, cached, written = (
        count(name)
        for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    total = (
        None
        if any(v is None for v in (uncached, cached, written))
        else (cast(int, uncached) + cast(int, cached) + cast(int, written))
    )
    event_id = raw["id"]
    if not isinstance(event_id, str):
        raise ValueError("expected a native event ID")
    return UsageObservation(
        id=event_id,
        revision=1,
        session=session,
        turn_id=turn_id,
        thread_id=thread_id,
        model=ModelRef(provider="anthropic", id=model_id) if model_id is not None else None,
        grain="model_request",
        basis="increment",
        input_tokens=total,
        input_cached_tokens=cached,
        input_cache_write_tokens=written,
        output_tokens=count("output_tokens"),
        native_meter=meter,
        completeness="measured"
        if total is not None and count("output_tokens") is not None
        else "partial",
        observed_at=observed_at,
    )


class UsageWalk(Protocol):
    def model_requests(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        model_id: str | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> AsyncIterator[UsageObservation]: ...


class AnthropicUsage:
    """Read only model spans, using the existing SDK paginator and normalizer."""

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    def _check(self, scope: Scope, session: ResourceRef) -> None:
        authorize(self._authorization, scope, "session", session.id)
        check_ref(scope, session, self._account_scope_id, "session")

    async def model_requests(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        model_id: str | None = None,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> AsyncIterator[UsageObservation]:
        self._check(scope, session)
        async for event in provider_iter(
            self._client.beta.sessions.events.list(
                session.id, order="asc", types=["span.model_request_end"]
            )
        ):
            if event.type == "span.model_request_end" and event.id not in exclude_ids:
                yield observation_from_event(
                    event.model_dump(mode="json"),
                    session,
                    observed_at=event.processed_at,
                    model_id=model_id,
                )

    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        return tuple([observation async for observation in self.model_requests(scope, session)])

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        self._check(scope, session)
        kwargs: EventListParams = {"types": ["span.model_request_end"]}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            kwargs["order"] = page.order
        result = await provider_call(self._client.beta.sessions.events.list(session.id, **kwargs))
        return Page(
            data=tuple(
                observation_from_event(
                    event.model_dump(mode="json"), session, observed_at=event.processed_at
                )
                for event in result.data
                if event.type == "span.model_request_end"
            ),
            has_more=result.has_next_page(),
            next_cursor=result.next_page if result.has_next_page() else None,
        )
