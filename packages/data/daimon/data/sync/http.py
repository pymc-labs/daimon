"""HTTP for connectors that call a provider's API.

`GuardedTransport` is the transport every such connector builds its client on: each request
(a redirect's next hop included) is resolved through the network guard, refused when the host
is private and not allowed, and sent to the address that was checked. `send` makes one GET
inside the source's request budget and honours the provider's rate-limit answers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx
from daimon.data.sync.netguard import NotPublic, resolve
from daimon.data.sync.rate import RateBudget

WAITS: Final = (1.0, 2.0, 4.0, 8.0, 16.0)
"""Back-off between retries after a network error or a 5xx answer."""
FLOOR: Final = 0.5
"""The least wait after a 429, whatever the provider says."""
GRACE: Final = 60.0
"""Seconds past a run's deadline a 429 wait may reach before the run fails."""
RESET_CAP: Final = 60.0
"""The longest wait for an emptied rate-limit bucket."""


class Refused(httpx.RequestError):
    """The network guard refused the host."""


class Upstream(RuntimeError):
    """A provider call that failed, worded so that it never echoes a credential."""


class GuardedTransport(httpx.AsyncBaseTransport):
    """Sends each request to the address its host resolved to just now, so a name that later
    points inward is never looked up a second time. `private_hosts` are the names and ranges
    the deployment allows (`DAIMON_DATA__PRIVATE_HOSTS`)."""

    def __init__(
        self,
        private_hosts: Sequence[str] = (),
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._private = tuple(private_hosts)
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        try:
            ip = await resolve(
                url.host, url.port or (443 if url.scheme == "https" else 80), self._private
            )
        except NotPublic as e:
            raise Refused(str(e), request=request) from None
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=ip),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _retry_after(r: httpx.Response) -> float:
    """Seconds a 429 asks for: the `Retry-After` header (seconds or an HTTP date), or the
    JSON body's `retry_after`."""
    for raw in (r.headers.get("retry-after"), _body_retry(r)):
        try:
            return max(float(raw), FLOOR)  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            pass
        try:
            when = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            continue
        return max(
            (when.replace(tzinfo=when.tzinfo or UTC) - datetime.now(UTC)).total_seconds(), FLOOR
        )
    return 1.0


def _clamp(seconds: float, deadline: float | None) -> float:
    """`seconds`, cut to what is left of the run's deadline (and its grace)."""
    if deadline is None:
        return seconds
    return max(min(seconds, deadline + GRACE - time.monotonic()), 0.0)


def _body_retry(r: httpx.Response) -> object:
    try:
        return r.json().get("retry_after")
    except (ValueError, AttributeError):
        return None


async def send(
    client: httpx.AsyncClient,
    budget: RateBudget,
    path: str,
    params: Mapping[str, object] | None = None,
    *,
    deadline: float | None = None,
    cap: float = 600.0,
    waits: Sequence[float] | None = None,
    json: object | None = None,
    throttled: Collection[int] = (429,),
) -> httpx.Response:
    """One GET inside the request budget (a POST of `json` when it is given, for providers
    that read through one). A throttled answer (429; `throttled` adds more) waits what the
    provider asks (up to `cap` in all, and never far past the run's deadline); an emptied
    bucket (`x-ratelimit-remaining: 0`) waits for its reset; network errors and 5xx back off.
    Returns the first answer below 500."""
    query = {k: v for k, v in (params or {}).items() if isinstance(v, str | int | float)}
    delays = WAITS if waits is None else waits
    tries, slept = 0, 0.0
    while True:
        await budget.take()
        try:
            r = await (
                client.get(path, params=query)
                if json is None
                else client.post(path, params=query, json=json)
            )
        except httpx.TransportError:
            r = None
        if r is not None and r.status_code in throttled:
            wait = _retry_after(r)
            slept += wait
            late = deadline is not None and time.monotonic() + wait > deadline + GRACE
            if slept > cap or late:
                raise Upstream(f"rate limited: waited {slept:.0f} s for {path}")
            await asyncio.sleep(wait)
            continue
        if r is not None and r.status_code < 500:
            if r.headers.get("x-ratelimit-remaining") == "0":
                reset = float(r.headers.get("x-ratelimit-reset-after") or 0)
                await asyncio.sleep(_clamp(min(reset, RESET_CAP), deadline))
            return r
        if tries >= len(delays):
            raise Upstream(f"the provider is unavailable ({path})")
        await asyncio.sleep(_clamp(delays[tries], deadline))
        tries += 1


async def download(
    transport: httpx.AsyncBaseTransport,
    url: str | None,
    hosts: Sequence[str],
    cap: int,
    headers: Mapping[str, str] | None = None,
) -> bytes | None:
    """The file at `url`, an https address on one of `hosts` (so `headers` go nowhere else),
    if it is at most `cap` bytes; None otherwise. No redirect is followed."""
    target = httpx.URL(url or "")
    if target.scheme != "https" or target.host not in hosts:
        return None
    try:
        async with (
            httpx.AsyncClient(timeout=30, transport=transport) as client,
            client.stream("GET", target, headers=headers) as r,
        ):
            if r.status_code != 200 or int(r.headers.get("content-length") or 0) > cap:
                return None
            out = bytearray()
            async for chunk in r.aiter_bytes():
                out += chunk
                if len(out) > cap:
                    return None
            return bytes(out)
    except httpx.TransportError:
        raise Upstream("the file download failed") from None
