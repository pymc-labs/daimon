"""Revisioned best-effort turn usage; nullable counts are never fabricated."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.openai._common import Context, objects, owned, query, text
from mux.drivers.openai.transport import Object, object_json, segment


class UsageRevisions(Protocol):
    """Host-owned atomic revision allocation; retain it across driver restarts.

    compare-and-swap the meter and increment only when it changes. A production
    host must persist this data and serialize refreshes per session; a worker
    cannot assign ordered revisions to stale overlapping provider snapshots.
    """

    async def revise(
        self, session: ResourceRef, observation_id: str, meter: Mapping[str, JsonValue]
    ) -> int: ...


class MemoryUsageRevisions:
    """Offline implementation. Sharing this object simulates host persistence."""

    def __init__(self) -> None:
        self._values: dict[tuple[ResourceRef, str], tuple[Object, int]] = {}

    async def revise(
        self, session: ResourceRef, observation_id: str, meter: Mapping[str, JsonValue]
    ) -> int:
        identity = (session, observation_id)
        prior = self._values.get(identity)
        rev = 1 if prior is None else prior[1] + (prior[0] != meter)
        self._values[identity] = (object_json(dict(meter)), rev)
        return rev


#: Ended turns: their items no longer change, so their search count is cached.
_ENDED = frozenset({"completed", "cancelled", "failed"})
_SEARCHES: OrderedDict[tuple[ResourceRef, str], tuple[str, int]] = OrderedDict()
_SEARCH_CACHE = 4096


def billable_search(item: Mapping[str, JsonValue]) -> bool:
    """A completed `web_search_call` search action; page opens and finds are not searches."""
    action = item.get("action")
    kind = action.get("type") if isinstance(action, dict) else None
    return (
        item.get("type") == "web_search_call"
        and item.get("status") == "completed"
        and kind in (None, "search")
    )


def observation(
    turn: Object, session: ResourceRef, *, revision: int, web_searches: int = 0
) -> UsageObservation:
    value = turn.get("usage")
    meter = {} if value is None else object_json(value)

    def count(raw: JsonValue) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("invalid token count")
        return raw

    cached = object_json(meter.get("input_tokens_details") or {})
    reasoning = object_json(meter.get("output_tokens_details") or {})
    inputs, outputs = count(meter.get("input_tokens")), count(meter.get("output_tokens"))
    return UsageObservation(
        id="openai:turn:" + text(turn["id"]),
        revision=revision,
        session=session,
        turn_id=text(turn["id"]),
        thread_id=text(turn["subagent_id"]) if turn.get("subagent_id") is not None else None,
        grain="turn",
        basis="cumulative",
        input_tokens=inputs,
        input_cached_tokens=count(cached.get("cached_tokens")),
        output_tokens=outputs,
        output_reasoning_tokens=count(reasoning.get("reasoning_tokens")),
        native_meter=meter,
        completeness="unknown"
        if value is None
        else "measured"
        if inputs is not None and outputs is not None
        else "partial",
        observed_at=datetime.now(UTC),
        web_searches=web_searches,
    )


class OpenAIUsage:
    def __init__(self, context: Context, revisions: UsageRevisions) -> None:
        self._c, self._revisions = context, revisions

    @owned
    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        self._c.check(scope, session, "session")
        raw = await self._c.call(
            "GET", f"/agents/sessions/{segment(session.id)}/turns", params=query(page)
        )
        values: list[UsageObservation] = []
        for turn in objects(raw["data"]):
            if turn.get("session_id") != session.id:
                raise ValueError("usage belongs to another session")
            observation(turn, session, revision=1)
            searches = await self._searches(session, turn)
            # Null usage and an empty reported meter have different completeness.
            # Attribution also belongs to observation identity. Retain both in
            # the revision signature rather than collapsing them to counts.
            meter: Object = {
                "usage": turn.get("usage"),
                "subagent_id": turn.get("subagent_id"),
            }
            if searches:
                meter["web_searches"] = searches
            rev = await self._revisions.revise(session, "openai:turn:" + text(turn["id"]), meter)
            values.append(observation(turn, session, revision=rev, web_searches=searches))
        more = raw["has_more"]
        if not isinstance(more, bool):
            raise ValueError("invalid has_more")
        return Page(
            data=tuple(values), has_more=more, next_cursor=text(raw["last_id"]) if more else None
        )

    async def _searches(self, session: ResourceRef, turn: Object) -> int:
        """Billable searches of an ended turn, read from its items.

        Turn usage carries no search count. A running turn reports none yet: its
        final observation carries them all, under a new revision.
        """
        if turn.get("status") not in _ENDED:
            return 0
        turn_id = text(turn["id"])
        key, signature = (session, turn_id), json.dumps([turn.get("status"), turn.get("usage")])
        if (hit := _SEARCHES.get(key)) is not None and hit[0] == signature:
            _SEARCHES.move_to_end(key)
            return hit[1]
        searches, cursor, seen = set[str](), None, set[str]()
        path = f"/agents/sessions/{segment(session.id)}/turns/{segment(turn_id)}/items"
        while True:
            params: dict[str, str | int] = {"order": "asc", "limit": 100}
            if cursor is not None:
                params["after"] = cursor
            raw = await self._c.call("GET", path, params=params)
            items = objects(raw["data"])
            searches.update(text(item["id"]) for item in items if billable_search(item))
            if raw["has_more"] is False:
                break
            if raw["has_more"] is not True:
                raise ValueError("invalid has_more")
            cursor = text(raw["last_id"])
            if cursor in seen:
                raise ValueError("usage pagination loop")
            seen.add(cursor)
        _SEARCHES[key] = (signature, len(searches))
        if len(_SEARCHES) > _SEARCH_CACHE:
            _SEARCHES.popitem(last=False)
        return len(searches)

    @owned
    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        values: list[UsageObservation] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await self.list(scope, session, page=PageRequest(cursor=cursor, order="asc"))
            values.extend(page.data)
            if page.next_cursor is None:
                return tuple(values)
            if page.next_cursor in seen:
                raise ValueError("usage pagination loop")
            seen.add(page.next_cursor)
            cursor = page.next_cursor
