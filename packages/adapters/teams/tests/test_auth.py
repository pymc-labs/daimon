"""Inbound JWT validation must not block the event loop.

Real FastAPI + real Microsoft SDK ingress with authentication ON; only the
JWKS HTTP fetch is faked (``urllib.request.urlopen``, which PyJWT calls
synchronously) and made slow, so a fetch on the loop shows up as a stall.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.core.config import TeamsSettings
from daimon.testing.asgi import asgi_lifespan
from jwt.algorithms import RSAAlgorithm
from microsoft_teams.api.auth.cloud_environment import (  # pyright: ignore[reportMissingTypeStubs]
    PUBLIC,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
)

_FETCH_SECONDS = 0.2
_SERVICE_URL = "https://smba.trafficmanager.net/teams/"


class _Jwks:
    """A slow, counting stand-in for the Bot Framework JWKS endpoint."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        self.body = json.dumps({"keys": [{**public, "kid": "real", "use": "sig"}]}).encode()
        self.fetches = 0

    def urlopen(self, *_args: Any, **_kwargs: Any) -> Any:
        self.fetches += 1
        time.sleep(_FETCH_SECONDS)
        body = self.body

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self, *_a: Any) -> bytes:
                return body

        return _Response()


@pytest.fixture
def jwks(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jwks]:
    fake = _Jwks()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    yield fake


def _service(db_factory: async_sessionmaker[AsyncSession]) -> Any:
    return create_teams_http_service(
        settings=TeamsSettings(
            client_id=BOT_CLIENT_ID,
            client_secret=SecretStr("test-secret"),
            tenant_id=ENTRA_TENANT_ID,
        ),
        runtime=build_teams_runtime(db_factory),
        client=build_teams_client(TeamsApiFake()),
    )


def _forged_token(kid: str) -> str:
    return jwt.encode({"aud": BOT_CLIENT_ID}, "x" * 32, algorithm="HS256", headers={"kid": kid})


@pytest.mark.asyncio
async def test_forged_kid_tokens_do_not_stall_the_event_loop(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
    jwks: _Jwks,
) -> None:
    """Unauthenticated POSTs with unknown kids must neither fetch JWKS per
    request nor run any fetch on the loop — every turn shares that loop."""
    service = _service(db_session_factory)
    gaps: list[float] = []

    async def _ticker() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async with asgi_lifespan(service.app):
        transport = httpx.ASGITransport(app=service.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ticker = asyncio.create_task(_ticker())
            await asyncio.sleep(0.05)
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/messages",
                        json=make_message_activity(),
                        headers={"Authorization": f"Bearer {_forged_token(f'forged-{i}')}"},
                    )
                    for i in range(5)
                )
            )
            await asyncio.sleep(0.05)
            ticker.cancel()

    assert [r.status_code for r in responses] == [401] * 5
    # One cold fetch plus at most one throttled forced refresh — not one per
    # forged request — and none of them on the event loop.
    stall = max(gaps)
    assert jwks.fetches <= 2 and stall < _FETCH_SECONDS / 2, (
        f"{jwks.fetches} JWKS fetches, event loop stalled {stall:.2f}s"
    )


@pytest.mark.asyncio
async def test_genuine_token_still_validates_after_hardening(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_bot_token: None,
    jwks: _Jwks,
) -> None:
    service = _service(db_session_factory)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": PUBLIC.token_issuer,
            "aud": BOT_CLIENT_ID,
            "iat": now,
            "exp": now + 600,
            "serviceurl": _SERVICE_URL,
        },
        jwks.private_key,
        algorithm="RS256",
        headers={"kid": "real"},
    )
    async with asgi_lifespan(service.app):
        validator: Any = service.teams_app.server._token_validator  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]
        payload = await validator.validate_token(token, _SERVICE_URL)
        with pytest.raises(jwt.InvalidTokenError):
            await validator.validate_token(token, "https://attacker.example/")
    assert payload["aud"] == BOT_CLIENT_ID


def test_unknown_kid_refresh_is_allowed_again_after_the_interval(jwks: _Jwks) -> None:
    """Throttling must not strand a genuine key rotation."""
    from daimon.adapters.teams.auth import ThrottledJWKClient

    clock = [1000.0]
    client = ThrottledJWKClient(
        "https://login.botframework.com/v1/.well-known/keys",
        min_refresh_interval=60.0,
        clock=lambda: clock[0],
    )
    with pytest.raises(jwt.PyJWKClientError):
        client.get_signing_key("rotated")
    assert jwks.fetches == 2  # cold fetch + the one allowed forced refresh
    with pytest.raises(jwt.PyJWKClientError):
        client.get_signing_key("rotated")
    assert jwks.fetches == 2  # throttled: no network
    clock[0] += 61.0
    with pytest.raises(jwt.PyJWKClientError):
        client.get_signing_key("rotated")
    assert jwks.fetches == 3
    assert client.get_signing_key("real").key_id == "real"
