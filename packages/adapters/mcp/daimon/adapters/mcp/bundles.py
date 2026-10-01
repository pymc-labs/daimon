"""PUT /bundles — push a report-host archive onto the Files API, ownership-bound.

Mounted on the FastMCP-derived ASGI app via `add_route`, the same way
`checkout.py` and `uploads.py` are — this bypasses `IdentityMiddleware`, so
the bearer is verified here, not upstream. The bearer is the report's own
agent token (minted by `mint_agent_mcp_token`), so an uploaded bundle is
bound at mint time to the tenant and agent that will later mount it via
`start_turn(bundle=...)`.

The report host holds no Anthropic key by design (it cannot call the Files
API itself), so this route exists to do the upload on its behalf and hand
back a signed handle instead of a durable database row.

Catch narrowly and only here: this module IS the catch boundary. An upload
failure from the SDK propagates and Starlette turns it into a 500 — the
correct loud failure for an ASGI edge.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.core import bundle_handle
from daimon.core.config import McpSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.stores.pending_file_deletes import enqueue_pending_file_delete
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

log = structlog.get_logger(__name__)

_GZIP_MAGIC = b"\x1f\x8b"


def build_bundles_route(
    *,
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    auth: TokenVerifier,
    mcp_settings: McpSettings,
    rate_limiter: RateLimiter,
) -> Callable[[Request], Awaitable[Response]]:
    """Build the `PUT /bundles` handler with dependencies bound."""

    async def handler(request: Request) -> Response:
        # --- bearer-token auth gate, same shape as checkout.py ---
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return Response(status_code=401)
        token = auth_header[len("bearer ") :]
        access: AccessToken | None = await auth.verify_token(token)
        if access is None:
            return Response(status_code=401)

        token_tenant_raw = access.claims.get("tenant_id")
        if not isinstance(token_tenant_raw, str):
            return Response(status_code=403)
        try:
            tenant_id = uuid.UUID(token_tenant_raw)
        except ValueError:
            return Response(status_code=403)

        # agent_id is read from the raw JWT claim mint_agent_mcp_token signs —
        # a plain account token has no agent scope and must not upload here.
        token_agent_raw = access.claims.get("agent_id")
        if not isinstance(token_agent_raw, str):
            return Response(status_code=403)
        try:
            agent_id = uuid.UUID(token_agent_raw)
        except ValueError:
            return Response(status_code=403)

        if mcp_settings.jwt_secret is None:
            return PlainTextResponse(
                "DAIMON_MCP__JWT_SECRET is required to mint a bundle handle",
                status_code=500,
            )

        rate_limit_key = str(access.claims.get("jti") or agent_id)
        if not rate_limiter.check_and_record(rate_limit_key):
            return PlainTextResponse(
                "bundle upload rate limit exceeded, try again later", status_code=429
            )

        declared = request.headers.get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > mcp_settings.bundle_max_bytes
        ):
            return PlainTextResponse(
                f"bundle exceeds the {mcp_settings.bundle_max_bytes} byte limit",
                status_code=413,
            )

        # Stream rather than buffering the whole body at once: a chunked body
        # with no Content-Length would sail past the header check above, so
        # the cap must also be enforced against a running total while reading.
        digest = hashlib.sha256()
        total_bytes = 0
        with tempfile.SpooledTemporaryFile(max_size=mcp_settings.bundle_max_bytes) as spooled:
            async for chunk in request.stream():
                total_bytes += len(chunk)
                if total_bytes > mcp_settings.bundle_max_bytes:
                    return PlainTextResponse(
                        f"bundle exceeds the {mcp_settings.bundle_max_bytes} byte limit",
                        status_code=413,
                    )
                digest.update(chunk)
                spooled.write(chunk)

            spooled.seek(0)
            magic = spooled.read(2)
            spooled.seek(0)
            if magic != _GZIP_MAGIC:
                return PlainTextResponse("bundle must be a gzip archive", status_code=415)

            uploaded: FileMetadata = await anthropic.beta.files.upload(
                file=("bundle.tar.gz", spooled, "application/gzip"),
            )

        now = datetime.now(UTC)
        delete_after = now + timedelta(days=mcp_settings.bundle_ttl_days)
        async with session_factory() as session, session.begin():
            await enqueue_pending_file_delete(
                session, file_id=uploaded.id, delete_after=delete_after
            )

        # The handle's expiry and the uploaded object's deletion time share the
        # same horizon on purpose — a handle that outlived its object would
        # verify successfully against bytes that no longer exist.
        handle = bundle_handle.mint(
            mcp_settings.jwt_secret.get_secret_value(),
            file_id=uploaded.id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            sha256=digest.hexdigest(),
            now=now,
            ttl_days=mcp_settings.bundle_ttl_days,
        )

        log.info(
            "bundles.uploaded",
            file_id=uploaded.id,
            size_bytes=total_bytes,
            tenant_id=str(tenant_id),
        )
        return JSONResponse(
            {
                "bundle": handle,
                "sha256": digest.hexdigest(),
                "size_bytes": total_bytes,
                "expires_at": delete_after.isoformat(),
            }
        )

    return handler
