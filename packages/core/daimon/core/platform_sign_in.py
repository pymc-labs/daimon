"""Chat-platform sign-in: who a Discord, Slack or Microsoft login is, and where they belong.

Shared by every surface that signs people in with their chat account (the MCP hub
mounts, the web app). Each helper does the provider's own I/O through an injected
`httpx.AsyncClient` and returns the platform user id plus the workspaces it vouches
for; matching those to installed tenants is `hub_identity`'s job.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import structlog
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.microsoft_identity import (
    MicrosoftIdentityMismatch,
    MicrosoftTokens,
    canonical_uuid,
    id_token_claims,
)
from daimon.core.teams_graph import GRAPH_ROOT

log = structlog.get_logger(__name__)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_SIGN_IN_SCOPES: tuple[str, ...] = ("identify", "guilds")
# Most guilds Discord returns per request of `/users/@me/guilds`.
DISCORD_GUILDS_PAGE = 200
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_USERS_INFO_URL = "https://slack.com/api/users.info"
# A user grant needs one user scope; this one also lets sign-in read the person's name.
SLACK_SIGN_IN_USER_SCOPES: tuple[str, ...] = ("users:read",)
# No "email" or Graph scope: tenants consent org-wide to openid and profile, and an extra
# scope would prompt each person for it. The token's own User.Read, where the tenant granted
# it, lets `fetch_microsoft_profile` read the mailbox address and organization name.
MICROSOFT_SIGN_IN_SCOPES: tuple[str, ...] = ("openid", "profile")
# A slow Graph must not hold up the sign-in.
MICROSOFT_PROFILE_TIMEOUT_S = 5.0


def code_challenge_s256(verifier: str) -> str:
    """The PKCE challenge for a verifier (RFC 7636, S256)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class PlatformSignInError(DaimonError, ValueError):
    """The provider answered, but not with a usable sign-in.

    A `ValueError` too, which is what the hub's OAuth proxy expects from a failed
    code exchange.
    """


def discord_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    interactive: bool = False,
) -> str:
    """Discord's consent URL. `prompt=none` signs a returning person in without a screen;
    `interactive` leaves it off so the person sees which account they are approving."""
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(DISCORD_SIGN_IN_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if not interactive:
        query["prompt"] = "none"
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(query)}"


async def exchange_discord_code(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    """Trade a Discord authorization code for the user's access token."""
    response = await http.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise PlatformSignInError("discord exchange returned no access token")
    return token


@dataclass(frozen=True, slots=True)
class DiscordUser:
    user_id: str
    name: str
    # (guild id, guild name) for every server the user is in.
    workspaces: list[tuple[str, str]]


async def fetch_discord_user(http: httpx.AsyncClient, *, access_token: str) -> DiscordUser:
    """The token's user and their guilds (scopes `identify guilds`)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    me = await http.get(f"{DISCORD_API}/users/@me", headers=headers)
    me.raise_for_status()
    body: dict[str, Any] = me.json()
    return DiscordUser(
        user_id=str(body["id"]),
        name=str(body.get("global_name") or body.get("username") or body["id"]),
        workspaces=await _fetch_discord_guilds(http, headers=headers),
    )


async def _fetch_discord_guilds(
    http: httpx.AsyncClient, *, headers: dict[str, str]
) -> list[tuple[str, str]]:
    """Every guild the token's user is in: Discord returns them in pages, ordered by id."""
    guilds: list[tuple[str, str]] = []
    params: dict[str, str | int] = {"limit": DISCORD_GUILDS_PAGE}
    while True:
        response = await http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers, params=params)
        response.raise_for_status()
        page: list[dict[str, Any]] = response.json()
        guilds.extend((str(g["id"]), str(g["name"])) for g in page)
        if len(page) < DISCORD_GUILDS_PAGE:
            return guilds
        params = {"limit": DISCORD_GUILDS_PAGE, "after": str(page[-1]["id"])}


async def fetch_discord_workspaces(
    http: httpx.AsyncClient, *, access_token: str
) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(user_id, [(guild_id, guild_name), ...])`` for the token's user."""
    user = await fetch_discord_user(http, access_token=access_token)
    return user.user_id, user.workspaces


def slack_authorize_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Slack's user-grant consent. It takes no PKCE, and `scope` would install the bot."""
    query = {
        "client_id": client_id,
        "user_scope": ",".join(SLACK_SIGN_IN_USER_SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{SLACK_AUTHORIZE_URL}?{urlencode(query)}"


async def fetch_slack_user_name(
    http: httpx.AsyncClient, *, access_token: str, user_id: str
) -> str | None:
    """The person's Slack display name, or None when Slack will not say."""
    response = await http.get(
        SLACK_USERS_INFO_URL,
        params={"user": user_id},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    user: dict[str, Any] = body.get("user") or {}
    profile: dict[str, Any] = user.get("profile") or {}
    name = profile.get("display_name") or user.get("real_name") or user.get("name")
    return str(name) if body.get("ok") and name else None


@dataclass(frozen=True, slots=True)
class SlackUserGrant:
    """A Slack user (xoxp) grant, lifted out of `oauth.v2.access`'s `authed_user`."""

    access_token: str
    scope: str
    team_id: str | None
    team_name: str | None
    user_id: str | None
    expires_in: int | None
    refresh_token: str | None


async def exchange_slack_user_code(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    url: str = SLACK_TOKEN_URL,
) -> SlackUserGrant:
    """Trade a Slack authorization code for the user's grant.

    Slack returns the user token under `authed_user`, not at the top level, and an
    Enterprise Grid install names an organisation rather than one workspace, which
    no tenant maps to.
    """
    response = await http.post(
        url,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if not payload.get("ok"):
        raise PlatformSignInError(f"slack exchange failed: {payload.get('error', 'unknown_error')}")
    if payload.get("is_enterprise_install"):
        raise PlatformSignInError("Enterprise Grid installs are not supported")
    authed_user: dict[str, Any] = payload.get("authed_user") or {}
    if "access_token" not in authed_user:
        raise PlatformSignInError("slack exchange returned no user token")
    team: dict[str, Any] = payload.get("team") or {}
    expires_in = authed_user.get("expires_in")
    user_id = authed_user.get("id")
    return SlackUserGrant(
        access_token=str(authed_user["access_token"]),
        scope=str(authed_user.get("scope", "")),
        team_id=str(team["id"]) if team.get("id") is not None else None,
        team_name=str(team["name"]) if team.get("name") is not None else None,
        user_id=str(user_id) if user_id is not None else None,
        expires_in=int(expires_in) if expires_in is not None else None,
        refresh_token=cast(str | None, authed_user.get("refresh_token")) or None,
    )


class MicrosoftGuestRefused(UserFacingError):
    """The account is a guest of the directory, not a member."""


@dataclass(frozen=True, slots=True)
class MicrosoftUser:
    # Canonical lowercase UUIDs: the Entra directory, and the person's object id in it.
    tenant_id: str
    object_id: str
    name: str
    username: str | None


def verify_microsoft_sign_in(
    tokens: MicrosoftTokens, config: MicrosoftAccountsSettings
) -> MicrosoftUser:
    """Who signed in, if the account is the configured directory's and the token is ours.

    The sign-in sibling of `microsoft_identity.verify_identity`: there is no Teams
    person to match yet, so any account of the directory passes.
    """
    if tokens.id_token is None:
        raise MicrosoftIdentityMismatch("token response carried no id_token")
    claims = id_token_claims(tokens.id_token)
    tid = canonical_uuid(claims.get("tid"))
    if tid is None or tid != config.tenant_id:
        raise MicrosoftIdentityMismatch("account belongs to another tenant")
    if claims.get("aud") != config.client_id:
        raise MicrosoftIdentityMismatch("id_token was issued to another app")
    # A B2B or personal-account guest carries the identity provider that vouched for it in
    # `idp`; a member of the directory has none, or its own issuer.
    idp = claims.get("idp")
    if idp is not None and idp != claims.get("iss"):
        raise MicrosoftGuestRefused(
            "Guest accounts can't sign in. Use an account of your organization's directory "
            "or ask its Microsoft admin."
        )
    oid = canonical_uuid(claims.get("oid"))
    if oid is None:
        raise MicrosoftIdentityMismatch("id_token carried no object id")
    username = claims.get("preferred_username")
    return MicrosoftUser(
        tenant_id=tid,
        object_id=oid,
        name=str(claims.get("name") or username or oid),
        username=str(username) if username else None,
    )


@dataclass(frozen=True, slots=True)
class MicrosoftProfile:
    email: str | None
    organization: str | None


async def fetch_microsoft_profile(
    http: httpx.AsyncClient, *, access_token: str
) -> MicrosoftProfile:
    """The person's address and their directory's name, each None when Graph will not say.

    Best effort: the token reaches Graph only where the tenant consented User.Read, so a
    403 or a slow answer is expected and never fails the sign-in.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # httpx times each phase of a request; this bounds the whole lookup.
        async with asyncio.timeout(MICROSOFT_PROFILE_TIMEOUT_S):
            me, org = await asyncio.gather(
                _graph_value(http, f"{GRAPH_ROOT}/me?$select=mail,userPrincipalName", headers),
                _graph_value(http, f"{GRAPH_ROOT}/organization?$select=displayName", headers),
            )
    except TimeoutError:
        log.info("microsoft_sign_in.profile_unavailable", error="TimeoutError")
        return MicrosoftProfile(email=None, organization=None)
    # `mail` is empty for an account with no mailbox; the UPN is then the sign-in address.
    email = _text(me, "mail") or _text(me, "userPrincipalName")
    organizations = org.get("value") if org else None
    first: object = (
        cast(list[object], organizations)[0]
        if isinstance(organizations, list) and organizations
        else None
    )
    return MicrosoftProfile(
        email=email if email and "@" in email else None,
        organization=_text(first, "displayName"),
    )


def _text(body: object, key: str) -> str | None:
    if not isinstance(body, dict):
        return None
    value = cast(dict[str, object], body).get(key)
    return value.strip() or None if isinstance(value, str) else None


async def _graph_value(
    http: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> dict[str, object] | None:
    """One Graph GET as a JSON object, or None with the reason logged."""
    try:
        response = await http.get(url, headers=headers, timeout=MICROSOFT_PROFILE_TIMEOUT_S)
        if response.status_code != 200:
            log.info("microsoft_sign_in.profile_unavailable", status=response.status_code)
            return None
        body: object = response.json()
    except (httpx.HTTPError, ValueError) as err:
        log.info("microsoft_sign_in.profile_unavailable", error=type(err).__name__)
        return None
    return cast(dict[str, object], body) if isinstance(body, dict) else None
