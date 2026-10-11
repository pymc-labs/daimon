"""Explicit offline rejected-token submit/readback over the unchanged Discord path.

No application imports this module. The loopback transport is owned by one
fixture; it cannot contact a provider or any unbound MCP origin.
"""

from __future__ import annotations

import hashlib
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

import aiohttp.web
import discord
import httpx
from daimon.adapters.discord.credential_modals import McpCredentialModal
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.mcp_oauth.discovery import McpProbe, probe_mcp_server
from daimon.core.mcp_oauth.urls import assert_public_https_url
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.db import require_owned_sessionmaker
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SOURCE_SHA256 = "5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd"
SOURCE_ASSERTION = (
    "select used_at is null from credential_requests where id = '{headless.request_id}'"
)
_READBACK = text("""
SELECT used_at, outcome, used_at IS NULL AS unused
FROM credential_requests
WHERE token = :request_id AND tenant_id = :tenant_id AND account_id = :account_id
  AND agent_id = :agent_id AND requester_platform_user_id = :requester_id
  AND kind = 'mcp' AND mcp_server_url = :mcp_server_url
  AND target_ma_agent_id = :target_ma_agent_id
""")


class Readback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    used_at: datetime | None
    outcome: str | None
    unused: bool


async def read_used_at(
    factory: async_sessionmaker[AsyncSession], row: CredentialRequestRow
) -> Readback:
    """Only this fixed SELECT is executable; all binds are guarded before connect."""
    require_owned_sessionmaker(factory)
    async with factory() as session:
        result = await session.execute(
            _READBACK,
            {
                "request_id": row.token,
                "tenant_id": row.tenant_id,
                "account_id": row.account_id,
                "agent_id": row.agent_id,
                "requester_id": row.requester_platform_user_id,
                "mcp_server_url": row.mcp_server_url,
                "target_ma_agent_id": row.target_ma_agent_id,
            },
        )
        data = result.mappings().one_or_none()
    if data is None:
        raise ValueError("readback has no genuinely scoped credential request")
    return Readback.model_validate(dict(data))


class _LoopbackTransport(httpx.AsyncBaseTransport):
    def __init__(self, origin: str, endpoint: str) -> None:
        self.origin, self.endpoint = origin, endpoint
        self.inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) != self.origin or request.method != "POST":
            raise ValueError("unbound request refused by loopback fixture")
        forwarded = httpx.Request(
            "POST", self.endpoint, headers=request.headers, content=await request.aread()
        )
        response = await self.inner.handle_async_request(forwarded)
        try:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=await response.aread(),
                request=request,
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass(frozen=True)
class ProbePost:
    token_sha256: str
    method: str
    status_code: int
    observed_s: float


class LoopbackRejectingProbe:
    """Run the real MCP initialize probe against one HTTP loopback rejecting app."""

    def __init__(self, origin: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        assert_public_https_url(origin)
        self.origin, self.clock = origin, clock
        self.posts: list[ProbePost] = []
        self.readbacks: list[Readback] = []
        self.results: list[McpProbe] = []
        self.sample: Callable[[], Awaitable[Readback]] | None = None
        self._runner: aiohttp.web.AppRunner | None = None
        self._endpoint: str | None = None

    async def __aenter__(self) -> LoopbackRejectingProbe:
        if self._runner is not None:
            raise ValueError("loopback probe is already active")
        app = aiohttp.web.Application(client_max_size=16_384)
        app.router.add_post("/mcp", self._reject)
        runner = aiohttp.web.AppRunner(app, access_log=None)
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
            port = sock.getsockname()[1]
            await aiohttp.web.SockSite(runner, sock).start()
        except BaseException:
            sock.close()
            await runner.cleanup()
            raise
        self._runner, self._endpoint = runner, f"http://127.0.0.1:{port}/mcp"
        return self

    async def __aexit__(self, *exc: object) -> None:
        runner, self._runner = self._runner, None
        self._endpoint = None
        if runner is not None:
            await runner.cleanup()

    async def _reject(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        body: object = await request.json()
        if not isinstance(body, dict) or cast(dict[str, Any], body).get("method") != "initialize":
            raise aiohttp.web.HTTPBadRequest()
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise aiohttp.web.HTTPUnauthorized()
        self.posts.append(
            ProbePost(
                token_sha256=hashlib.sha256(authorization[7:].encode()).hexdigest(),
                method="initialize",
                status_code=403,
                observed_s=self.clock(),
            )
        )
        return aiohttp.web.json_response({"error": "fixture token rejected"}, status=403)

    async def __call__(self, url: str, token: str) -> McpProbe:
        if url != self.origin or self._endpoint is None:
            raise ValueError("MCP probe requires its one active owned origin")
        if self.sample is not None:
            self.readbacks.append(await self.sample())
        async with httpx.AsyncClient(
            transport=_LoopbackTransport(self.origin, self._endpoint),
            trust_env=False,
            follow_redirects=False,
            timeout=5.0,
        ) as client:
            result = await probe_mcp_server(client, mcp_server_url=url, bearer_token=token)
            self.results.append(result)
            return result


class RejectedTokenReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    scenario_id: Literal["QA-NEW27-MISTYPED-TOKEN-HEADLESS"] = "QA-NEW27-MISTYPED-TOKEN-HEADLESS"
    provider: Literal["anthropic"] = "anthropic"
    source_sha256: Literal["5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd"] = (
        SOURCE_SHA256
    )
    request_token_sha256: str
    before: Readback
    at_probe: Readback
    after: Readback
    probe_status: Literal[403]
    unused_assertion: bool


async def rejected_token_submit(
    *,
    runtime: DiscordRuntime,
    row: CredentialRequestRow,
    interaction: discord.Interaction,
    token_value: str,
    probe: LoopbackRejectingProbe,
) -> RejectedTokenReceipt:
    """Call the genuine modal handler, then read its actual row. Never fix it."""
    require_owned_sessionmaker(runtime.sessionmaker)
    if (
        runtime.mcp_token_probe is not probe
        or probe.sample is not None
        or probe.origin != row.mcp_server_url
        or probe.posts
        or probe.readbacks
        or probe.results
    ):
        raise ValueError("submit requires one fresh owned rejecting probe")

    async def sample() -> Readback:
        return await read_used_at(runtime.sessionmaker, row)

    # Reserve before the first await: one probe belongs to one submit.
    probe.sample = sample
    try:
        return await _submit(runtime, row, interaction, token_value, probe)
    finally:
        probe.sample = None


async def _submit(
    runtime: DiscordRuntime,
    row: CredentialRequestRow,
    interaction: discord.Interaction,
    token_value: str,
    probe: LoopbackRejectingProbe,
) -> RejectedTokenReceipt:
    before = await read_used_at(runtime.sessionmaker, row)
    if not before.unused:
        raise ValueError("submit requires a genuinely unused owned form")
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    # discord.py fills this field from the authenticated modal input; only this
    # dedicated offline form object is populated, never a product handler patched.
    modal.token_input._value = token_value  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(interaction)
    after = await read_used_at(runtime.sessionmaker, row)
    if (
        len(probe.posts) != 1
        or len(probe.readbacks) != 1
        or len(probe.results) != 1
        or probe.results[0].status_code != 403
        or probe.posts[0].token_sha256 != hashlib.sha256(token_value.encode()).hexdigest()
    ):
        raise ValueError("real submit did not reach its owned rejecting MCP server once")
    # Main #611 may release this attempt; surviving consumption must match the probe.
    if after.used_at is not None and probe.readbacks[0].used_at != after.used_at:
        raise ValueError("rejected-token readbacks changed across the rejecting probe")
    return RejectedTokenReceipt(
        request_token_sha256=hashlib.sha256(row.token.encode()).hexdigest(),
        before=before,
        at_probe=probe.readbacks[0],
        after=after,
        unused_assertion=after.unused,
        probe_status=probe.results[0].status_code,
    )
