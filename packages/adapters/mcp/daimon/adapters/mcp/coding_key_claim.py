"""`POST /coding-keys/claim`: a pasted prompt's code, traded for a coding key.

Mounted on the FastMCP-derived ASGI app via `add_route` in server.py, a sibling
of /uploads and /bundles, and like them it bypasses IdentityMiddleware: the
single-use code IS the auth. The code goes in the form body, never the URL, so
no access log holds it. The answer is the bare key as plain text, so one `curl`
can write it into a config file and the key never enters a chat.

Every refusal is the same empty 404, so a caller cannot tell an unknown code
from a used, expired or revoked one. Attempts are counted per client address:
the one the trusted proxy appended to X-Forwarded-For, never a value the caller
sent.
"""

from __future__ import annotations

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


def client_address(forwarded_for: str, peer: str | None, *, trusted_hops: int) -> str:
    """The caller's address, from the X-Forwarded-For entry the trusted proxies appended.

    Each trusted proxy appends the address it saw, so the entries from the right
    are theirs and everything left of them is whatever the caller sent. The
    `trusted_hops`-th from the right is the outermost trusted proxy's, the
    caller's real address. With none trusted, or fewer entries than proxies (so
    the header is not what they wrote), the socket peer stands.
    """
    entries = [part.strip() for part in forwarded_for.split(",")]
    if trusted_hops > 0 and len(entries) >= trusted_hops and entries[-trusted_hops]:
        return entries[-trusted_hops]
    return peer or "unknown"


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
    trusted_hops: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[Request], Awaitable[Response]]:
    """Build the `POST /coding-keys/claim` handler."""

    async def claim(request: Request) -> Response:
        peer = request.client.host if request.client else None
        address = client_address(
            request.headers.get("x-forwarded-for", ""), peer, trusted_hops=trusted_hops
        )
        if jwt_secret is None or not rate_limiter.check_and_record(address):
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
