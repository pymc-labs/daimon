"""What the read-only live connectors (Linear, HubSpot) share: a client on the guarded
transport with the source's own token, and one call inside the source's request budget that
never waits long (a search that cannot be answered quickly is left out)."""

from __future__ import annotations

import httpx
from daimon.data.sync.connector import Json, Source
from daimon.data.sync.connectors.chat import obj
from daimon.data.sync.http import GuardedTransport, Upstream, send

WAIT: float = 2.0
"""Seconds of throttling a call waits out in all."""


def client_of(
    source: Source,
    base_url: str,
    transport: httpx.AsyncBaseTransport | None,
    *,
    bearer: bool = True,
) -> httpx.AsyncClient:
    token = source.credentials.get("token", "")
    if not token:
        raise ValueError("the credential needs the token")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}" if bearer else token},
        timeout=10,
        transport=transport or GuardedTransport(source.private_hosts),
    )


async def call(
    client: httpx.AsyncClient, source: Source, service: str, path: str, body: Json | None = None
) -> Json:
    """A GET, or a POST of `body`; the answer's JSON object. A refusal or error raises
    `Upstream`, worded without the provider's own text."""
    r = await send(
        client, source.budget, path, None, deadline=source.deadline, cap=WAIT, waits=(), json=body
    )
    if r.status_code in (401, 403):
        raise Upstream(f"{service} refused the token (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise Upstream(f"{service} answered HTTP {r.status_code}")
    return obj(r.json())
