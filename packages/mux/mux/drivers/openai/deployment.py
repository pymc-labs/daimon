"""Explicit request-owned SDK transport; credentials are supplied by the host."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import IO, Self, cast

import httpx
from openai import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT, AsyncOpenAI

from mux.drivers import isolated_ssl_context
from mux.drivers.openai.transport import Method, Object, SDKTransport, Transport

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


def _check_environment() -> None:
    # Mapping membership reads values on os.environ. Inspect names only, including
    # empty and future SDK settings; never include unknown names in diagnostics.
    names = frozenset(os.environ)
    if "OPENAI_CUSTOM_HEADERS" in names:
        raise ValueError("ambient OpenAI custom headers are unsupported")
    if any(name.upper().startswith("OPENAI_") for name in names):
        raise ValueError("ambient OpenAI configuration is unsupported")
    if any(name.upper() in _TLS_PROXY_ENV for name in names):
        raise ValueError("ambient OpenAI TLS/proxy configuration is unsupported")


class _DeploymentAsyncOpenAI(AsyncOpenAI):
    # SDK copy/with_options reconstruct self.__class__. Refuse late overrides
    # before allocation and inherited initialization can consult their values.
    def __new__(cls, *args: object, **kwargs: object) -> Self:  # pyright: ignore[reportInconsistentConstructor]
        _check_environment()
        return super().__new__(cls)

    @property
    def user_agent(self) -> str:
        # Keep the original SDK wire header despite the guarded subclass.
        return super().user_agent.replace(type(self).__name__, AsyncOpenAI.__name__, 1)


def _http_transport() -> httpx.AsyncBaseTransport:
    # Python's default TLS context also discovers SSLKEYLOGFILE, even when
    # httpx trust_env=False. Refuse overrides before context construction.
    _check_environment()
    # Explicit TLS roots and no proxy discovery, including SSL_CERT_FILE/DIR.
    # Preserve SDK connection limits, timeout and redirect policy below.
    return httpx.AsyncHTTPTransport(
        verify=isolated_ssl_context(),
        trust_env=False,
        proxy=None,
        limits=DEFAULT_CONNECTION_LIMITS,
        retries=0,
    )


class _OwnedStream[T](AsyncIterator[T]):
    def __init__(self, source: AsyncIterator[T], client: AsyncOpenAI) -> None:
        self._source, self._client = source, client
        self._closed = False

    def __aiter__(self) -> _OwnedStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._source.__anext__()
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
            await self._client.close()


class _ConfiguredTransport:
    def __init__(self, api_key: str, project: str | None) -> None:
        self._api_key, self._project = api_key, project

    def _client(self) -> AsyncOpenAI:
        # Explicit origin/project/key override SDK environment discovery. The
        # credential is not a client-global singleton shared between callers.
        _check_environment()
        return _DeploymentAsyncOpenAI(
            api_key=self._api_key,
            admin_api_key=None,
            workload_identity=None,
            webhook_secret=None,
            provider=None,
            websocket_base_url=None,
            default_headers={},
            default_query={},
            project=self._project,
            organization="",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=DEFAULT_TIMEOUT,
            http_client=httpx.AsyncClient(
                transport=_http_transport(),
                trust_env=False,
                timeout=DEFAULT_TIMEOUT,
                limits=DEFAULT_CONNECTION_LIMITS,
                follow_redirects=True,
            ),
        )

    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).request(method, path, body=body, query=query, key=key)

    async def open_stream(self, path: str) -> AsyncIterator[Object]:
        client = self._client()
        try:
            source = await SDKTransport(client).open_stream(path)
        except BaseException:
            await client.close()
            raise
        return _OwnedStream(source, client)

    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> Object:
        async with self._client() as client:
            return await SDKTransport(client).multipart(path, files=files, fields=fields, key=key)

    async def download(self, path: str) -> AsyncIterator[bytes]:
        client = self._client()
        try:
            source = await SDKTransport(client).download(path)
        except BaseException:
            await client.close()
            raise
        return _OwnedStream(source, client)


def configured_transport(*, api_key: str, project: str | None) -> Transport:
    """No I/O at construction; each request/stream owns its SDK client lifetime.

    `project=None` sends no OpenAI-Project header, so the key's own default project
    applies; ambient `OPENAI_*` settings are refused either way. A blank project is
    still refused: None is the only way to omit it.
    """
    _check_environment()
    if not api_key.strip() or (project is not None and not project.strip()):
        raise ValueError("explicit OpenAI credential and project are required")
    return _ConfiguredTransport(api_key, project)
