"""A thin HTTP client for ``daimon-harness/0``, validating replies with the wire models."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from mux.drivers.selfhosted.wire import (
    ActionRequest,
    CancelResponse,
    Event,
    EventsResponse,
    HealthResponse,
    TurnRequest,
)


def parse_sse(text: str) -> list[Event]:
    """Parse an SSE body into validated events; ``id`` must equal the event ``seq``."""
    events: list[Event] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            name, _, value = line.partition(":")
            if name and not line.startswith(":"):
                fields[name] = value.removeprefix(" ")
        if "data" not in fields:
            continue
        event = Event.model_validate_json(fields["data"])
        assert fields.get("id") == str(event.seq), "SSE id must equal the event seq"
        assert fields.get("event") == event.type, "SSE event name must equal the event type"
        events.append(event)
    return events


class HarnessClient:
    """Calls one harness; ``base_url`` may be replaced after a restart."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout_s = timeout_s
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: object = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        merged = self._headers(headers) if authenticated else dict(headers or {})
        return await self._http.request(method, self.base_url + path, json=json, headers=merged)

    async def health(self) -> HealthResponse:
        response = await self.request("GET", "/v0/healthz")
        response.raise_for_status()
        return HealthResponse.model_validate(response.json())

    async def submit(
        self, request: TurnRequest, *, sse: bool = False, after_seq: int | None = None
    ) -> httpx.Response:
        """POST a turn. JSON acceptance (202) by default; ``sse`` reads the whole stream.

        ``after_seq`` is the client's fence: the harness never numbers a new event at or below it.
        """
        accept = "text/event-stream" if sse else "application/json"
        query = "" if after_seq is None else f"?after_seq={after_seq}"
        return await self.request(
            "POST",
            "/v0/turns" + query,
            json=request.model_dump(mode="json"),
            headers={"Accept": accept},
        )

    async def events_response(
        self, session_id: str, after_seq: int = 0, *, replay_after_seq: int | None = None
    ) -> httpx.Response:
        query = f"after_seq={after_seq}"
        if replay_after_seq is not None:
            query += f"&replay_after_seq={replay_after_seq}"
        return await self.request("GET", f"/v0/sessions/{session_id}/events?{query}")

    async def events(
        self, session_id: str, after_seq: int = 0, *, replay_after_seq: int | None = None
    ) -> EventsResponse:
        response = await self.events_response(
            session_id, after_seq, replay_after_seq=replay_after_seq
        )
        response.raise_for_status()
        return EventsResponse.model_validate(response.json())

    async def act(self, turn_id: str, action: ActionRequest) -> httpx.Response:
        return await self.request(
            "POST", f"/v0/turns/{turn_id}/actions", json=action.model_dump(mode="json")
        )

    async def cancel(self, turn_id: str) -> httpx.Response:
        return await self.request("POST", f"/v0/turns/{turn_id}/cancel")

    async def cancelled(self, turn_id: str) -> bool:
        response = await self.cancel(turn_id)
        response.raise_for_status()
        return CancelResponse.model_validate(response.json()).cancelled

    async def wait_for(
        self,
        session_id: str,
        turn_id: str,
        done: Callable[[list[Event]], bool],
        *,
        what: str,
    ) -> list[Event]:
        """Poll the turn's events until ``done`` accepts them; raise AssertionError on timeout."""
        seen: list[Event] = []
        try:
            async with asyncio.timeout(self.timeout_s):
                while True:
                    page = await self.events(session_id, seen[-1].seq if seen else 0)
                    seen += [event for event in page.events if event.turn_id == turn_id]
                    if done(seen):
                        return seen
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise AssertionError(
                f"timed out waiting for {what}; saw {[e.type for e in seen]}"
            ) from None

    async def wait_ended(self, session_id: str, turn_id: str) -> list[Event]:
        return await self.wait_for(
            session_id,
            turn_id,
            lambda events: any(event.type == "turn_ended" for event in events),
            what="turn_ended",
        )

    async def wait_event(self, session_id: str, turn_id: str, event_type: str) -> list[Event]:
        return await self.wait_for(
            session_id,
            turn_id,
            lambda events: any(event.type == event_type for event in events),
            what=event_type,
        )
