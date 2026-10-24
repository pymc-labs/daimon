"""Owned offline SDK construction; copies repeat environment admission."""

import os
from collections.abc import Mapping
from typing import Protocol

import httpx
from openai import AsyncOpenAI, NotGiven, not_given


class SDKFactory(Protocol):
    """Inject a test-owned SDK without importing host test infrastructure."""

    def __call__(
        self,
        *,
        transport: httpx.AsyncBaseTransport,
        api_key: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncOpenAI: ...


def build_offline_sdk(
    *,
    transport: httpx.AsyncBaseTransport,
    api_key: str,
    base_url: str,
    headers: Mapping[str, str] | None = None,
) -> AsyncOpenAI:
    """Standalone mux fallback; host tests inject their shared SDK factory."""
    refuse_inherited_settings()
    return OfflineAsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=headers,
        http_client=httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False),
    )


def refuse_inherited_settings() -> None:
    """Inspect presence only, preserving caller settings and safe diagnostics."""
    # _Environ inherits a membership implementation that fetches values.
    # Iteration obtains only names, including for other Mapping implementations.
    names = set(os.environ)
    if any(
        name in names
        for name in (
            "OPENAI_ADMIN_KEY",
            "OPENAI_WEBHOOK_SECRET",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID",
            "OPENAI_CUSTOM_HEADERS",
        )
    ):
        raise ValueError("offline SDK adapter refuses inherited OpenAI credential/account settings")


class OfflineAsyncOpenAI(AsyncOpenAI):
    """Pinned SDK with explicit offline defaults and guarded SDK copies.

    Callers supply their mock HTTP client explicitly. No production builder
    imports this module. AsyncOpenAI.copy/with_options construct self.__class__,
    so copies repeat admission without modifying production transport.
    None triggers account/credential fallback in SDK 2.54.0; empty values do
    not. Custom environment headers merge even with an empty explicit mapping,
    so their presence must refuse before the parent constructor.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        api_key: str = "offline-fixture",
        base_url: str | httpx.URL = "https://openai.invalid/v1",
        admin_api_key: str | None = "",
        organization: str | None = "",
        project: str | None = "",
        webhook_secret: str | None = "",
        workload_identity: None = None,
        provider: None = None,
        websocket_base_url: str | httpx.URL | None = None,
        timeout: float | httpx.Timeout | None | NotGiven = not_given,
        max_retries: int = 0,
        default_headers: Mapping[str, str] | None = None,
        default_query: Mapping[str, object] | None = None,
        _strict_response_validation: bool = False,
        _enforce_credentials: bool = True,
    ) -> None:
        refuse_inherited_settings()
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            admin_api_key=admin_api_key or "",
            organization=organization or "",
            project=project or "",
            webhook_secret=webhook_secret or "",
            workload_identity=workload_identity,
            provider=provider,
            websocket_base_url=websocket_base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers={} if default_headers is None else default_headers,
            default_query=default_query,
            http_client=http_client,
            _strict_response_validation=_strict_response_validation,
            _enforce_credentials=_enforce_credentials,
        )
