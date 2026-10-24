"""Measured model-request observations, replayed without introducing new charges."""

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted.events import SelfHostedEvents


class SelfHostedUsage:
    def __init__(self, events: SelfHostedEvents) -> None:
        self._events = events

    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        await self._events.replay(scope, session)
        return self._events.observations(session)

    async def reconcile_turn(
        self, scope: Scope, session: ResourceRef, turn_id: str
    ) -> tuple[UsageObservation, ...]:
        """Filter at the source before the host applies its per-turn bound."""
        return tuple(
            value
            for value in await self.reconcile(scope, session)
            if value.turn_id == turn_id and value.thread_id is None
        )

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        values = await self.reconcile(scope, session)
        if page.order == "desc":
            values = values[::-1]
        if page.cursor is not None:
            index = next((i for i, value in enumerate(values) if value.id == page.cursor), None)
            if index is None:
                raise ValueError("unknown usage cursor")
            values = values[index + 1 :]
        count = page.limit or 100
        more = len(values) > count
        values = values[:count]
        return Page(data=values, has_more=more, next_cursor=values[-1].id if more else None)
