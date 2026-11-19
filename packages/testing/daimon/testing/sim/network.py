"""The simulated network: provider hosts answer from strict fakes; nothing else leaves.

Production builds its SDK clients itself (base URLs, TLS, retries, pools). Only the
socket layer is replaced: `httpx.AsyncHTTPTransport` / `httpx.HTTPTransport` hand a
provider host's request to its fake, pass loopback through, and refuse every other
host, so an unexpected outbound call fails the test instead of reaching the internet.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from daimon.testing.sim.shapes import violations

type Handler = Callable[[httpx.Request, re.Match[str]], httpx.Response | Awaitable[httpx.Response]]
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# Hosts production probes that are unreachable off their cloud: refused quietly.
_OFF_CLOUD = frozenset({"metadata.google.internal"})


@dataclass
class Seen:
    """One request a fake received, body decoded when it is JSON."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any


@dataclass
class StrictFake:
    """A provider API: shape-checked routes, recorded traffic, provider-shaped refusals.

    `shape(method, path, Params)` binds a request body to the SDK's own param type;
    a body that departs from it is refused with `refuse`'s 400, as the API does. A
    request no route serves is a violation: the test fails at teardown.
    """

    host: str
    seen: list[Seen] = field(default_factory=list[Seen])
    violations: list[str] = field(default_factory=list[str])
    _routes: list[tuple[str, re.Pattern[str], Handler]] = field(
        default_factory=list[tuple[str, re.Pattern[str], Handler]]
    )
    _shapes: list[tuple[str, re.Pattern[str], object]] = field(
        default_factory=list[tuple[str, re.Pattern[str], object]]
    )

    def route(self, method: str, path: str, handler: Handler) -> None:
        self._routes.append((method, re.compile(path + "$"), handler))

    def shape(self, method: str, path: str, params: object) -> None:
        self._shapes.append((method, re.compile(path + "$"), params))

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        raise NotImplementedError

    def requests(self, method: str, path: str) -> list[Seen]:
        pattern = re.compile(path + "$")
        return [s for s in self.seen if s.method == method and pattern.match(s.path)]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        body: Any = None
        if raw and "json" in request.headers.get("content-type", ""):
            body = json.loads(raw)
        seen = Seen(
            request.method,
            request.url.path,
            {k: request.url.params.get_list(k) for k in request.url.params},
            {k.lower(): v for k, v in request.headers.items()},
            body,
        )
        self.seen.append(seen)
        for method, pattern, params in self._shapes:
            hit = method == request.method and pattern.match(request.url.path)
            if hit and body is not None and (found := violations(body, params)):
                return self.refuse(400, found[0], found[0].split(":")[0].rsplit(".", 1)[-1])
        for method, pattern, handler in self._routes:
            if method == request.method and (match := pattern.match(request.url.path)):
                try:
                    response = handler(request, match)
                    return await response if inspect.isawaitable(response) else response
                except Exception as error:  # a fake bug must fail the test, not look like a 5xx
                    self.violations.append(
                        f"fake raised on {request.method} {request.url.path}: {str(error)[:600]}"
                    )
                    raise
        self.violations.append(f"unserved {request.method} {request.url.path}")
        return self.refuse(404, f"no route for {request.method} {request.url.path}")


@dataclass
class ProviderNetwork:
    """Every simulated provider by host, plus the requests that reached no fake."""

    fakes: dict[str, StrictFake]
    refused: list[str] = field(default_factory=list[str])

    def assert_clean(self) -> None:
        problems = self.refused + [v for fake in self.fakes.values() for v in fake.violations]
        assert not problems, f"simulated network saw unexpected traffic: {problems}"

    @contextmanager
    def installed(self) -> Iterator[ProviderNetwork]:
        real_async = httpx.AsyncHTTPTransport.handle_async_request
        real_sync = httpx.HTTPTransport.handle_request
        network = self

        async def handle_async(transport: httpx.AsyncHTTPTransport, request: httpx.Request):
            fake = network.fakes.get(request.url.host)
            if fake is not None:
                return await fake.handle(request)
            if request.url.host in _LOOPBACK:
                return await real_async(transport, request)
            if request.url.host not in _OFF_CLOUD:
                network.refused.append(f"{request.method} {request.url.host}{request.url.path}")
            raise httpx.ConnectError(f"simulated network: no route to {request.url.host}")

        def handle_sync(transport: httpx.HTTPTransport, request: httpx.Request):
            if request.url.host in _LOOPBACK:
                return real_sync(transport, request)
            network.refused.append(f"{request.method} {request.url.host} (sync)")
            raise httpx.ConnectError(f"simulated network: no route to {request.url.host}")

        httpx.AsyncHTTPTransport.handle_async_request = handle_async  # type: ignore[method-assign]
        httpx.HTTPTransport.handle_request = handle_sync  # type: ignore[method-assign]
        try:
            yield self
        finally:
            httpx.AsyncHTTPTransport.handle_async_request = real_async  # type: ignore[method-assign]
            httpx.HTTPTransport.handle_request = real_sync  # type: ignore[method-assign]


def sse(events: list[dict[str, Any]], *, named: bool = False) -> httpx.Response:
    """A finished SSE body; `named` adds `event:` lines for SDKs that dispatch on them."""
    frames = "".join(
        (f"event: {e['type']}\n" if named else "") + f"data: {json.dumps(e)}\n\n" for e in events
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=frames)
