"""OIDC sign-in: PKCE, the authorize URL, code exchange and every ID token check."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from daimon.core.config import WebOidcSettings
from daimon.core.web_oidc import (
    OidcCache,
    OidcError,
    OidcKeyUnknown,
    OidcTokenInvalid,
    OidcTokens,
    build_authorize_url,
    code_challenge_s256,
    discover,
    exchange_code,
    fetch_jwks,
    refresh,
    verify_id_token,
)
from daimon.testing.oidc import FakeOidcProvider

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_REDIRECT = "https://web.example.com/auth/oidc/callback"


def _settings(provider: FakeOidcProvider) -> WebOidcSettings:
    return WebOidcSettings(
        issuer=provider.issuer,
        client_id=provider.client_id,
        client_secret="secret",  # pyright: ignore[reportArgumentType]  # SecretStr coerces str
        tenant_id=uuid.uuid4(),
    )


def _http(provider: FakeOidcProvider) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(provider.handler))


def _tokens(id_token: str | None) -> OidcTokens:
    return OidcTokens(
        access_token="a", refresh_token=None, id_token=id_token, expires_in=600, scope="openid"
    )


async def _verify(provider: FakeOidcProvider, id_token: str, *, nonce: str = "n1"):
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)
        jwks = await fetch_jwks(http, meta)
    return verify_id_token(
        _tokens(id_token), meta, jwks, _settings(provider), nonce=nonce, now=_NOW
    )


def test_code_challenge_matches_the_rfc_7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


async def test_authorize_url_carries_pkce_state_nonce_and_the_configured_scopes() -> None:
    provider = FakeOidcProvider()
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)

    url = build_authorize_url(
        meta,
        _settings(provider),
        state="s1",
        nonce="n1",
        code_challenge="c1",
        redirect_uri=_REDIRECT,
    )

    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{provider.issuer}/authorize?"), "goes to the authorization endpoint"
    assert query["response_type"] == "code", "authorization code flow"
    assert (query["code_challenge"], query["code_challenge_method"]) == ("c1", "S256")
    assert (query["state"], query["nonce"]) == ("s1", "n1"), "state and nonce are sent"
    assert query["scope"] == "openid email profile", "default scopes"
    assert query["redirect_uri"] == _REDIRECT, "the redirect uri is exact"


async def test_discovery_refuses_a_document_for_another_issuer() -> None:
    provider = FakeOidcProvider()
    other = FakeOidcProvider(issuer="https://other.example.com")

    def swapped(request: httpx.Request) -> httpx.Response:
        return other.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(swapped)) as http:
        with pytest.raises(OidcError, match="different issuer"):
            await discover(http, provider.issuer)


async def test_exchange_code_sends_the_verifier_and_reads_the_refresh_token() -> None:
    provider = FakeOidcProvider()
    verifier = "v" * 50
    code = provider.issue_code(
        code_challenge=code_challenge_s256(verifier),
        id_token=provider.id_token(nonce="n1"),
        refresh_token="r1",
    )
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)
        tokens = await exchange_code(
            http,
            meta,
            _settings(provider),
            code=code,
            code_verifier=verifier,
            redirect_uri=_REDIRECT,
        )
        with pytest.raises(OidcError, match="invalid_grant"):
            await exchange_code(
                http,
                meta,
                _settings(provider),
                code=code,
                code_verifier=verifier,
                redirect_uri=_REDIRECT,
            )

    assert tokens.refresh_token == "r1", "a refresh token is kept when the provider returns one"
    assert tokens.id_token is not None, "the id token is read"
    assert provider.token_requests[0]["client_secret"] == "secret", "client_secret_post is used"


async def test_exchange_code_refuses_a_verifier_that_does_not_match_the_challenge() -> None:
    provider = FakeOidcProvider()
    code = provider.issue_code(
        code_challenge=code_challenge_s256("right" * 10), id_token=provider.id_token(nonce="n1")
    )
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)
        with pytest.raises(OidcError, match="invalid_grant"):
            await exchange_code(
                http,
                meta,
                _settings(provider),
                code=code,
                code_verifier="wrong" * 10,
                redirect_uri=_REDIRECT,
            )


async def test_valid_id_token_yields_the_identity() -> None:
    provider = FakeOidcProvider()
    token = provider.id_token(nonce="n1", now=_NOW.timestamp(), groups=["staff", "ops"])

    identity = await _verify(provider, token)

    assert (identity.provider, identity.issuer, identity.subject) == (
        "oidc",
        provider.issuer,
        "user-1",
    ), "who signed in is read from iss and sub"
    assert (identity.email, identity.email_verified) == ("ada@example.com", True)
    assert identity.groups == ("staff", "ops"), "groups come from the configured claim"
    assert identity.display_name == "Ada Lovelace", "the name claim wins"


async def test_id_token_with_the_wrong_nonce_is_refused() -> None:
    provider = FakeOidcProvider()
    token = provider.id_token(nonce="attacker", now=_NOW.timestamp())
    with pytest.raises(OidcTokenInvalid, match="nonce"):
        await _verify(provider, token, nonce="n1")


async def test_expired_id_token_is_refused_but_clock_skew_is_tolerated() -> None:
    provider = FakeOidcProvider()
    expired = provider.id_token(nonce="n1", now=_NOW.timestamp() - 700)
    with pytest.raises(OidcTokenInvalid, match="expired"):
        await _verify(provider, expired)
    skewed = provider.id_token(nonce="n1", now=_NOW.timestamp() - 630)
    assert (await _verify(provider, skewed)).subject == "user-1", "a few seconds of skew is allowed"


async def test_id_token_for_another_audience_or_issuer_is_refused() -> None:
    provider = FakeOidcProvider()
    wrong_aud = provider.id_token(nonce="n1", now=_NOW.timestamp(), audience="someone-else")
    with pytest.raises(OidcTokenInvalid, match="Audience"):
        await _verify(provider, wrong_aud)
    wrong_iss = provider.id_token(
        nonce="n1", now=_NOW.timestamp(), issuer="https://evil.example.com"
    )
    with pytest.raises(OidcTokenInvalid, match="Issuer"):
        await _verify(provider, wrong_iss)


async def test_id_token_signed_with_a_symmetric_algorithm_is_refused() -> None:
    provider = FakeOidcProvider()
    forged = provider.sign(
        {
            "iss": provider.issuer,
            "aud": provider.client_id,
            "sub": "admin",
            "exp": int(_NOW.timestamp()) + 600,
            "nonce": "n1",
        },
        algorithm="HS256",
    )
    with pytest.raises(OidcTokenInvalid, match="algorithm"):
        await _verify(provider, forged)


async def test_id_token_signed_by_another_key_is_refused() -> None:
    provider = FakeOidcProvider()
    token = provider.id_token(nonce="n1", now=_NOW.timestamp())
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(OidcTokenInvalid, match="Signature"):
        await _verify(provider, tampered)


async def test_missing_id_token_and_unknown_key_are_refused() -> None:
    provider = FakeOidcProvider()
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)
        jwks = await fetch_jwks(http, meta)
    with pytest.raises(OidcTokenInvalid, match="no id_token"):
        verify_id_token(_tokens(None), meta, jwks, _settings(provider), nonce="n1", now=_NOW)
    claims = jwt.decode(
        provider.id_token(nonce="n1", now=_NOW.timestamp()), options={"verify_signature": False}
    )
    rotated = provider.sign(claims, kid="rotated-key")
    with pytest.raises(OidcKeyUnknown):
        verify_id_token(_tokens(rotated), meta, jwks, _settings(provider), nonce="n1", now=_NOW)


async def test_cache_fetches_the_key_set_once_and_refetches_for_an_unknown_key() -> None:
    provider = FakeOidcProvider()
    fetches: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        fetches.append(request.url.path)
        return provider.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(counting)) as http:
        cache = OidcCache(http, _settings(provider))
        for _ in range(2):
            token = provider.id_token(nonce="n1", now=_NOW.timestamp())
            await cache.verify(_tokens(token), nonce="n1", now=_NOW)
        assert fetches.count("/jwks") == 1, "the key set is reused within its lifetime"
        assert fetches.count("/.well-known/openid-configuration") == 1, "metadata is reused"
        unknown = provider.sign(
            jwt.decode(token, options={"verify_signature": False}), kid="not-published"
        )
        with pytest.raises(OidcKeyUnknown):
            await cache.verify(_tokens(unknown), nonce="n1", now=_NOW.replace(minute=5))
    assert fetches.count("/jwks") == 2, "an unknown key triggers one refetch"


async def test_refresh_rotates_the_refresh_token_and_refuses_the_old_one() -> None:
    provider = FakeOidcProvider()
    provider.allow_refresh("r1")
    async with _http(provider) as http:
        meta = await discover(http, provider.issuer)
        tokens = await refresh(http, meta, _settings(provider), refresh_token="r1")
        with pytest.raises(OidcError, match="invalid_grant"):
            await refresh(http, meta, _settings(provider), refresh_token="r1")

    assert tokens.access_token.startswith("access-"), "a new access token comes back"
    assert tokens.refresh_token not in {None, "r1"}, "the refresh token rotates"
    assert tokens.expires_in == 600, "with its lifetime"
    sent = provider.token_requests[0]
    assert (sent["grant_type"], sent["refresh_token"]) == ("refresh_token", "r1"), sent
    assert sent["client_secret"] == "secret", "the client authenticates"
