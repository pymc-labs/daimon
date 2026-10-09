"""Put existing turn fixture scripts behind the real SDK and HTTP transport.

The fixture retains its assertions/counters and scripted event iterator. SDK
serialization, query strings, beta headers and SSE parsing run normally.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx
import pytest
from daimon.testing import turn_fakes
from daimon.testing.ma import list_response, send_events_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport


class EventBytes(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for event in self.stream:
            value = event.model_dump(mode="json")
            yield f"event: {value['type']}\ndata: {json.dumps(value)}\n\n".encode()

    async def aclose(self) -> None:
        await self.stream.close()


class HttpTurnFixtures:
    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.clients: dict[int, anthropic.AsyncAnthropic] = {}
        self.scripts: list[ScriptedTransport] = []
        original_send = turn_fakes.FakeEventsResource.send
        original_stream = turn_fakes.FakeEventsResource.stream
        original_retrieve = turn_fakes.FakeSessionsBeta.retrieve
        owners: dict[int, Any] = {}
        timeouts: dict[int, Any] = {}

        def client(events: Any) -> Any:
            key = id(events)
            if key in self.clients:
                return self.clients[key]
            script = ScriptedTransport()
            self.scripts.append(script)

            async def dispatch(request: httpx.Request) -> httpx.Response:
                await request.aread()
                match = re.fullmatch(
                    r"/v1/sessions/([^/]+)(/events(?:/stream)?)?", request.url.path
                )
                if match is None:
                    script.violations.append(f"Unexpected turn fixture path: {request.url.path}")
                    raise AssertionError(script.violations[-1])
                assert match is not None
                session_id, suffix = match.groups()
                try:
                    if request.method == "POST" and suffix == "/events":
                        await original_send(
                            events, session_id, events=json.loads(request.content)["events"]
                        )
                        response = send_events_response()
                    elif request.method == "GET" and suffix == "/events/stream":
                        stream = await original_stream(
                            events, session_id=session_id, timeout=timeouts.get(key)
                        )
                        response = httpx.Response(
                            200,
                            headers={"content-type": "text/event-stream"},
                            stream=EventBytes(stream),
                        )
                    elif request.method == "GET" and suffix == "/events":
                        response = list_response(
                            [event.model_dump(mode="json") for event in events.replay_events]
                        )
                    elif request.method == "GET" and suffix is None and key in owners:
                        session = await original_retrieve(owners[key], session_id)
                        response = httpx.Response(200, json=session.model_dump(mode="json"))
                    else:
                        script.violations.append(
                            f"Unexpected turn fixture request: {request.method} {request.url.path}"
                        )
                        raise AssertionError(script.violations[-1])
                except anthropic.APIStatusError as error:
                    response = httpx.Response(
                        error.status_code,
                        headers=dict(error.response.headers),
                        json={
                            "type": "error",
                            "error": {
                                "type": "rate_limit_error"
                                if error.status_code == 429
                                else "api_error",
                                "message": str(error),
                            },
                        },
                    )
                script.queue(ScriptedReply(request.method, request.url.path, response))
                return script.dispatch(request)

            result = anthropic.AsyncAnthropic(
                api_key="offline-fixture",
                max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(dispatch)),
            )
            self.clients[key] = result
            return result

        async def send(resource: Any, session_id: str, **kwargs: Any) -> Any:
            return await client(resource).beta.sessions.events.send(session_id, **kwargs)

        async def stream(events: Any, **kwargs: Any) -> Any:
            timeouts[id(events)] = kwargs.get("timeout")
            return await client(events).beta.sessions.events.stream(**kwargs)

        def replay(events: Any, **kwargs: Any) -> Any:
            return client(events).beta.sessions.events.list(**kwargs)

        async def retrieve(sessions: Any, session_id: str) -> Any:
            owners[id(sessions.events)] = sessions
            return await client(sessions.events).beta.sessions.retrieve(session_id)

        patch.setattr(turn_fakes.FakeEventsResource, "send", send)
        patch.setattr(turn_fakes.FakeEventsResource, "stream", stream)
        patch.setattr(turn_fakes.FakeEventsResource, "list", replay)
        patch.setattr(turn_fakes.FakeSessionsBeta, "retrieve", retrieve)

    def assert_consumed(self) -> None:
        for script in self.scripts:
            script.assert_consumed()
