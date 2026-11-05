"""`POST /coding-keys/claim`: a pasted prompt's code, traded for a coding key.

Mounted on the FastMCP-derived ASGI app via `add_route` in server.py, a sibling
of /uploads and /bundles, and like them it bypasses IdentityMiddleware: the
single-use code IS the auth. The code goes in the form body, never the URL, so
no access log holds it. The answer is the bare key as plain text, so one `curl`
can write it into a config file and the key never enters a chat.

Every refusal is the same empty 404, so a caller cannot tell an unknown code
from a used, expired or revoked one. Attempts are counted per client key: the
rightmost entries of X-Forwarded-For, joined (see `client_key`).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs

import structlog
from daimon.core.agent_editing import AgentEditDeps
from daimon.core.coding_key_connect import claim_coding_key
from daimon.core.notebooks._rate_limit import RateLimiter
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

log = structlog.get_logger(__name__)

#: A form body of one code is under 100 bytes; anything past this is not one.
_MAX_BODY_BYTES = 1024


def _refused() -> Response:
    return Response(status_code=404)


def _normalised(address: str) -> str:
    """The address as one rate-limit identity: an IPv6 address is its /64, as one
    subscriber holds a whole /64 and could otherwise rotate through 2^64 of them;
    an IPv4-mapped IPv6 address is the IPv4 one. Anything else is kept as sent."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def client_key(forwarded_for: str, peer: str | None, *, entries: int) -> str:
    """What the claim route counts attempts against.

    The rightmost `entries` of X-Forwarded-For, each normalised and joined. What a
    proxy appended sits at the right and a caller can only add to the left, so
    nobody can forge a way into another caller's key. The service may be reached
    both behind a load balancer, which appends `<client>, <lb>`, and directly,
    where the host appends `<client>`; taking both entries keeps the two apart,
    and at worst a direct caller rotates its own key, which gains nothing against
    a code that cannot be guessed. With none counted, or fewer entries than that
    (so the header is not what a proxy wrote), the normalised socket peer stands.
    """
    parts = [part.strip() for part in forwarded_for.split(",")]
    if entries > 0 and len(parts) >= entries and all(parts[-entries:]):
        return ",".join(_normalised(part) for part in parts[-entries:])
    return _normalised(peer) if peer else "unknown"


async def _code_of(request: Request) -> str | None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY_BYTES:
            return None
    values = parse_qs(body.decode("utf-8", errors="replace")).get("code", [])
    return values[0] if len(values) == 1 and values[0] else None


def build_coding_key_claim_route(
    *,
    deps: AgentEditDeps,
    jwt_secret: SecretStr | None,
    rate_limiter: RateLimiter,
    forwarded_entries: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[Request], Awaitable[Response]]:
    """Build the `POST /coding-keys/claim` handler."""

    async def claim(request: Request) -> Response:
        peer = request.client.host if request.client else None
        key = client_key(
            request.headers.get("x-forwarded-for", ""), peer, entries=forwarded_entries
        )
        if jwt_secret is None or not rate_limiter.check_and_record(key):
            return _refused()
        code = await _code_of(request)
        if code is None:
            return _refused()
        token = await claim_coding_key(
            deps,
            code=code,
            secret=jwt_secret.get_secret_value().encode(),
            now=clock(),
        )
        if token is None:
            log.info("coding_key.claim_refused")
            return _refused()
        log.info("coding_key.claimed")
        return PlainTextResponse(token, headers={"Cache-Control": "no-store"})

    return claim
