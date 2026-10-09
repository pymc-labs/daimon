"""Ordered offline recordings at the real AsyncAnthropic HTTP boundary.

Reuse MARouter and ma_models for resource fixtures and sse_response for streams.
No network fallback exists: unexpected or out-of-order requests fail loudly.
Clients default to zero SDK retries so scripts control every retry. Production
uses MA_MAX_RETRIES=8; pass client(max_retries=8) to capture SDK retry traffic.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import NoReturn, cast

import httpx
from anthropic import AsyncAnthropic
from daimon.testing.ma import MARouter, build_fake_anthropic, sse_response

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: bytes
    # Only protocol headers; API keys/cookies/authorization are never recorded.
    protocol_headers: tuple[tuple[str, str], ...]

    def json(self) -> Json:
        return cast(Json, json.loads(self.body)) if self.body else None

    def to_dict(self) -> dict[str, object]:
        content_type = dict(self.protocol_headers).get("content-type", "")
        return {
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "body": self.json() if "json" in content_type else self.body,
            "protocol_headers": self.protocol_headers,
        }


@dataclass(frozen=True)
class ScriptedReply:
    method: str
    path: str
    response: httpx.Response | Exception
    query: tuple[tuple[str, str], ...] | None = None
    request_json: Json = None
    check_json: bool = False

    @classmethod
    def stream(cls, path: str, events: list[dict[str, object]]) -> ScriptedReply:
        return cls("GET", path, sse_response(events))


@dataclass
class ScriptedTransport:
    """Consume strict ordered replies; optional existing router handles setup.

    Router fallback applies only to method/path pairs absent from the script.
    A known scripted pair in the wrong position is always an error. Requests,
    including failed attempts and retries, stay in their original order.
    """

    replies: deque[ScriptedReply] = field(default_factory=deque[ScriptedReply])
    router: MARouter | None = None
    requests: list[RecordedRequest] = field(default_factory=list[RecordedRequest])
    violations: list[str] = field(default_factory=list[str])

    def _violate(self, message: str) -> NoReturn:
        # The SDK wraps handler exceptions as APIConnectionError. Remember the
        # original violation so reconnect/degraded handling cannot swallow it.
        self.violations.append(message)
        raise AssertionError(message)

    def queue(self, *replies: ScriptedReply) -> None:
        self.replies.extend(replies)

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        record = RecordedRequest(
            request.method,
            request.url.path,
            tuple(request.url.params.multi_items()),
            request.read(),
            tuple(
                (name, request.headers[name])
                for name in ("anthropic-beta", "anthropic-version", "content-type")
                if name in request.headers
            ),
        )
        self.requests.append(record)
        if self.replies:
            reply = self.replies[0]
            if (request.method, request.url.path) == (reply.method.upper(), reply.path):
                if reply.query is not None and record.query != reply.query:
                    self._violate(f"Unexpected query for {reply.path}")
                if reply.check_json and record.json() != reply.request_json:
                    self._violate(f"Unexpected body for {reply.path}")
                self.replies.popleft()
                if isinstance(reply.response, Exception):
                    raise reply.response
                return reply.response
            if any(
                (item.method.upper(), item.path) == (request.method, request.url.path)
                for item in self.replies
            ):
                self._violate(
                    f"Out-of-order MA request {request.method} {request.url.path}; "
                    f"expected {reply.method} {reply.path}"
                )
        if self.router is not None:
            try:
                return self.router.dispatch(request)
            except AssertionError as error:
                self._violate(str(error))
        self._violate(f"Unscripted MA request {request.method} {request.url.path}")

    def client(self, *, max_retries: int = 0) -> AsyncAnthropic:
        """Real SDK parsing; opt into the production retry budget when needed."""
        client = build_fake_anthropic(self.dispatch)
        client.max_retries = max_retries
        return client

    def assert_consumed(self) -> None:
        assert not self.violations, f"MA script violations: {self.violations}"
        assert not self.replies, (
            f"Unconsumed MA replies: {[(r.method, r.path) for r in self.replies]}"
        )
