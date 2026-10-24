"""Explicit request-owned Gemini SDK transport, with no ambient routing."""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue

from mux.drivers import isolated_ssl_context
from mux.drivers.gemini.catalog_transport import AgentCatalogTransport, SDKAgentCatalogTransport
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import MountEchoBook, Object, SDKTransport, Transport

if TYPE_CHECKING:
    from google import genai


_TLS_PROXY_ENV = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def _require_clean_environment() -> None:
    # Iterate names only: Mapping membership/getenv can read a present value.
    # Unknown provider knobs (including mixed-case names) also refuse; names
    # and values may contain secrets, so neither appears in the exception.
    if any(
        name.upper().startswith(("GOOGLE_", "GEMINI_")) or name.upper() in _TLS_PROXY_ENV
        for name in os.environ
    ):
        raise ValueError("Gemini transport refuses ambient SDK/TLS/proxy configuration")


class _OwnedStream(AsyncIterator[Object]):
    def __init__(self, source: AsyncIterator[Object], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> _OwnedStream:
        return self

    async def __anext__(self) -> Object:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._source)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._source, "aclose", None)
            if callable(close):
                await cast(Callable[[], Awaitable[None]], close)()
        finally:
            await self._close()


class _ConfiguredTransport:
    def __init__(self, api_key: str, *, protect_storage_credentials: bool = False) -> None:
        self._api_key = api_key
        self._protect_storage_credentials = protect_storage_credentials
        self._storage_tokens: set[RedactedCredential] = set()
        # Interaction id -> digests of mount headers sent (never the token).
        self._mount_echoes = MountEchoBook()
        # Shared like the storage tokens: each call gets a fresh SDKTransport, and
        # every later read must scrub a credential an earlier create dispatched.
        self._mcp_tokens: set[RedactedCredential] = set()

    def mount_echo_digests(self, interaction_id: str) -> frozenset[str]:
        return self._mount_echoes.get(interaction_id)

    def bind_mount_echo(self, interaction_id: str, digests: AbstractSet[str]) -> None:
        self._mount_echoes.add(interaction_id, digests)

    def _sdk(self, client: genai.Client) -> SDKTransport:
        return SDKTransport(
            client,
            protect_storage_credentials=self._protect_storage_credentials,
            storage_tokens=self._storage_tokens,
            mount_echoes=self._mount_echoes,
            mcp_tokens=self._mcp_tokens,
        )

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[genai.Client]:
        _require_clean_environment()
        # Validate request policy before constructor imports or TLS/HTTP allocation.
        from google import genai
        from google.genai.types import HttpOptions, HttpRetryOptions

        context = isolated_ssl_context()
        with httpx.Client(trust_env=False, timeout=60, verify=context, proxy=None) as synchronous:
            async with httpx.AsyncClient(
                trust_env=False, timeout=60, verify=context, proxy=None
            ) as asynchronous:
                client = genai.Client(
                    api_key=self._api_key,
                    enterprise=False,
                    vertexai=False,
                    credentials=None,
                    project=None,
                    location=None,
                    debug_config=genai.client.DebugConfig(
                        client_mode=None, replays_directory=None, replay_id=None
                    ),
                    http_options=HttpOptions(
                        base_url="https://generativelanguage.googleapis.com",
                        api_version="v1beta",
                        httpx_client=synchronous,
                        httpx_async_client=asynchronous,
                        client_args={"trust_env": False, "verify": context, "proxy": None},
                        async_client_args={
                            "trust_env": False,
                            "verify": context,
                            "ssl": context,
                            "proxy": None,
                        },
                        headers={"x-goog-api-key": self._api_key},
                        retry_options=HttpRetryOptions(attempts=1),
                    ),
                )
                try:
                    # The interactions SDK counts retries rather than attempts.
                    # Pin the owned clients before exposing any request boundary.
                    client._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                    client.aio._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                    yield client
                finally:
                    try:
                        await client.aio.aclose()
                    finally:
                        client.close()

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        async with self._client() as client:
            return await self._sdk(client).count_input_tokens(request)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        async with self._client() as client:
            return await self._sdk(client).create(request)

    async def get(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await self._sdk(client).get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        async with self._client() as client:
            return await self._sdk(client).cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        async with self._client() as client:
            return await self._sdk(client).download_snapshot(environment_id)

    async def open_stream(
        self,
        interaction_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[Object]:
        lifetime = self._client()
        client = await lifetime.__aenter__()
        try:
            source = await self._sdk(client).open_stream(interaction_id, after=after)
        except BaseException:
            await lifetime.__aexit__(None, None, None)
            raise

        async def close() -> None:
            await lifetime.__aexit__(None, None, None)

        return _OwnedStream(source, close)


def configured_transport(*, api_key: str, protect_storage_credentials: bool = False) -> Transport:
    if not api_key.strip():
        raise ValueError("explicit Gemini credential is required")
    return _ConfiguredTransport(api_key, protect_storage_credentials=protect_storage_credentials)


class _ConfiguredCatalogTransport(_ConfiguredTransport):
    async def create_agent(self, definition: Mapping[str, JsonValue]) -> Object:
        async with self._client() as client:
            return await SDKAgentCatalogTransport(client).create_agent(definition)

    async def get_agent(self, id_: str) -> Object:
        async with self._client() as client:
            return await SDKAgentCatalogTransport(client).get_agent(id_)


def configured_catalog_transport(*, api_key: str) -> AgentCatalogTransport:
    if not api_key.strip():
        raise ValueError("explicit Gemini credential is required")
    return _ConfiguredCatalogTransport(api_key)


def downscoped_storage_token(
    *,
    token: str,
    expires_at: datetime,
    retained_until: datetime,
    bucket: str,
    prefix: str,
    sources: tuple[str, ...],
) -> str:
    """Exchange explicit source authority for one read-only bucket/prefix CAB.

    The source bearer is used only at Google's STS edge, never at the sandbox
    edge. No ambient credentials, refresh-token grant, redirect or retry exists.
    """
    from google.auth import downscoped
    from google.auth.transport import Request, Response
    from google.oauth2.credentials import Credentials

    from mux.drivers.gemini.mcp_auth import credential
    from mux.errors import ProviderError

    try:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", bucket)
            or not re.fullmatch(r"[A-Za-z0-9._/-]+/", prefix)
            or prefix.startswith("/")
            or any(part in ("", ".", "..") for part in prefix[:-1].split("/"))
            or not sources
            or any(not source.startswith(f"gs://{bucket}/{prefix}") for source in sources)
            or prefix
            != posixpath.commonpath([urlsplit(source).path.strip("/") for source in sources]) + "/"
        ):
            raise ValueError("invalid storage credential boundary")
        expression = (
            f"resource.name.startsWith('projects/_/buckets/{bucket}/objects/{prefix}') || "
            "api.getAttribute('storage.googleapis.com/objectListPrefix', '')"
            f".startsWith('{prefix}')"
        )
        boundary = downscoped.CredentialAccessBoundary(
            rules=[
                downscoped.AccessBoundaryRule(
                    available_resource=f"//storage.googleapis.com/projects/_/buckets/{bucket}",
                    available_permissions=["inRole:roles/storage.objectViewer"],
                    availability_condition=downscoped.AvailabilityCondition(expression=expression),
                )
            ]
        )
        expiry = expires_at.astimezone(UTC).replace(tzinfo=None)

        def explicit_source(
            _request: Request, scopes: Sequence[str] | None = None
        ) -> tuple[str, datetime]:
            return token, expiry

        source = Credentials(token=token, expiry=expiry, refresh_handler=explicit_source)

        class ScopedCredential(Protocol):
            token: object
            expiry: object

            def refresh(self, request: Request) -> None: ...

        restricted = cast(
            ScopedCredential,
            downscoped.Credentials(source_credentials=source, credential_access_boundary=boundary),
        )

        class Result(Response):
            def __init__(self, response: httpx.Response) -> None:
                self.response = response

            @property
            def status(self) -> int:
                return self.response.status_code

            @property
            def headers(self) -> Mapping[str, str]:
                return self.response.headers

            @property
            def data(self) -> bytes:
                return self.response.content

        class Exchange(Request):
            def __init__(self, client: httpx.Client) -> None:
                self.client, self.used = client, False

            def __call__(
                self,
                url: str,
                method: str = "GET",
                body: bytes | None = None,
                headers: Mapping[str, str] | None = None,
                timeout: float | None = None,
                **kwargs: object,
            ) -> Response:
                if (
                    self.used
                    or method != "POST"
                    or url != "https://sts.googleapis.com/v1/token"
                    or kwargs
                ):
                    raise ValueError("unsupported credential exchange")
                self.used = True
                return Result(
                    self.client.request(
                        method, url, content=body, headers=headers, follow_redirects=False
                    )
                )

        with httpx.Client(
            trust_env=False, timeout=15.0, verify=isolated_ssl_context(), proxy=None
        ) as client:
            restricted.refresh(Exchange(client))
        result = credential(restricted.token)
        actual_expiry = restricted.expiry
        if (
            result == token
            or not isinstance(actual_expiry, datetime)
            or min(actual_expiry.replace(tzinfo=UTC), expires_at)
            <= retained_until + timedelta(seconds=30)
        ):
            raise ValueError("invalid downscoped credential lifetime")
        return result
    except Exception:
        raise ProviderError(
            "permission", retryable=False, native_code="gcs_downscoping_failed"
        ) from None
