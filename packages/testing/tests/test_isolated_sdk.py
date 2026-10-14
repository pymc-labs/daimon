"""Ambient configuration refuses construction before any SDK/client or I/O."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from unittest.mock import patch

import httpx
import pytest
from daimon.testing.isolated_sdk import (
    SDK_ENV_VARS,
    TEST_API_KEY,
    TLS_PROXY_ENV_VARS,
    AmbientSDKConfigurationError,
    build_async_anthropic,
    build_async_openai,
    build_gemini_client,
)

FACTORIES = (build_async_openai, build_async_anthropic, build_gemini_client)
UNKNOWN_PROVIDER_NAMES = tuple(
    prefix + "PRIVATE_FUTURE_SETTING"
    for prefix in (
        "ANTHROPIC_",
        "OPENAI_",
        "GOOGLE_",
        "GEMINI_",
        "anthropic_",
        "openai_",
        "google_",
        "gemini_",
        "Anthropic_",
        "OpenAI_",
        "Google_",
        "Gemini_",
    )
)
TLS_PROXY_CASE_NAMES = tuple(
    variant for name in TLS_PROXY_ENV_VARS for variant in (name.lower(), name.title())
)
REFUSED_ENV_NAMES = (*SDK_ENV_VARS, *UNKNOWN_PROVIDER_NAMES, *TLS_PROXY_CASE_NAMES)


def _environment_snapshot() -> str:
    """Compare environment changes without exposing values in failed asserts."""
    return hashlib.sha256(json.dumps(sorted(os.environ.items())).encode()).hexdigest()


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in tuple(os.environ):
        if (
            name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
            or name.upper() in TLS_PROXY_ENV_VARS
        ):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("name", REFUSED_ENV_NAMES)
@pytest.mark.parametrize(
    "value", ["", "offline-secret-do-not-log", "X-Poison: sentinel\nX-Other: value"]
)
def test_every_provider_variable_refuses_each_factory_before_construction(
    monkeypatch: pytest.MonkeyPatch, factory, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    before = _environment_snapshot()
    with (
        patch("httpx.AsyncClient") as asynchronous,
        patch("httpx.Client") as synchronous,
        patch("httpx.create_ssl_context") as tls,
        pytest.raises(AmbientSDKConfigurationError) as error,
    ):
        factory(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    if name in SDK_ENV_VARS:
        assert name in str(error.value)
    else:
        assert name not in str(error.value)
    if value:
        assert value not in str(error.value)
    assert _environment_snapshot() == before
    synchronous.assert_not_called()
    asynchronous.assert_not_called()
    tls.assert_not_called()


def test_multiple_variables_report_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "hidden-admin")
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "hidden-webhook")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "hidden-project")
    with pytest.raises(AmbientSDKConfigurationError) as error:
        build_async_openai(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    message = str(error.value)
    assert all(
        name in message
        for name in ("OPENAI_ADMIN_KEY", "ANTHROPIC_WEBHOOK_SIGNING_KEY", "GOOGLE_CLOUD_PROJECT")
    )
    assert "hidden-" not in message


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", None),
        ("api_key", ""),
        ("api_key", " "),
        ("api_key", lambda: "credential-provider"),
        ("base_url", None),
        ("base_url", ""),
        ("base_url", " "),
        ("transport", None),
    ],
)
def test_missing_explicit_auth_route_or_transport_never_falls_back(factory, field, value) -> None:
    kwargs = {"transport": httpx.MockTransport(lambda _: httpx.Response(500)), field: value}
    with (
        patch("httpx.AsyncClient") as asynchronous,
        patch("httpx.Client") as synchronous,
        pytest.raises(ValueError),
    ):
        factory(**kwargs)
    synchronous.assert_not_called()
    asynchronous.assert_not_called()


@pytest.mark.parametrize("factory", [build_async_openai, build_async_anthropic])
async def test_async_sdk_requests_have_explicit_auth_route_headers_and_no_ambient_scope(
    factory,
) -> None:
    calls: list[httpx.Request] = []
    headers = {"X-Explicit-Test": "owned"}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    client = factory(
        transport=httpx.MockTransport(handle),
        api_key="owned-test-key",
        base_url="https://owned.invalid",
        headers=headers,
    )
    try:
        await client.models.list()
        assert len(calls) == 1 and calls[0].url.host == "owned.invalid"
        assert calls[0].headers["X-Explicit-Test"] == "owned"
        assert not any(
            h in calls[0].headers
            for h in ("OpenAI-Organization", "OpenAI-Project", "Authorization-Admin", "X-Poison")
        )
        assert client._client._trust_env is False
        assert client._client.params == httpx.QueryParams()
        assert client.max_retries == 0
        assert headers == {"X-Explicit-Test": "owned"}
        if factory is build_async_openai:
            assert calls[0].headers["Authorization"] == "Bearer owned-test-key"
            assert client.organization is client.project is client.admin_api_key is None
            assert client.webhook_secret is client.workload_identity is None
        else:
            assert calls[0].headers["X-Api-Key"] == "owned-test-key"
            assert "Authorization" not in calls[0].headers
            assert client.auth_token is client.credentials is client.webhook_key is None
    finally:
        await client.close()
    assert client.is_closed()


async def test_gemini_sync_async_models_and_interactions_use_only_owned_http_clients() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "interactions" in request.url.path:
            return httpx.Response(
                200, json={"id": "owned-interaction", "status": "completed", "outputs": []}
            )
        return httpx.Response(200, json={"models": []})

    headers = {"X-Explicit-Test": "owned"}
    client = build_gemini_client(
        transport=httpx.MockTransport(handle),
        api_key="owned-test-key",
        base_url="https://owned.invalid",
        headers=headers,
    )
    try:
        list(client.models.list())
        await client.aio.models.list()
        client.interactions.get("owned-interaction")
        await client.aio.interactions.get("owned-interaction")
        assert len(calls) == 4
        for request in calls:
            assert request.url.host == "owned.invalid"
            assert request.headers["x-goog-api-key"] == "owned-test-key"
            assert request.headers["X-Explicit-Test"] == "owned"
            assert not any(
                h in request.headers for h in ("Authorization", "x-goog-user-project", "X-Poison")
            )
        assert headers == {"X-Explicit-Test": "owned"}
        assert client._api_client._httpx_client._trust_env is False
        assert client._api_client._async_httpx_client._trust_env is False
        assert client._api_client.vertexai is False
        assert client._api_client.project is client._api_client.location is None
        assert client._api_client._credentials is None
    finally:
        client.close()
        await client.aio.aclose()


@pytest.mark.parametrize("factory", FACTORIES)
async def test_default_test_key_empty_custom_headers_and_no_environment_mutation(factory) -> None:
    before = _environment_snapshot()
    client = factory(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"models": [], "data": []})
        )
    )
    try:
        assert _environment_snapshot() == before
        if factory is build_gemini_client:
            assert client._api_client.api_key == TEST_API_KEY
            assert "X-Poison" not in client._api_client._http_options.headers
        else:
            assert client.api_key == TEST_API_KEY and client._custom_headers == {}
    finally:
        if factory is build_gemini_client:
            client.close()
            await client.aio.aclose()
        else:
            await client.close()


def test_gemini_single_protocol_transport_requires_both_boundaries() -> None:
    class SyncOnly(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("construction must not make requests")

    with pytest.raises(ValueError, match="async test transport"):
        build_gemini_client(transport=SyncOnly())


@pytest.mark.parametrize("factory", FACTORIES)
async def test_transient_response_is_not_automatically_retried(factory) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503, json={"error": {"message": "offline failure", "type": "api_error", "code": 503}}
        )

    import anthropic
    import openai
    from google.genai import _interactions

    expected = (
        _interactions.InternalServerError
        if factory is build_gemini_client
        else openai.InternalServerError
        if factory is build_async_openai
        else anthropic.InternalServerError
    )
    client = factory(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(expected):
            if factory is build_gemini_client:
                await client.aio.interactions.get("owned-interaction")
            else:
                await client.models.list()
        assert len(calls) == 1
    finally:
        if factory is build_gemini_client:
            client.close()
            await client.aio.aclose()
        else:
            await client.close()


@pytest.mark.parametrize("entrypoint", ["fake", "stub", "scripted", "no_retry"])
@pytest.mark.parametrize("name", SDK_ENV_VARS)
@pytest.mark.parametrize("value", ["", "ambient-secret-do-not-log"])
def test_shared_anthropic_builders_refuse_ambient_configuration(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, name: str, value: str
) -> None:
    from daimon.testing.ma import (
        build_fake_anthropic,
        build_no_retry_anthropic,
        build_stub_anthropic,
    )
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    monkeypatch.setenv(name, value)
    script = ScriptedTransport()
    script.queue(ScriptedReply("GET", "/v1/models", httpx.Response(200, json={})))
    before = _environment_snapshot()
    with (
        patch("httpx.AsyncClient") as asynchronous,
        pytest.raises(AmbientSDKConfigurationError) as error,
    ):
        if entrypoint == "fake":
            build_fake_anthropic(script.dispatch)
        elif entrypoint == "stub":
            build_stub_anthropic(script.dispatch)
        elif entrypoint == "no_retry":
            build_no_retry_anthropic(script.dispatch)
        else:
            script.client()
    asynchronous.assert_not_called()
    assert not script.requests and not script.violations and len(script.replies) == 1
    assert _environment_snapshot() == before
    assert name in str(error.value)
    if value:
        assert value not in str(error.value)


async def test_shared_anthropic_builder_preserves_original_wire_and_sdk_defaults() -> None:
    from anthropic import DEFAULT_TIMEOUT, AsyncAnthropic
    from daimon.testing.ma import build_fake_anthropic

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [], "has_more": False})

    # Independent baseline: the exact previous builder on a clean environment.
    legacy = AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="https://api.anthropic.com"
        ),
    )
    isolated = build_fake_anthropic(handle)
    try:
        assert isolated.max_retries == legacy.max_retries == 2
        assert isolated.timeout == legacy.timeout == DEFAULT_TIMEOUT
        assert isolated._client._trust_env is False
        assert isolated.auth_token is isolated.credentials is isolated.webhook_key is None
        await legacy.models.list()
        await isolated.models.list()
        old, new = calls
        assert (new.method, new.url, new.content, new.headers) == (
            old.method,
            old.url,
            old.content,
            old.headers,
        )
    finally:
        await legacy.close()
        await isolated.close()


async def test_no_retry_builder_preserves_original_wire_timeout_and_explicit_auth() -> None:
    from anthropic import DEFAULT_TIMEOUT, AsyncAnthropic
    from daimon.testing.ma import build_no_retry_anthropic

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [], "has_more": False})

    # Independent clean baseline: the pre-fix no-retry builder's exact body.
    legacy = AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="https://api.anthropic.com"
        ),
        max_retries=0,
    )
    isolated = build_no_retry_anthropic(handle)
    try:
        assert isolated.max_retries == legacy.max_retries == 0
        assert isolated.timeout == legacy.timeout == DEFAULT_TIMEOUT
        assert isolated._client._trust_env is False
        assert isolated.auth_token is isolated.credentials is isolated.webhook_key is None
        assert isolated._custom_headers == {}
        await legacy.models.list()
        await isolated.models.list()
        old, new = calls
        assert (new.method, new.url, new.content, new.headers) == (
            old.method,
            old.url,
            old.content,
            old.headers,
        )
    finally:
        await legacy.close()
        await isolated.close()


@pytest.mark.parametrize("status", [409, 429, 500, 503])
@pytest.mark.parametrize("entrypoint", ["handler", "router"])
async def test_no_retry_builder_never_consumes_the_next_scripted_response(
    status: int, entrypoint: str
) -> None:
    from anthropic import APIStatusError
    from daimon.testing.ma import MARouter, build_no_retry_anthropic
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    script = ScriptedTransport()
    script.queue(
        ScriptedReply(
            "GET",
            "/v1/models",
            httpx.Response(
                status,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "api_error", "message": "scripted"}},
            ),
        ),
        ScriptedReply("GET", "/v1/models", httpx.Response(200, json={"data": []})),
    )
    router = MARouter()
    router.add("GET", r"/v1/models", lambda request, _match: script.dispatch(request))
    async with build_no_retry_anthropic(
        router if entrypoint == "router" else script.dispatch
    ) as client:
        with pytest.raises(APIStatusError) as error:
            await client.models.list()
        assert error.value.status_code == status
        assert client.max_retries == 0
    assert len(script.requests) == 1 and len(script.replies) == 1
    assert not script.violations


@pytest.mark.parametrize("max_retries", [0, 2, 8])
async def test_scripted_client_preserves_explicit_retry_budget(max_retries: int) -> None:
    from anthropic import DEFAULT_TIMEOUT
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    script = ScriptedTransport()
    script.queue(ScriptedReply("GET", "/v1/models", httpx.Response(200, json={"data": []})))
    async with script.client(max_retries=max_retries) as client:
        assert client.max_retries == max_retries
        assert client.timeout == DEFAULT_TIMEOUT
        assert client._client._trust_env is False
        await client.models.list()
    script.assert_consumed()


@pytest.mark.parametrize(
    "boundary", ["sync_models", "async_models", "sync_interactions", "async_interactions"]
)
async def test_all_gemini_boundaries_make_one_attempt_on_transient_failure(boundary: str) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"error": {"message": "offline failure", "code": 503}})

    from google.genai import _interactions, errors

    expected = errors.ServerError if "models" in boundary else _interactions.InternalServerError
    client = build_gemini_client(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(expected):
            if boundary == "sync_models":
                list(client.models.list())
            elif boundary == "async_models":
                await client.aio.models.list()
            elif boundary == "sync_interactions":
                client.interactions.get("owned-interaction")
            else:
                await client.aio.interactions.get("owned-interaction")
        assert len(calls) == 1
    finally:
        client.close()
        await client.aio.aclose()


async def test_anthropic_explicit_test_key_does_not_load_profile_credentials() -> None:
    with patch(
        "anthropic._client.default_credentials", side_effect=AssertionError("credential chain")
    ) as credentials:
        client = build_async_anthropic(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        )
        await client.close()
    credentials.assert_not_called()


class _NamesOnlyEnvironment(Mapping[str, str]):
    """Mapping membership has os.environ's value-fetching semantics."""

    def __init__(self, *names: str) -> None:
        self.names = names
        self.value_reads = 0

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __getitem__(self, name: str) -> str:
        self.value_reads += 1
        raise AssertionError("guard fetched an environment value")


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("name", REFUSED_ENV_NAMES)
def test_environment_preflight_reads_names_only_before_any_sdk_construction(
    monkeypatch: pytest.MonkeyPatch, factory, name: str
) -> None:
    # Inherited Mapping.__contains__ calls __getitem__, just like os.environ.
    # Names are visible; reading even one value is forbidden in this probe.
    environment = _NamesOnlyEnvironment(name)
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    with (
        patch("openai.AsyncOpenAI") as openai_sdk,
        patch("anthropic.AsyncAnthropic") as anthropic_sdk,
        patch("google.genai.Client") as gemini_sdk,
        patch("httpx.Client") as synchronous,
        patch("httpx.AsyncClient") as asynchronous,
        patch("httpx.create_ssl_context") as tls,
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(os, "environ", environment)
            with pytest.raises(AmbientSDKConfigurationError) as error:
                factory(transport=transport, api_key="explicit-test-key")
        if name in SDK_ENV_VARS:
            assert name in str(error.value)
        else:
            assert name not in str(error.value)
    assert environment.value_reads == 0
    openai_sdk.assert_not_called()
    anthropic_sdk.assert_not_called()
    gemini_sdk.assert_not_called()
    synchronous.assert_not_called()
    asynchronous.assert_not_called()
    tls.assert_not_called()


@pytest.mark.parametrize("name", ["GOOGLE_FUTURE_ROUTE", "gemini_future_secret_name"])
def test_unknown_gemini_setting_refuses_without_reading_values_or_exposing_name(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    environment = _NamesOnlyEnvironment(name)
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    with patch("google.genai.Client") as sdk, monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", environment)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            build_gemini_client(transport=transport)
    assert environment.value_reads == 0
    assert name not in str(error.value)
    sdk.assert_not_called()


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("name", UNKNOWN_PROVIDER_NAMES[:4])
def test_mixed_known_and_unknown_provider_names_refuse_without_value_reads_or_unknown_names(
    monkeypatch: pytest.MonkeyPatch, factory, name: str
) -> None:
    environment = _NamesOnlyEnvironment("OPENAI_ADMIN_KEY", name)
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    with (
        patch("httpx.Client") as synchronous,
        patch("httpx.AsyncClient") as asynchronous,
        monkeypatch.context() as scoped,
    ):
        scoped.setattr(os, "environ", environment)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            factory(transport=transport)
    assert name not in str(error.value)
    assert environment.value_reads == 0
    synchronous.assert_not_called()
    asynchronous.assert_not_called()


@pytest.mark.parametrize("name", ["GOOGLE_API_KEY", "GEMINI_API_KEY"])
@pytest.mark.parametrize("value", ["", "synthetic-key-never-read"])
def test_real_environment_gemini_keys_refuse_before_value_lookup_or_sdk_construction(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    environment_type = type(os.environ)
    original_getitem = environment_type.__getitem__
    reads: list[str] = []

    def guarded_getitem(environment, key: str) -> str:
        if key in SDK_ENV_VARS:
            reads.append(key)
            raise AssertionError("guard fetched an ambient provider value")
        return original_getitem(environment, key)

    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    with (
        patch("google.genai.Client") as sdk,
        patch("httpx.Client") as synchronous,
        patch("httpx.AsyncClient") as asynchronous,
        monkeypatch.context() as scoped,
    ):
        scoped.setattr(environment_type, "__getitem__", guarded_getitem)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            build_gemini_client(transport=transport, api_key="explicit-test-key")
    assert not reads
    assert name in str(error.value)
    if value:
        assert value not in str(error.value)
    sdk.assert_not_called()
    synchronous.assert_not_called()
    asynchronous.assert_not_called()


COPY_ENTRYPOINTS = ("openai", "anthropic", "fake", "stub", "scripted", "no_retry")


def _owned_copy_client(entrypoint: str, handler):
    from daimon.testing.ma import (
        MARouter,
        build_fake_anthropic,
        build_no_retry_anthropic,
        build_stub_anthropic,
    )
    from daimon.testing.ma_transport import ScriptedTransport

    if entrypoint == "openai":
        return build_async_openai(transport=httpx.MockTransport(handler))
    if entrypoint == "anthropic":
        return build_async_anthropic(transport=httpx.MockTransport(handler))
    if entrypoint == "fake":
        return build_fake_anthropic(handler)
    if entrypoint == "stub":
        return build_stub_anthropic(handler)
    if entrypoint == "no_retry":
        return build_no_retry_anthropic(handler)
    router = MARouter()
    router.add("GET", r"/v1/models", lambda request, _match: handler(request))
    return ScriptedTransport(router=router).client()


@pytest.mark.parametrize("entrypoint", COPY_ENTRYPOINTS)
@pytest.mark.parametrize("method", ["copy", "with_options"])
@pytest.mark.parametrize("name", REFUSED_ENV_NAMES)
async def test_every_owned_sdk_copy_refuses_late_environment_before_parent_construction(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, method: str, name: str
) -> None:
    client = _owned_copy_client(entrypoint, lambda _: httpx.Response(500))
    environment = _NamesOnlyEnvironment(name)
    try:
        with (
            patch(
                "openai.AsyncOpenAI.__init__", side_effect=AssertionError("parent constructor")
            ) as openai_parent,
            patch(
                "anthropic.AsyncAnthropic.__init__",
                side_effect=AssertionError("parent constructor"),
            ) as anthropic_parent,
            monkeypatch.context() as scoped,
        ):
            scoped.setattr(os, "environ", environment)
            with pytest.raises(AmbientSDKConfigurationError) as error:
                getattr(client, method)()
        if name in SDK_ENV_VARS:
            assert name in str(error.value)
        else:
            assert name not in str(error.value)
        assert environment.value_reads == 0
        openai_parent.assert_not_called()
        anthropic_parent.assert_not_called()
    finally:
        await client.close()


LATE_DRIVER_SETTINGS = (
    ("openai", "OPENAI_ADMIN_KEY", "synthetic-admin"),
    ("openai", "OPENAI_WEBHOOK_SECRET", "synthetic-webhook"),
    ("openai", "OPENAI_ORG_ID", "synthetic-org"),
    ("openai", "OPENAI_PROJECT_ID", "synthetic-project"),
    ("openai", "OPENAI_CUSTOM_HEADERS", "X-Poison: synthetic-header"),
    *(
        (entrypoint, name, value)
        for entrypoint in ("anthropic", "fake", "stub", "scripted", "no_retry")
        for name, value in (
            ("ANTHROPIC_WEBHOOK_SIGNING_KEY", "synthetic-webhook"),
            ("ANTHROPIC_CUSTOM_HEADERS", "X-Poison: synthetic-header"),
        )
    ),
)


@pytest.mark.parametrize("entrypoint,name,value", LATE_DRIVER_SETTINGS)
async def test_real_driver_copies_refuse_late_ambient_settings_without_requests(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, name: str, value: str
) -> None:
    from mux.drivers.anthropic.transport import mutation_client, single_attempt_mutation
    from mux.drivers.openai.transport import SDKTransport

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    client = _owned_copy_client(entrypoint, handle)
    retry_budget = client.max_retries
    try:
        monkeypatch.setenv(name, value)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            if entrypoint == "openai":
                SDKTransport(client)
            else:
                with single_attempt_mutation():
                    mutation_client(client)
        assert name in str(error.value) and value not in str(error.value)
        assert not calls
        assert client.max_retries == retry_budget
        assert client._custom_headers == {}
        assert client._client._trust_env is False
        if entrypoint == "openai":
            assert (
                client.admin_api_key
                is client.webhook_secret
                is client.organization
                is client.project
                is None
            )
        else:
            assert client.webhook_key is None
    finally:
        await client.close()


@pytest.mark.parametrize("entrypoint", COPY_ENTRYPOINTS)
async def test_clean_copies_preserve_native_wire_owner_and_retry_budgets(entrypoint: str) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    client = _owned_copy_client(entrypoint, handle)
    try:
        copies = (client.copy(), client.with_options())
        await client.models.list()
        for copied in copies:
            assert type(copied) is type(client)
            assert copied._client is client._client
            assert copied.max_retries == client.max_retries
            assert copied.timeout == client.timeout
            await copied.models.list()
        assert len(calls) == 3
        original = calls[0]
        for request in calls[1:]:
            assert (request.method, request.url, request.content, request.headers) == (
                original.method,
                original.url,
                original.content,
                original.headers,
            )
        native_name = "AsyncOpenAI" if entrypoint == "openai" else "AsyncAnthropic"
        assert original.headers["User-Agent"].startswith(native_name + "/Python ")
    finally:
        await client.close()


@pytest.mark.parametrize("entrypoint", COPY_ENTRYPOINTS)
async def test_clean_real_driver_copy_keeps_single_attempt_and_original_client(
    entrypoint: str,
) -> None:
    from mux.drivers.anthropic.transport import mutation_client, single_attempt_mutation
    from mux.drivers.openai.transport import SDKTransport

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    client = _owned_copy_client(entrypoint, handle)
    budget = client.max_retries
    try:
        if entrypoint == "openai":
            driver = SDKTransport(client)
            assert driver._client.max_retries == 0
            assert driver._client._client is client._client
            await driver.request("GET", "/models")
        else:
            with single_attempt_mutation():
                copied = mutation_client(client)
                assert copied.max_retries == 0 and copied._client is client._client
                await copied.models.list()
            assert mutation_client(client) is client
        assert len(calls) == 1
        assert client.max_retries == budget
    finally:
        await client.close()


@pytest.mark.parametrize("entrypoint", ["fake", "stub", "scripted", "no_retry"])
@pytest.mark.parametrize("name", REFUSED_ENV_NAMES)
def test_shared_builder_preflight_never_fetches_environment_values(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, name: str
) -> None:
    # Import the shared entrypoints before the names-only environment is active.
    from daimon.testing.ma import (
        build_fake_anthropic,
        build_no_retry_anthropic,
        build_stub_anthropic,
    )
    from daimon.testing.ma_transport import ScriptedTransport

    environment = _NamesOnlyEnvironment(name)
    with (
        patch("anthropic.AsyncAnthropic.__init__", return_value=None) as parent,
        patch("httpx.AsyncClient") as http_client,
        monkeypatch.context() as scoped,
    ):
        scoped.setattr(os, "environ", environment)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            if entrypoint == "fake":
                build_fake_anthropic(lambda _: httpx.Response(500))
            elif entrypoint == "stub":
                build_stub_anthropic()
            elif entrypoint == "no_retry":
                build_no_retry_anthropic(lambda _: httpx.Response(500))
            else:
                ScriptedTransport().client()
    if name in (*UNKNOWN_PROVIDER_NAMES, *TLS_PROXY_CASE_NAMES):
        assert name not in str(error.value)
    assert environment.value_reads == 0
    parent.assert_not_called()
    http_client.assert_not_called()
