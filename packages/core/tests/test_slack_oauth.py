"""Unit tests for slack_oauth pure helpers — transport-level fakes only.

Covers: build_authorize_url, exchange_code, SlackExchangeResult, SLACK_BOT_SCOPES,
mint_state, verify_state, and SlackOAuthError taxonomy.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from daimon.core.errors import SlackOAuthError
from daimon.core.slack_oauth import (
    SLACK_BOT_SCOPES,
    SLACK_USER_SCOPES,
    SlackExchangeResult,
    build_authorize_url,
    build_slack_connect_url,
    exchange_code,
    mint_state,
    verify_state,
)

# ---------------------------------------------------------------------------
# build_authorize_url
# ---------------------------------------------------------------------------


def test_build_authorize_url_returns_slack_v2_authorize_url() -> None:
    url = build_authorize_url(
        client_id="CLIENT123",
        redirect_url="https://example.com/oauth/slack/callback",
        state="signed_state_token",
        scopes=("app_mentions:read", "chat:write"),
    )
    assert url.startswith("https://slack.com/oauth/v2/authorize?"), (
        "authorize URL should target Slack's v2 authorize endpoint"
    )


def test_build_authorize_url_comma_joins_scopes() -> None:
    """Slack authorize URL requires comma-separated scopes, not space-separated."""
    url = build_authorize_url(
        client_id="CLIENT123",
        redirect_url="https://example.com/oauth/slack/callback",
        state="s",
        scopes=("app_mentions:read", "chat:write", "commands"),
    )
    # comma-joined then urlencoded: "," becomes "%2C"
    assert "%2C" in url or "app_mentions%3Aread%2Cchat%3Awrite" in url, (
        "scopes must be comma-joined (not space-joined) in the authorize URL"
    )
    # Must NOT use space (urlencoded as + or %20)
    assert "scope=app_mentions%3Aread+chat%3Awrite" not in url, (
        "space-joined scopes would cause wrong/zero grants from Slack"
    )


def test_build_authorize_url_includes_client_id_redirect_uri_and_state() -> None:
    url = build_authorize_url(
        client_id="MY_CLIENT",
        redirect_url="https://bot.example.com/oauth/slack/callback",
        state="my_signed_state",
        scopes=("chat:write",),
    )
    assert "client_id=MY_CLIENT" in url, "URL must include client_id"
    assert "state=my_signed_state" in url, "URL must include signed state"
    assert "redirect_uri=" in url, "URL must include redirect_uri"


def test_slack_bot_scopes_constant_contains_required_v3_day1_scopes() -> None:
    required = {
        "app_mentions:read",
        "chat:write",
        "commands",
        "users:read",
        "channels:history",
        "groups:history",
        "reactions:write",  # D-01: ⌛ queued-mention reaction parity (Phase 80)
        "files:read",  # attachments & vision: read event.files + fetch url_private
        "channels:read",  # channel tools: public channel metadata + membership checks
        "groups:read",  # channel tools: private channel metadata + membership checks
    }
    assert required <= set(SLACK_BOT_SCOPES), (
        "SLACK_BOT_SCOPES must include all v3.0 day-1 bot scopes"
    )
    assert len(SLACK_BOT_SCOPES) == 10, (
        "SLACK_BOT_SCOPES must have exactly 10 scopes after adding channels:read and groups:read"
    )


def test_slack_user_scopes_include_users_read_for_author_resolution() -> None:
    assert "users:read" in SLACK_USER_SCOPES, (
        "user-token reads resolve author display names via users.info, "
        "which requires the users:read user scope"
    )


# ---------------------------------------------------------------------------
# exchange_code — success path
# ---------------------------------------------------------------------------


async def test_exchange_code_parses_success_body_with_team_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "slack.com", "exchange_code should call slack.com"
        assert request.url.path == "/api/oauth.v2.access", (
            "exchange_code should POST to /api/oauth.v2.access"
        )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-123-456-abc",
                "team": {"id": "T12345", "name": "My Workspace"},
                "is_enterprise_install": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exchange_code(
            client=client,
            code="abc",
            client_id="cid",
            client_secret="secret",
            redirect_url="https://example.com/oauth/slack/callback",
        )
    assert isinstance(result, SlackExchangeResult), (
        "exchange_code should return a SlackExchangeResult"
    )
    assert result.access_token == "xoxb-123-456-abc", "access_token should be the xoxb- bot token"
    assert result.team_id == "T12345", "team_id should be read from team.id"
    assert result.team_name == "My Workspace", (
        "team_name should be read from team.name for the success page"
    )
    assert result.is_enterprise_install is False, (
        "is_enterprise_install should be False for a regular workspace"
    )


async def test_exchange_code_includes_rotation_fields_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-rotated",
                "team": {"id": "T99", "name": "Rotated WS"},
                "is_enterprise_install": False,
                "expires_in": 43200,
                "refresh_token": "xoxe-1-abc",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exchange_code(
            client=client,
            code="code",
            client_id="cid",
            client_secret="secret",
            redirect_url="https://example.com/cb",
        )
    assert result.expires_in == 43200, "expires_in should be parsed from the response"
    assert result.refresh_token == "xoxe-1-abc", "refresh_token should be parsed"


async def test_exchange_code_yields_none_rotation_fields_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-no-rotation",
                "team": {"id": "T1", "name": "WS"},
                "is_enterprise_install": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exchange_code(
            client=client,
            code="code",
            client_id="cid",
            client_secret="secret",
            redirect_url="https://example.com/cb",
        )
    assert result.expires_in is None, "expires_in should be None when rotation is disabled"
    assert result.refresh_token is None, "refresh_token should be None when rotation is disabled"


# ---------------------------------------------------------------------------
# exchange_code — ok:false raises SlackOAuthError (not KeyError)
# ---------------------------------------------------------------------------


async def test_exchange_code_raises_slack_oauth_error_on_ok_false() -> None:
    """Slack returns HTTP 200 with ok:false on bad/expired codes.

    exchange_code must raise SlackOAuthError, not KeyError on access_token.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SlackOAuthError) as ei:
            await exchange_code(
                client=client,
                code="bad_code",
                client_id="cid",
                client_secret="secret",
                redirect_url="https://example.com/cb",
            )
    assert "invalid_code" in str(ei.value), "SlackOAuthError should carry Slack's error code"


async def test_exchange_code_raises_slack_oauth_error_with_unknown_when_error_field_absent() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SlackOAuthError) as ei:
            await exchange_code(
                client=client,
                code="bad",
                client_id="cid",
                client_secret="secret",
                redirect_url="https://example.com/cb",
            )
    assert "unknown_error" in str(ei.value), (
        "SlackOAuthError should fall back to 'unknown_error' when error field absent"
    )


# ---------------------------------------------------------------------------
# exchange_code — Enterprise Grid (is_enterprise_install=True)
# ---------------------------------------------------------------------------


async def test_exchange_code_enterprise_install_yields_none_team_id_and_name() -> None:
    """Enterprise Grid installs nest the id under enterprise, team may be null.

    exchange_code should NOT crash on missing team; team_id and team_name must be None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-enterprise",
                "is_enterprise_install": True,
                "enterprise": {"id": "E123", "name": "BigCorp"},
                # team is absent/null for org-level enterprise installs
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await exchange_code(
            client=client,
            code="ent_code",
            client_id="cid",
            client_secret="secret",
            redirect_url="https://example.com/cb",
        )
    assert result.is_enterprise_install is True, (
        "is_enterprise_install should be True for Grid installs"
    )
    assert result.team_id is None, (
        "team_id must be None for enterprise installs (team.id is not the tenant key)"
    )
    assert result.team_name is None, "team_name must be None for enterprise installs"


# ---------------------------------------------------------------------------
# mint_state / verify_state
# ---------------------------------------------------------------------------


def test_mint_state_and_verify_state_roundtrip_returns_empty_dict() -> None:
    secret = "test_signing_secret"
    now = 1_700_000_000.0
    token = mint_state(signing_secret=secret, now=now)
    assert verify_state(token=token, signing_secret=secret, now=now) == {}, (
        "a freshly minted payloadless token should verify to an empty dict"
    )


def test_verify_state_returns_none_for_tampered_signature() -> None:
    secret = "test_signing_secret"
    now = 1_700_000_000.0
    token = mint_state(signing_secret=secret, now=now)
    # Flip the last character of the token (corrupts the signature)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_state(token=tampered, signing_secret=secret, now=now) is None, (
        "a token with a tampered signature must not verify (return None)"
    )


def test_verify_state_returns_none_for_expired_token() -> None:
    secret = "test_signing_secret"
    mint_now = 1_700_000_000.0
    token = mint_state(signing_secret=secret, now=mint_now)
    # Verify one second past the TTL
    verify_now = mint_now + 601
    assert verify_state(token=token, signing_secret=secret, now=verify_now, ttl_s=600) is None, (
        "a token older than ttl_s must not verify (return None)"
    )


def test_verify_state_returns_empty_dict_for_replay_within_ttl() -> None:
    """D-06: replay within TTL is accepted (no nonce store)."""
    secret = "test_signing_secret"
    mint_now = 1_700_000_000.0
    token = mint_state(signing_secret=secret, now=mint_now)
    replay_now = mint_now + 60  # 60 s later, well within 600 s TTL
    assert verify_state(token=token, signing_secret=secret, now=replay_now, ttl_s=600) == {}, (
        "replay within TTL should succeed and return empty dict (idempotent provisioning re-run is harmless)"
    )


def test_verify_state_raises_value_error_for_token_missing_dot_separator() -> None:
    """A token without the '.' separator is structurally malformed."""
    with pytest.raises(ValueError):
        verify_state(
            token="notokenwithoutseparator",
            signing_secret="secret",
            now=1_700_000_000.0,
        )


def test_verify_state_raises_value_error_for_non_base64_content() -> None:
    """Non-decodable base64 in the token is a structural error."""
    with pytest.raises(ValueError):
        verify_state(
            token="not!valid!base64.alsoinvalid!",
            signing_secret="secret",
            now=1_700_000_000.0,
        )


def test_verify_state_wrong_secret_returns_none() -> None:
    secret = "correct_secret"
    wrong_secret = "wrong_secret"
    now = 1_700_000_000.0
    token = mint_state(signing_secret=secret, now=now)
    assert verify_state(token=token, signing_secret=wrong_secret, now=now) is None, (
        "a token signed with a different secret must not verify (return None)"
    )


def test_mint_state_round_trips_payload_through_verify() -> None:
    token = mint_state(
        signing_secret="sec",
        now=1000.0,
        payload={"flow": "user_connect", "team_id": "T1", "slack_user_id": "U1"},
    )
    payload = verify_state(token=token, signing_secret="sec", now=1200.0)
    assert payload == {"flow": "user_connect", "team_id": "T1", "slack_user_id": "U1"}, (
        "payload minted into the state must round-trip through verify_state"
    )


def test_verify_state_returns_empty_dict_for_payloadless_token() -> None:
    token = mint_state(signing_secret="sec", now=1000.0)
    assert verify_state(token=token, signing_secret="sec", now=1200.0) == {}, (
        "install-flow tokens (no payload) verify to an empty dict, not None"
    )


def test_verify_state_returns_none_on_bad_signature() -> None:
    token = mint_state(signing_secret="sec", now=1000.0, payload={"flow": "user_connect"})
    assert verify_state(token=token, signing_secret="other", now=1200.0) is None, (
        "wrong signing secret must yield None"
    )


def test_verify_state_returns_none_on_expiry() -> None:
    token = mint_state(signing_secret="sec", now=1000.0)
    assert verify_state(token=token, signing_secret="sec", now=1000.0 + 601) is None, (
        "expired token must yield None"
    )


def test_build_slack_connect_url_shape() -> None:
    url = build_slack_connect_url(
        app_root_url="https://app.example",
        signing_secret="sec",
        team_id="T1",
        slack_user_id="U1",
        now=1000.0,
    )
    assert url.startswith("https://app.example/oauth/slack/connect?state="), (
        "connect URL must target /oauth/slack/connect with a state param"
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    payload = verify_state(token=state, signing_secret="sec", now=1000.0)
    assert payload == {"flow": "user_connect", "team_id": "T1", "slack_user_id": "U1"}, (
        "connect URL state must carry the user_connect binding payload"
    )


# ---------------------------------------------------------------------------
# user_scope parameter and authed_user parsing
# ---------------------------------------------------------------------------


def test_build_authorize_url_includes_user_scope_when_given() -> None:
    url = build_authorize_url(
        client_id="cid",
        redirect_url="https://x/cb",
        state="s",
        scopes=(),
        user_scope=("search:read", "channels:history"),
    )
    parsed = parse_qs(urlparse(url).query)
    assert parsed["user_scope"] == ["search:read,channels:history"], (
        "user_scope must be comma-joined into the user_scope query param"
    )
    assert "scope" not in parsed, "empty bot scopes must omit the scope param entirely"


def test_build_authorize_url_omits_user_scope_when_none() -> None:
    url = build_authorize_url(
        client_id="cid", redirect_url="https://x/cb", state="s", scopes=("chat:write",)
    )
    parsed = parse_qs(urlparse(url).query)
    assert "user_scope" not in parsed, "no user_scope param when user_scope is None"
    assert parsed["scope"] == ["chat:write"], "bot scopes unchanged"


async def test_exchange_code_parses_authed_user_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "authed_user": {
                    "id": "U777",
                    "access_token": "xoxp-secret",
                    "scope": "search:read,channels:history",
                },
                "team": {"id": "T1", "name": "ws"},
                "is_enterprise_install": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await exchange_code(
            client=client, code="c", client_id="i", client_secret="s", redirect_url="https://x/cb"
        )
    assert result.access_token is None, "user-only exchange has no top-level bot access_token"
    assert result.authed_user_id == "U777", "authed_user.id must be parsed"
    assert result.authed_user_access_token == "xoxp-secret", "xoxp token must be parsed"
    assert result.authed_user_scope == "search:read,channels:history", "granted scopes recorded"


async def test_exchange_code_without_authed_user_yields_none_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-bot",
                "team": {"id": "T1", "name": "ws"},
                "is_enterprise_install": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await exchange_code(
            client=client, code="c", client_id="i", client_secret="s", redirect_url="https://x/cb"
        )
    assert result.access_token == "xoxb-bot", "bot install path unchanged"
    assert result.authed_user_id is None, "no authed_user block → None fields"
    assert result.authed_user_access_token is None, "no authed_user block → None fields"
