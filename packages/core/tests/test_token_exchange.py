from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from daimon.core.mcp_identity import generate_signing_key
from daimon.core.token_exchange import (
    TOKEN_EXCHANGE_GRANT,
    ExchangeClient,
    TokenExchangeError,
    exchange_subject_token,
    sign_subject_token,
)

_NOW = datetime(2026, 5, 1, tzinfo=UTC)
_CLIENT = ExchangeClient(
    token_url="https://data.example.com/oauth/token",
    client_id="c",
    client_secret="s",
    scope="openid data",
)


def test_the_subject_token_names_the_person_and_workspace_for_two_minutes() -> None:
    key = generate_signing_key()
    token = sign_subject_token(
        key=key,
        issuer="iss-1",
        audience="https://data.example.com",
        subject="discord:u-1",
        workspace="discord:g-1",
        now=_NOW,
        jti="j",
    )
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["EdDSA"],
        audience="https://data.example.com",
        options={"verify_exp": False},
    )
    assert (claims["iss"], claims["sub"], claims["ws"]) == (
        "iss-1",
        "discord:u-1",
        "discord:g-1",
    ), "who, where and by whom"
    assert claims["exp"] - claims["iat"] == 120, "a subject token lives two minutes"


async def test_the_exchange_posts_the_grant_and_returns_the_token() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "at", "expires_in": 600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        got = await exchange_subject_token(http, _CLIENT, subject_token="st", timeout=1.0)

    assert (got.access_token, got.expires_in) == ("at", 600), "the server's token and life"
    assert seen["grant_type"] == [TOKEN_EXCHANGE_GRANT], "an RFC 8693 exchange"
    assert seen["scope"] == ["openid data"], "the configured scopes"


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (400, {"error": "invalid_grant", "error_description": "secret detail"}, "invalid_grant"),
        (200, {"token_type": "Bearer"}, "no access_token"),
        (502, "not json", "502"),
    ],
)
async def test_a_refused_exchange_raises_with_only_the_error_code(
    status: int, body: object, match: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TokenExchangeError, match=match) as caught:
            await exchange_subject_token(http, _CLIENT, subject_token="st", timeout=1.0)
    assert "secret detail" not in str(caught.value), "descriptions are never echoed"
