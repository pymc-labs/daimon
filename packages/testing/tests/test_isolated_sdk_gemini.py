"""Real pinned Gemini SDK boundaries with only synthetic auth and HTTP replies."""

from __future__ import annotations

# Deliberate SDK/client identity probes; no production code uses these fields.
# pyright: reportPrivateUsage=false
from collections.abc import Iterator
from pathlib import Path
from ssl import SSLContext
from typing import NoReturn
from unittest.mock import patch

import httpx
import pytest
from daimon.testing.isolated_sdk import (
    SDK_ENV_VARS,
    TLS_PROXY_ENV_VARS,
    AmbientSDKConfigurationError,
    build_gemini_client,
)
from google import auth
from google.genai import _base_url, _interactions, errors

GOOGLE_NAMES = tuple(n for n in SDK_ENV_VARS if n.startswith(("GOOGLE_", "GEMINI_")))
FUTURE_NAMES = (
    "GOOGLE_FUTURE_ROUTE",
    "GOOGLE_FUTURE_CREDENTIAL",
    "GEMINI_FUTURE_HEADER",
    "google_future_route",
    "GOOGLE_NAME_CONTAINS_PRIVATE_SENTINEL",
)


async def test_invented_keylog_path_refuses_before_tls_context_or_file_creation(
    tmp_path: Path,
) -> None:
    import os

    target = tmp_path / "invented-tls-session-keys.log"
    client = None
    calls: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    # Replace the entire environment with an invented owned path. No operator
    # value is read and no handshake/request is needed to reproduce this bug.
    with (
        patch.object(os, "environ", {"SSLKEYLOGFILE": str(target)}),
        patch("httpx.create_ssl_context", wraps=httpx.create_ssl_context) as tls,
    ):
        try:
            with pytest.raises(AmbientSDKConfigurationError):
                client = build_gemini_client(transport=httpx.MockTransport(reply))
        finally:
            if client is not None:
                client.close()
                await client.aio.aclose()
    tls.assert_not_called()
    assert not target.exists()
    assert calls == []


@pytest.fixture(autouse=True)
def clean_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import os

    for name in tuple(os.environ):
        if (
            name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
            or name.upper() in TLS_PROXY_ENV_VARS
        ):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.mark.parametrize("name", (*GOOGLE_NAMES, *FUTURE_NAMES))
@pytest.mark.parametrize("value", ["", "private-test-sentinel"])
def test_google_names_refuse_before_client_transport_or_credentials(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    with (
        patch("google.genai.Client") as sdk,
        patch("httpx.Client") as synchronous,
        patch("httpx.AsyncClient") as asynchronous,
        patch("google.auth.default") as credentials,
        pytest.raises(AmbientSDKConfigurationError) as error,
    ):
        build_gemini_client(transport=transport, api_key="owned-test-key")
    sdk.assert_not_called()
    synchronous.assert_not_called()
    asynchronous.assert_not_called()
    credentials.assert_not_called()
    assert "private-test-sentinel" not in str(error.value)
    if name in FUTURE_NAMES:
        assert name not in str(error.value)


async def test_all_four_sdk_boundaries_keep_clean_tls_despite_later_ambient_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    generic = {
        "HTTP_PROXY": "http://unusable.invalid:1",
        "HTTPS_PROXY": "http://unusable.invalid:1",
        "ALL_PROXY": "http://unusable.invalid:1",
        "http_proxy": "http://unusable.invalid:1",
        "https_proxy": "http://unusable.invalid:1",
        "all_proxy": "http://unusable.invalid:1",
        "NO_PROXY": "owned.invalid",
        "SSL_CERT_FILE": "/missing/isolated-test-cert",
        "SSL_CERT_DIR": "/missing/isolated-test-certs",
        "SSLKEYLOGFILE": str(tmp_path / "invented-late-keylog.log"),
        "REQUESTS_CA_BUNDLE": "/missing/isolated-test-ca",
        "CURL_CA_BUNDLE": "/missing/isolated-test-curl-ca",
    }

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("isolated client consulted ambient proxy/auth settings")

    monkeypatch.setattr(auth, "default", forbidden)
    monkeypatch.setattr("httpx._client.get_environment_proxies", forbidden)
    monkeypatch.setattr(_base_url, "_default_base_gemini_url", "https://unowned.invalid")
    calls: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/interactions/" in request.url.path:
            return httpx.Response(200, json={"id": "owned", "status": "completed", "outputs": []})
        return httpx.Response(200, json={"models": []})

    headers = {"X-Explicit-Test": "owned"}
    client = build_gemini_client(
        transport=httpx.MockTransport(reply),
        api_key="owned-test-key",
        base_url="https://owned.invalid",
        headers=headers,
    )
    try:
        api = client._api_client
        assert api._http_options.client_args is not None
        assert api._http_options.async_client_args is not None
        context = api._http_options.client_args["verify"]
        assert isinstance(context, SSLContext) and context.keylog_filename is None
        assert api._http_options.async_client_args["verify"] is context
        assert api._http_options.async_client_args["ssl"] is context
        # Lazy Interactions clients and their copies must keep the original
        # explicitly owned configuration after the process environment changes.
        for name, value in generic.items():
            monkeypatch.setenv(name, value)
        for name in GOOGLE_NAMES:
            monkeypatch.setenv(name, "late-unowned-sentinel")
        list(client.models.list())
        await client.aio.models.list()
        client.interactions.get("owned")
        await client.aio.interactions.get("owned")
        client.interactions._client.with_options().interactions.get("owned")
        await client.aio.interactions._client.with_options().interactions.get("owned")
        assert len(calls) == 6
        for request in calls:
            assert request.url.host == "owned.invalid"
            assert request.headers["x-goog-api-key"] == "owned-test-key"
            assert request.headers["X-Explicit-Test"] == "owned"
            assert "authorization" not in request.headers
            assert "x-goog-user-project" not in request.headers
            assert "late-unowned-sentinel" not in str(request.url)
            assert "late-unowned-sentinel" not in str(request.headers)
        assert api.vertexai is False and api.project is None and api.location is None
        assert api._credentials is None and api._use_aiohttp() is False
        assert api._httpx_client is not None and api._httpx_client._trust_env is False
        assert api._async_httpx_client is not None and api._async_httpx_client._trust_env is False
        assert client.interactions._client._client is api._httpx_client
        assert client.aio.interactions._client._client is api._async_httpx_client
        assert headers == {"X-Explicit-Test": "owned"}
        assert all(os.environ[name] == value for name, value in generic.items())
        assert all(os.environ[name] == "late-unowned-sentinel" for name in GOOGLE_NAMES)
        assert context.keylog_filename is None
        assert not Path(generic["SSLKEYLOGFILE"]).exists()
    finally:
        client.close()
        await client.aio.aclose()
    assert api._httpx_client.is_closed and api._async_httpx_client.is_closed


@pytest.mark.parametrize(
    "boundary", ["models_sync", "models_async", "interactions_sync", "interactions_async"]
)
@pytest.mark.parametrize("status", [503, 307])
async def test_transient_and_redirect_responses_never_retry_or_change_route(
    boundary: str,
    status: int,
) -> None:
    calls: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://unowned.invalid/redirect"},
            json={"error": {"code": status, "message": "synthetic failure"}},
        )

    client = build_gemini_client(
        transport=httpx.MockTransport(reply),
        api_key="owned-test-key",
        base_url="https://owned.invalid",
    )
    try:
        rejected = False
        try:
            if boundary == "models_sync":
                list(client.models.list())
            elif boundary == "models_async":
                await client.aio.models.list()
            elif boundary == "interactions_sync":
                client.interactions.get("owned")
            else:
                await client.aio.interactions.get("owned")
        except (errors.APIError, _interactions.APIStatusError):
            rejected = True
        if status == 503:
            assert rejected
        assert len(calls) == 1 and calls[0].url.host == "owned.invalid"
    finally:
        client.close()
        await client.aio.aclose()
