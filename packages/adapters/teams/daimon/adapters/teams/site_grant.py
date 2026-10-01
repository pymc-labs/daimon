"""Grant daimon's `Sites.Selected` app access to one Teams team's SharePoint site.

An admin signs in once with `Sites.FullControl.All`; that delegated token lets
daimon add its own app to the site's permissions. Commercial Microsoft 365 only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import time
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlencode

import httpx
import structlog
from daimon.core.config import TeamsSettings
from daimon.core.errors import DaimonError
from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

log = structlog.get_logger(__name__)

CALLBACK_PATH = "/oauth/teams/files/callback"
STATE_TTL_S = 900

_LOGIN = "https://login.microsoftonline.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/Sites.FullControl.All"
# Separates this HMAC key from every other use of the Teams client secret.
_STATE_KEY_LABEL = b"daimon.teams-site-grant.state.v1"
_TIMEOUT_S = 15.0
_ADMIN_NEEDED = "Sign in as a SharePoint or global admin."


class SiteGrantFailed(DaimonError):
    """The grant did not happen. The message is safe to show the admin."""


class _Token(BaseModel):
    access_token: str


class _Site(BaseModel):
    id: str
    web_url: str = Field(alias="webUrl")
    display_name: str = Field(default="", alias="displayName")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: bytes, secret: str) -> bytes:
    key = hmac.new(secret.encode(), _STATE_KEY_LABEL, hashlib.sha256).digest()
    return hmac.new(key, body, hashlib.sha256).digest()


def sign_state(group_id: str, *, secret: str, now: float) -> str:
    """Sign `group_id` with an expiry, as `base64url(body).base64url(mac)`."""
    body = f"{uuid.UUID(group_id)}:{int(now) + STATE_TTL_S}".encode()
    return f"{_b64(body)}.{_b64(_sign(body, secret))}"


def verify_state(state: str, *, secret: str, now: float) -> str | None:
    """Return the signed group id, or None if `state` is forged, expired or malformed."""
    try:
        b64_body, b64_mac = state.split(".")
        body, mac = _unb64(b64_body), _unb64(b64_mac)
        if not hmac.compare_digest(mac, _sign(body, secret)):
            return None
        group_id, expires = body.decode().split(":")
        if now > int(expires):
            return None
        return str(uuid.UUID(group_id))
    except ValueError:  # bad base64, UTF-8, split shape, int or UUID
        return None


def authorize_url(*, tenant_id: str, client_id: str, redirect_uri: str, state: str) -> str:
    """The admin's delegated sign-in URL."""
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": _SCOPE,
            "state": state,
            "prompt": "select_account",
        }
    )
    return f"{_LOGIN}/{quote(tenant_id, safe='')}/oauth2/v2.0/authorize?{query}"


def _check(response: httpx.Response, *, step: str, refused: str) -> None:
    if response.is_success:
        return
    # Status only: Microsoft's error bodies can echo request details.
    log.warning("teams_site_grant.step_failed", step=step, status=response.status_code)
    if response.status_code in (401, 403):
        raise SiteGrantFailed(_ADMIN_NEEDED)
    raise SiteGrantFailed(refused)


async def grant_team_site(
    http: httpx.AsyncClient,
    *,
    code: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    group_id: str,
) -> str:
    """Redeem the admin's code and grant this app write on the team's root site.

    Returns the site's display name, or its URL when it has none.
    """
    try:
        token_response = await http.post(
            f"{_LOGIN}/{quote(tenant_id, safe='')}/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "scope": _SCOPE,
            },
            timeout=_TIMEOUT_S,
            follow_redirects=False,
        )
        _check(token_response, step="token", refused="The sign-in expired. Start again from Teams.")
        token = _Token.model_validate_json(token_response.content).access_token
        headers = {"Authorization": f"Bearer {token}"}

        site_response = await http.get(
            f"{_GRAPH}/groups/{quote(group_id, safe='')}/sites/root",
            headers=headers,
            timeout=_TIMEOUT_S,
            follow_redirects=False,
        )
        _check(site_response, step="site", refused="Could not find this team's SharePoint site.")
        site = _Site.model_validate_json(site_response.content)

        permission_response = await http.post(
            f"{_GRAPH}/sites/{quote(site.id, safe=',')}/permissions",
            headers=headers,
            json={
                "roles": ["write"],
                "grantedToIdentities": [
                    {"application": {"id": client_id, "displayName": "daimon"}}
                ],
            },
            timeout=_TIMEOUT_S,
            follow_redirects=False,
        )
        _check(
            permission_response,
            step="permission",
            refused="Microsoft refused to give daimon access to the site.",
        )
    except ValidationError as err:  # also covers bodies that are not JSON
        raise SiteGrantFailed("Microsoft sent an unexpected reply. Try again.") from err
    except httpx.HTTPError as err:
        raise SiteGrantFailed("Could not reach Microsoft. Try again in a minute.") from err
    return site.display_name or site.web_url


def _page(title: str, text: str, *, status: int = 200) -> HTMLResponse:
    body = (
        f"<!doctype html><meta charset=utf-8><title>daimon</title>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
    )
    return HTMLResponse(body, status_code=status)


def callback_route(
    settings: TeamsSettings, http: httpx.AsyncClient, *, on_granted: Callable[[str], None]
) -> Callable[[Request], Awaitable[HTMLResponse]]:
    """The sign-in's redirect target; the signed state is the only auth it needs."""
    secret = settings.client_secret.get_secret_value()

    async def callback(request: Request) -> HTMLResponse:
        query = request.query_params
        if "error" in query:
            return _page("Files not turned on", "The sign-in did not complete.", status=400)
        group_id = verify_state(query.get("state", ""), secret=secret, now=time.time())
        if group_id is None or not settings.public_url:
            return _page(
                "Files not turned on", "This link expired. Start again from Teams.", status=400
            )
        try:
            site = await grant_team_site(
                http,
                code=query.get("code", ""),
                tenant_id=settings.tenant_id,
                client_id=settings.client_id,
                client_secret=secret,
                redirect_uri=redirect_uri(settings.public_url),
                group_id=group_id,
            )
        except SiteGrantFailed as err:
            log.warning("teams_site_grant.failed", group_id=group_id, reason=str(err))
            return _page("Files not turned on", str(err), status=502)
        log.info("teams_site_grant.granted", group_id=group_id)
        on_granted(group_id)
        return _page(
            "Files are on for this team",
            f"daimon can now read and save files in {site}. Close this tab and go back to Teams.",
        )

    return callback


def redirect_uri(public_url: object) -> str:
    """The callback's URL under the Teams service's public base URL."""
    return str(public_url).rstrip("/") + CALLBACK_PATH
