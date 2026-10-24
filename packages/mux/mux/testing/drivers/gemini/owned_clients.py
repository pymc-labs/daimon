"""Owned offline HTTP clients for the Gemini deployment constructors.

`owned_clients` swaps the httpx clients the SDK allocates for ones backed by a mock handler
and records them, so a test can prove the driver owns and closes both. `FORBIDDEN_NAMES`
lists the ambient configuration names the deployment constructors must refuse.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn

import httpx
import pytest
from google import auth

from mux.drivers.gemini import deployment

FORBIDDEN_NAMES = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GEMINI_BASE_URL",
    "GEMINI_NEXT_GEN_API_BASE_URL",
    "google_future_private_setting",
    "GeMiNi_FuTuRe_HeAdEr",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSLKEYLOGFILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def forbidden(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("constructor allocated or discovered ambient configuration")


@dataclass
class OwnedClients:
    synchronous: list[httpx.Client] = field(default_factory=list[httpx.Client])
    asynchronous: list[httpx.AsyncClient] = field(default_factory=list[httpx.AsyncClient])
    contexts: list[ssl.SSLContext] = field(default_factory=list[ssl.SSLContext])

    def require_closed(self) -> None:
        if len(self.synchronous) != 1 or len(self.asynchronous) != 1:
            raise AssertionError("SDK did not use exactly two owned HTTP clients")
        if not all(client.is_closed for client in (*self.synchronous, *self.asynchronous)):
            raise AssertionError("owned HTTP lifetime was not closed")
        if len(self.contexts) != 2 or self.contexts[0] is not self.contexts[1]:
            raise AssertionError("both clients require the same explicit TLS context")


def owned_clients(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> OwnedClients:
    synchronous_type, asynchronous_type = httpx.Client, httpx.AsyncClient
    owned = OwnedClients()

    class OfflineSync(synchronous_type):
        def __init__(
            self, *, trust_env: bool, timeout: float, verify: object, proxy: str | None
        ) -> None:
            if trust_env or proxy is not None or not isinstance(verify, ssl.SSLContext):
                raise AssertionError("sync HTTP inherited TLS/proxy policy")
            super().__init__(
                transport=httpx.MockTransport(handler),
                trust_env=False,
                timeout=timeout,
                verify=verify,
                proxy=None,
            )
            owned.synchronous.append(self)
            owned.contexts.append(verify)

    class OfflineAsync(asynchronous_type):
        def __init__(
            self, *, trust_env: bool, timeout: float, verify: object, proxy: str | None
        ) -> None:
            if trust_env or proxy is not None or not isinstance(verify, ssl.SSLContext):
                raise AssertionError("async HTTP inherited TLS/proxy policy")
            super().__init__(
                transport=httpx.MockTransport(handler),
                trust_env=False,
                timeout=timeout,
                verify=verify,
                proxy=None,
            )
            owned.asynchronous.append(self)
            owned.contexts.append(verify)

    monkeypatch.setattr(deployment.httpx, "Client", OfflineSync)
    monkeypatch.setattr(deployment.httpx, "AsyncClient", OfflineAsync)
    monkeypatch.setattr(auth, "default", forbidden)
    monkeypatch.setattr("httpx._client.get_environment_proxies", forbidden)
    return owned
