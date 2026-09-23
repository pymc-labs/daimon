"""Keep inbound Bot Framework JWT validation off the event loop.

The SDK's ``TokenValidator`` looks signing keys up through PyJWT's
``PyJWKClient``, whose JWKS fetch is a synchronous ``urllib`` call made from
inside ``async def validate_token``. Worse, a token whose ``kid`` is not in
the cached set forces a refetch on every call — and the ``kid`` is read from
the unverified header, so any unauthenticated POST to ``/api/messages`` can
block the loop for a full JWKS round-trip (up to the 30 s urllib timeout).
This process shares that loop with every streaming turn.

``harden_token_validation`` fixes both halves after ``App.initialize``:
unknown-``kid`` refreshes are throttled to one per interval, so forged
tokens cost no network, and validation runs in a worker thread, so the
remaining legitimate fetches (cold start, cache expiry) never block the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from jwt import PyJWK, PyJWKClient, PyJWKClientError, PyJWKSet
from microsoft_teams.apps import App  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.apps.auth.token_validator import (  # pyright: ignore[reportMissingTypeStubs]
    TokenValidator,
)

# Microsoft rolls Bot Framework signing keys with days of overlap, so a
# genuinely new kid is rare; one forced refresh a minute is ample.
MIN_UNKNOWN_KID_REFRESH_SECONDS = 60.0


class ThrottledJWKClient(PyJWKClient):
    """A ``PyJWKClient`` that refreshes for an unknown ``kid`` at most once per interval.

    A miss inside the interval raises ``PyJWKClientError`` without touching
    the network — the same error the base client raises after a refresh that
    still does not find the key, so the SDK answers 401 either way.
    """

    def __init__(
        self,
        uri: str,
        *,
        min_refresh_interval: float = MIN_UNKNOWN_KID_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(uri)
        self._min_refresh_interval = min_refresh_interval
        self._clock = clock
        self._last_forced_refresh: float | None = None
        self._refresh_lock = threading.Lock()
        self._fetch_lock = threading.Lock()

    def get_jwk_set(self, refresh: bool = False) -> PyJWKSet:
        # Validation runs in worker threads: serialize so concurrent requests
        # on a cold or expired cache share one fetch instead of each making one.
        with self._fetch_lock:
            return super().get_jwk_set(refresh=refresh)

    def get_signing_key(self, kid: str) -> PyJWK:
        signing_key = self.match_kid(self.get_signing_keys(), kid)
        if signing_key is not None:
            return signing_key
        with self._refresh_lock:
            now = self._clock()
            last = self._last_forced_refresh
            if last is not None and now - last < self._min_refresh_interval:
                raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
            self._last_forced_refresh = now
        signing_key = self.match_kid(self.get_signing_keys(refresh=True), kid)
        if signing_key is None:
            raise PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
        return signing_key


class OffLoopTokenValidator:
    """Run the SDK validator's (synchronous-in-practice) body in a worker thread.

    ``TokenValidator.validate_token`` is ``async`` but never awaits: it is a
    blocking JWKS lookup plus ``jwt.decode``. Driving that coroutine on a
    private loop in a worker thread keeps the SDK's validation logic intact
    while the main loop stays free.
    """

    def __init__(self, inner: TokenValidator) -> None:
        self._inner = inner

    async def validate_token(
        self, raw_token: str, service_url: str | None = None, scope: str | None = None
    ) -> dict[str, Any]:
        def _validate() -> dict[str, Any]:
            return asyncio.run(self._inner.validate_token(raw_token, service_url, scope))

        return await asyncio.to_thread(_validate)


def harden_token_validation(teams_app: App) -> None:
    """Swap in the throttled, off-loop validator. Call after ``App.initialize``.

    A no-op when the SDK installed no validator (unauthenticated dev mode, or
    no credentials — the SDK then rejects every request itself). Reaches into
    SDK privates on purpose: ``AppOptions`` exposes no validator seam, and an
    SDK upgrade that renames them fails loudly here at startup rather than
    silently reverting to the blocking path.
    """
    server: Any = teams_app.server  # pyright: ignore[reportUnknownMemberType]
    validator = server._token_validator
    if validator is None:
        return
    if not isinstance(validator._jwks_client, PyJWKClient):
        raise TypeError("unexpected Teams SDK token validator shape; re-check auth.py")
    validator._jwks_client = ThrottledJWKClient(validator._jwks_client.uri)
    server._token_validator = OffLoopTokenValidator(validator)
