"""Actual product SDK isolation, names-only refusal and owned HTTP policy."""

from __future__ import annotations

import os
import ssl
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast

import httpx
import pytest
from mux.drivers.openai import deployment
from mux.drivers.openai.transport import SDKTransport
from openai import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT, AsyncOpenAI, __version__


class NamesOnly(Mapping[str, str]):
    """Any present-value read is a failure, even membership on this mapping."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._items = dict(values)
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, name: str) -> str:
        if name in self._items:
            self.reads.append(name)
            raise AssertionError("present environment value was read")
        raise KeyError(name)


_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_BASE_URL",
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_UNKNOWN_SECRET_TOKEN",
    "oPeNaI_fUtUrE_sEcReT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSLKEYLOGFILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "sslkeylogfile",
    "hTtPs_PrOxY",
)


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("value", ["", "synthetic-never-read"])
@pytest.mark.parametrize("step", ["factory", "request", "multipart", "stream", "download"])
async def test_overrides_refused_by_name_before_any_http_or_sdk_allocation(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, step: str
) -> None:
    monkeypatch.setattr(os, "environ", {})
    transport = deployment.configured_transport(api_key="selected-key", project="selected-project")
    env = NamesOnly({name: value})
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", env)

        def forbidden(*args: object, **kwargs: object) -> object:
            pytest.fail("refused override reached HTTP or SDK construction")

        monkeypatch.setattr(deployment, "_http_transport", forbidden)
        monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
        with pytest.raises(ValueError, match="ambient OpenAI") as error:
            if step == "factory":
                deployment.configured_transport(api_key="selected-key", project="selected-project")
            elif step == "request":
                await transport.request("POST", "/agents/sessions", body={}, key="one-attempt")
            elif step == "multipart":
                await transport.multipart("/files", files=(), fields={}, key="one-attempt")
            elif step == "stream":
                await transport.open_stream("/agents/sessions/native/events")
            else:
                await transport.download("/files/native/content")
        assert env.reads == []
        assert value == "" or value not in str(error.value)
        assert "UNKNOWN_SECRET_TOKEN" not in str(error.value)
        assert "fUtUrE_sEcReT" not in str(error.value)


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("value", ["", "synthetic-never-read"])
@pytest.mark.parametrize("step", ["copy", "with_options", "transport"])
async def test_actual_sdk_copies_refuse_late_overrides_before_parent_constructor(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, step: str
) -> None:
    monkeypatch.setattr(os, "environ", {})
    requests: list[httpx.Request] = []

    def forbidden_io(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        pytest.fail("SDK copy refusal reached native I/O")

    monkeypatch.setattr(deployment, "_http_transport", lambda: httpx.MockTransport(forbidden_io))
    client = deployment._ConfiguredTransport("selected-key", "selected-project")._client()  # pyright: ignore[reportPrivateUsage]
    env = NamesOnly({name: value})
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", env)

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("SDK copy reached inherited initialization")

        monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
        try:
            with pytest.raises(ValueError, match="ambient OpenAI"):
                if step == "copy":
                    client.copy()
                elif step == "with_options":
                    client.with_options(max_retries=0)
                else:
                    SDKTransport(client)
            assert env.reads == [] and requests == []
        finally:
            await client.close()
        assert client.is_closed()


async def test_actual_sdk_retains_selected_identity_and_copy_wire_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "environ", {})
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "selected"})

    monkeypatch.setattr(deployment, "_http_transport", lambda: httpx.MockTransport(handle))
    client = deployment._ConfiguredTransport("selected-key", "selected-project")._client()  # pyright: ignore[reportPrivateUsage]
    try:
        copy = client.with_options(max_retries=0)
        assert copy.api_key == client.api_key == "selected-key"
        assert copy.project == client.project == "selected-project"
        assert copy.organization == client.organization == ""
        assert str(copy.base_url) == str(client.base_url) == "https://api.openai.com/v1/"
        assert copy.admin_api_key is None and copy.webhook_secret is None
        assert copy.workload_identity is None and copy.websocket_base_url is None
        assert copy.max_retries == 0 and copy.timeout == DEFAULT_TIMEOUT
        assert copy.user_agent == f"AsyncOpenAI/Python {__version__}"
        assert await SDKTransport(client).request("GET", "/agents/selected") == {"id": "selected"}
        assert len(requests) == 1
        request = requests[0]
        assert request.headers["Authorization"] == "Bearer selected-key"
        assert request.headers["OpenAI-Project"] == "selected-project"
        assert request.headers.get("OpenAI-Organization", "") == ""
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        assert request.url.host == "api.openai.com" and not request.url.query
        assert request.headers["User-Agent"] == client.user_agent
    finally:
        await client.close()
    assert copy.is_closed() and client.is_closed()


async def test_no_project_sends_no_project_header_and_a_blank_one_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None leaves the key's own default project; only None omits it."""
    monkeypatch.setattr(os, "environ", {})
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "selected"})

    monkeypatch.setattr(deployment, "_http_transport", lambda: httpx.MockTransport(handle))
    with pytest.raises(ValueError, match="project"):
        deployment.configured_transport(api_key="selected-key", project=" ")
    deployment.configured_transport(api_key="selected-key", project=None)
    client = deployment._ConfiguredTransport("selected-key", None)._client()  # pyright: ignore[reportPrivateUsage]
    try:
        assert await SDKTransport(client).request("GET", "/agents/selected") == {"id": "selected"}
        assert "OpenAI-Project" not in requests[0].headers
    finally:
        await client.close()


async def test_owned_real_http_policy_keeps_verified_tls_without_key_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Construct the actual HTTP and SDK clients, without opening any connection.
    env = NamesOnly({})
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", env)
        contexts: list[ssl.SSLContext] = []
        policies: list[dict[str, object]] = []
        original_context = httpx.create_ssl_context
        original_transport = httpx.AsyncHTTPTransport

        def context(*, trust_env: bool) -> ssl.SSLContext:
            assert trust_env is False
            result = original_context(trust_env=trust_env)
            contexts.append(result)
            return result

        def transport(**kwargs: object) -> httpx.AsyncBaseTransport:
            policies.append(kwargs)
            return original_transport(
                verify=cast(ssl.SSLContext, kwargs["verify"]),
                trust_env=cast(bool, kwargs["trust_env"]),
                proxy=None,
                limits=cast(httpx.Limits, kwargs["limits"]),
                retries=cast(int, kwargs["retries"]),
            )

        monkeypatch.setattr(httpx, "create_ssl_context", context)
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
        client = deployment._ConfiguredTransport("selected-key", "selected-project")._client()  # pyright: ignore[reportPrivateUsage]
        try:
            assert env.reads == [] and len(contexts) == len(policies) == 1
            assert contexts[0].verify_mode == ssl.CERT_REQUIRED and contexts[0].check_hostname
            assert contexts[0].keylog_filename is None
            assert policies[0] == {
                "verify": contexts[0],
                "trust_env": False,
                "proxy": None,
                "limits": DEFAULT_CONNECTION_LIMITS,
                "retries": 0,
            }
            assert client.timeout == DEFAULT_TIMEOUT
            http = client._client  # pyright: ignore[reportPrivateUsage]
            assert http.trust_env is False and http.follow_redirects
        finally:
            await client.close()
        assert client.is_closed()


@pytest.mark.parametrize(
    "step",
    [
        "factory",
        "client",
        "http",
        "request",
        "multipart",
        "stream",
        "download",
        "copy",
        "with_options",
    ],
)
async def test_invented_keylog_path_refuses_before_file_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, step: str
) -> None:
    monkeypatch.setattr(os, "environ", {})
    path = tmp_path / "invented-tls-session-keys.log"
    transport = deployment.configured_transport(api_key="selected-key", project="selected-project")
    client: AsyncOpenAI | None = None
    if step in ("copy", "with_options"):
        # This is the actual complete product SDK/HTTP constructor, with no
        # connection opened. Late copy refusal must preserve owned cleanup.
        client = deployment._ConfiguredTransport("selected-key", "selected-project")._client()  # pyright: ignore[reportPrivateUsage]
    env = {"SSLKEYLOGFILE": str(path)}
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", env)
        try:
            with pytest.raises(
                ValueError, match="^ambient OpenAI TLS/proxy configuration is unsupported$"
            ) as error:
                if step == "factory":
                    deployment.configured_transport(
                        api_key="selected-key", project="selected-project"
                    )
                elif step == "client":
                    deployment._ConfiguredTransport("selected-key", "selected-project")._client()  # pyright: ignore[reportPrivateUsage]
                elif step == "http":
                    deployment._http_transport()  # pyright: ignore[reportPrivateUsage]
                elif step == "request":
                    await transport.request("POST", "/agents/sessions", body={}, key="one-attempt")
                elif step == "multipart":
                    await transport.multipart("/files", files=(), fields={}, key="one-attempt")
                elif step == "stream":
                    await transport.open_stream("/agents/sessions/native/events")
                elif step == "download":
                    await transport.download("/files/native/content")
                else:
                    assert client is not None
                    if step == "copy":
                        client.copy()
                    else:
                        client.with_options(max_retries=0)
            assert not path.exists()
            assert env == {"SSLKEYLOGFILE": str(path)}  # operator settings are preserved
            assert str(path) not in str(error.value)
        finally:
            if client is not None:
                await client.close()
        assert client is None or client.is_closed()
