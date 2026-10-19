"""Explicit test SDK clients: no ambient provider configuration or HTTP proxy use.

These factories own their HTTP clients; callers supply the test transport and
close the returned SDK normally. Provider SDK imports remain optional and lazy.
No factory changes the process environment or loads ambient credentials.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Self

import httpx

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from google.genai import Client
    from openai import AsyncOpenAI

# Explicit None still means "consult the environment" in several SDK fields.
# Reject presence, including empty strings, before importing/constructing an SDK.
TLS_PROXY_ENV_VARS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSLKEYLOGFILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
SDK_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_CUSTOM_HEADERS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_WEBHOOK_SIGNING_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_SCOPE",
    "GEMINI_API_KEY",
    "GEMINI_NEXT_GEN_API_BASE_URL",
    "GOOGLE_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_VERTEX_BASE_URL",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_USE_CLIENT_CERTIFICATE",
    "GOOGLE_API_USE_MTLS_ENDPOINT",
    "GOOGLE_GENAI_CLIENT_MODE",
    "GOOGLE_GENAI_REPLAYS_DIRECTORY",
    "GOOGLE_GENAI_REPLAY_ID",
    *TLS_PROXY_ENV_VARS,
)

TEST_API_KEY = "test-isolated-sdk"


class AmbientSDKConfigurationError(ValueError):
    """A test attempted SDK construction with ambient provider configuration."""


def _require_clean_environment() -> None:
    # os.environ inherits Mapping.__contains__, which calls __getitem__ and
    # reads values. Snapshot keys through iteration before membership checks.
    names = frozenset(os.environ)
    present = [name for name in SDK_ENV_VARS if name in names]
    # Future provider settings can contain secrets in their names as well as
    # values. Refuse all four prefixes, without reporting unknown names.
    if any(
        (
            name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
            or name.upper() in TLS_PROXY_ENV_VARS
        )
        and name not in SDK_ENV_VARS
        for name in names
    ):
        raise AmbientSDKConfigurationError("test SDK refuses ambient provider configuration")
    if present:
        raise AmbientSDKConfigurationError(
            "test SDK refuses ambient variables: " + ", ".join(present)
        )


def _require_explicit_configuration(api_key: object, base_url: object) -> None:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("an explicit nonempty test api_key is required")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("an explicit test base_url is required")


def _require_async_transport(transport: object) -> None:
    if not isinstance(transport, httpx.AsyncBaseTransport):
        raise ValueError("an explicit async test transport is required")


def _require_sync_transport(transport: object) -> None:
    if not isinstance(transport, httpx.BaseTransport):
        raise ValueError("an explicit sync test transport is required")


def build_async_openai(
    *,
    transport: httpx.AsyncBaseTransport,
    api_key: str = TEST_API_KEY,
    base_url: str = "https://api.openai.com/v1",
    headers: Mapping[str, str] | None = None,
) -> AsyncOpenAI:
    """Own an AsyncOpenAI client with an explicit test transport and no retries."""
    _require_clean_environment()
    _require_explicit_configuration(api_key, base_url)
    _require_async_transport(transport)
    from openai import AsyncOpenAI

    class _IsolatedAsyncOpenAI(AsyncOpenAI):
        # SDK copy/with_options reconstruct self.__class__. Guard allocation
        # before inherited __init__ can inspect ambient account/auth/headers.
        # Allocation accepts the SDK's arguments; inherited __init__ retains
        # its full validation/signature. No argument is consumed or changed.
        def __new__(cls, *args: object, **kwargs: object) -> Self:  # pyright: ignore[reportInconsistentConstructor]
            _require_clean_environment()
            return super().__new__(cls)

        @property
        def user_agent(self) -> str:
            # The SDK derives this header from the runtime class name.
            return super().user_agent.replace(type(self).__name__, AsyncOpenAI.__name__, 1)

    return _IsolatedAsyncOpenAI(
        api_key=api_key,
        admin_api_key=None,
        workload_identity=None,
        organization=None,
        project=None,
        webhook_secret=None,
        provider=None,
        base_url=base_url,
        websocket_base_url=None,
        default_headers=dict(headers or {}),
        default_query={},
        max_retries=0,
        timeout=5,
        http_client=httpx.AsyncClient(transport=transport, trust_env=False),
    )


def build_async_anthropic(
    *,
    transport: httpx.AsyncBaseTransport,
    api_key: str = TEST_API_KEY,
    base_url: str = "https://api.anthropic.com",
    headers: Mapping[str, str] | None = None,
    max_retries: int = 0,
    timeout: float | httpx.Timeout = 5,
) -> AsyncAnthropic:
    """Own an AsyncAnthropic client with explicit auth, routing and headers."""
    _require_clean_environment()
    _require_explicit_configuration(api_key, base_url)
    _require_async_transport(transport)
    from anthropic import AsyncAnthropic

    class _IsolatedAsyncAnthropic(AsyncAnthropic):
        # Preserve the SDK constructor signature and copy behavior while
        # refusing late ambient settings before any parent construction.
        # Allocation accepts the SDK's arguments; inherited __init__ retains
        # its full validation/signature. No argument is consumed or changed.
        def __new__(cls, *args: object, **kwargs: object) -> Self:  # pyright: ignore[reportInconsistentConstructor]
            _require_clean_environment()
            return super().__new__(cls)

        @property
        def user_agent(self) -> str:
            return super().user_agent.replace(type(self).__name__, AsyncAnthropic.__name__, 1)

        def _calculate_retry_timeout(  # pyright: ignore[reportIncompatibleMethodOverride]
            self, *args: object, **kwargs: object
        ) -> float:
            # The SDK still retries `max_retries` times (a mock transport that raises or
            # answers 429/5xx sees every attempt), but backs off for 0 s instead of 0.4-8 s
            # of idle wall clock per failed call, which a test does not need to observe.
            return 0.0

    return _IsolatedAsyncAnthropic(
        api_key=api_key,
        auth_token=None,
        credentials=None,
        config=None,
        profile=None,
        webhook_key=None,
        base_url=base_url,
        default_headers=dict(headers or {}),
        default_query={},
        middleware=(),
        max_retries=max_retries,
        timeout=timeout,
        http_client=httpx.AsyncClient(transport=transport, trust_env=False),
    )


def _require_clean_gemini_environment() -> None:
    # A future SDK pin can discover additional provider knobs. Check names only;
    # unknown names can themselves contain secrets, so do not echo them either.
    if any(name.upper().startswith(("GOOGLE_", "GEMINI_")) for name in os.environ):
        raise AmbientSDKConfigurationError("test Gemini SDK refuses ambient provider configuration")


def build_gemini_client(
    *,
    transport: httpx.BaseTransport,
    async_transport: httpx.AsyncBaseTransport | None = None,
    api_key: str = TEST_API_KEY,
    base_url: str = "https://generativelanguage.googleapis.com",
    api_version: str = "v1beta",
    headers: Mapping[str, str] | None = None,
) -> Client:
    """Own Gemini sync/async HTTP clients, including the interactions boundary.

    MockTransport implements both transport protocols; another sync transport
    requires an explicit async_transport. No aiohttp/default network fallback.
    """
    # google-genai 2.7 reads GOOGLE_API_KEY/GEMINI_API_KEY unconditionally,
    # even with explicit api_key. The shared named preflight refuses both.
    _require_clean_environment()
    _require_clean_gemini_environment()
    _require_explicit_configuration(api_key, base_url)
    _require_sync_transport(transport)
    if async_transport is None:
        if not isinstance(transport, httpx.AsyncBaseTransport):
            raise ValueError("an explicit async test transport is required")
        async_transport = transport
    _require_async_transport(async_transport)
    from google import genai
    from google.genai import types

    # trust_env=False still honors SSLKEYLOGFILE in native TLS construction.
    # Shared names-only preflight refuses TLS/proxy overrides before this call.
    # Explicit contexts cover Gemini's HTTP and websocket initialization.
    context = httpx.create_ssl_context(trust_env=False)
    client = genai.Client(
        api_key=api_key,
        enterprise=False,
        vertexai=False,
        credentials=None,
        project=None,
        location=None,
        debug_config=genai.client.DebugConfig(
            client_mode=None, replays_directory=None, replay_id=None
        ),
        http_options=types.HttpOptions(
            base_url=base_url,
            api_version=api_version,
            headers=dict(headers or {}),
            timeout=5000,
            retry_options=types.HttpRetryOptions(attempts=1),
            client_args={"trust_env": False, "verify": context},
            async_client_args={"trust_env": False, "verify": context, "ssl": context},
            httpx_client=httpx.Client(transport=transport, trust_env=False),
            httpx_async_client=httpx.AsyncClient(transport=async_transport, trust_env=False),
        ),
    )
    # Models counts attempts; the interactions SDK counts retries instead.
    # Configure the owned sync/async interaction clients before exposing them.
    client._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
    client.aio._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
    return client
