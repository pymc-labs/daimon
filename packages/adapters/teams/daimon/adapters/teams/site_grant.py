"""Grant daimon's `Sites.Selected` app access to the SharePoint site of one Teams channel.

An admin signs in once from the channel. Their delegated token finds its Files
folder (`filesFolder`, which needs `Files.Read.All` and, in a private or shared
channel, their membership) and lets daimon add its own app to the permissions
of the site holding it (`Sites.FullControl.All`): the team's site for a
standard channel, the channel's own for a private or shared one. The folder is
stored, since the app cannot look it up. Commercial Microsoft 365 only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx
import structlog
from daimon.core.config import TeamsSettings
from daimon.core.errors import DaimonError
from daimon.core.teams_graph import is_sharepoint_host, path_segment
from daimon.core.teams_sharepoint import DriveFolder
from fastapi import Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError

log = structlog.get_logger(__name__)

CALLBACK_PATH = "/oauth/teams/files/callback"
STATE_TTL_S = 900

_LOGIN = "https://login.microsoftonline.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = (
    "https://graph.microsoft.com/Sites.FullControl.All https://graph.microsoft.com/Files.Read.All"
)
# Separates this HMAC key from every other use of the Teams client secret; v2 adds the channel.
_STATE_KEY_LABEL = b"daimon.teams-site-grant.state.v2"
_TIMEOUT_S = 15.0
_ADMIN_NEEDED = "Sign in as a SharePoint or global admin who is a member of this channel."


class SiteGrantFailed(DaimonError):
    """The grant did not happen. The message is safe to show the admin."""


class _Token(BaseModel):
    access_token: str


class _Site(BaseModel):
    id: str
    web_url: str = Field(alias="webUrl")
    display_name: str = Field(default="", alias="displayName")


class _Parent(BaseModel):
    drive_id: str = Field(alias="driveId")


class _Folder(BaseModel):
    id: str
    web_url: str = Field(alias="webUrl")
    parent_reference: _Parent = Field(alias="parentReference")


@dataclass(frozen=True)
class GrantTarget:
    """The channel an Enable files sign-in was started from, and its team's Entra group."""

    group_id: str
    channel_id: str


@dataclass(frozen=True)
class GrantedSite:
    """The site daimon was granted, by name for the admin, and the channel's folder on it."""

    name: str
    site_id: str
    folder: DriveFolder


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: bytes, secret: str) -> bytes:
    key = hmac.new(secret.encode(), _STATE_KEY_LABEL, hashlib.sha256).digest()
    return hmac.new(key, body, hashlib.sha256).digest()


def sign_state(target: GrantTarget, *, secret: str, now: float) -> str:
    """Sign `target` with an expiry, as `base64url(body).base64url(mac)`."""
    if not target.channel_id:
        raise ValueError("a grant needs its channel")
    body = f"{uuid.UUID(target.group_id)}:{int(now) + STATE_TTL_S}:{target.channel_id}".encode()
    return f"{_b64(body)}.{_b64(_sign(body, secret))}"


def verify_state(state: str, *, secret: str, now: float) -> GrantTarget | None:
    """Return the signed target, or None if `state` is forged, expired or malformed."""
    try:
        b64_body, b64_mac = state.split(".")
        body, mac = _unb64(b64_body), _unb64(b64_mac)
        if not hmac.compare_digest(mac, _sign(body, secret)):
            return None
        # Channel ids hold colons (`19:…@thread.tacv2`): the channel is the rest.
        group_id, expires, channel_id = body.decode().split(":", 2)
        if now > int(expires) or not channel_id:
            return None
        return GrantTarget(group_id=str(uuid.UUID(group_id)), channel_id=channel_id)
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


async def grant_channel_site(
    http: httpx.AsyncClient,
    *,
    code: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    target: GrantTarget,
) -> GrantedSite:
    """Redeem the admin's code, find the channel's folder and grant this app write on its site."""
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

        channel = (
            f"teams/{path_segment(target.group_id)}/channels/{path_segment(target.channel_id)}"
        )
        folder_response = await http.get(
            f"{_GRAPH}/{channel}/filesFolder",
            headers=headers,
            timeout=_TIMEOUT_S,
            follow_redirects=False,
        )
        # A new channel's site is made when its Files tab is first opened.
        _check(
            folder_response,
            step="folder",
            refused="Could not find this channel's files. Open its Files tab once, then try again.",
        )
        folder = _Folder.model_validate_json(folder_response.content)

        site_response = await http.get(
            f"{_GRAPH}/sites/{_site_path(folder.web_url)}",
            headers=headers,
            timeout=_TIMEOUT_S,
            follow_redirects=False,
        )
        _check(site_response, step="site", refused="Could not find this channel's SharePoint site.")
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
    return GrantedSite(
        name=site.display_name or site.web_url,
        site_id=site.id,
        folder=DriveFolder(drive_id=folder.parent_reference.drive_id, item_id=folder.id),
    )


def _site_path(web_url: str) -> str:
    """`host:/sites/name` of the site holding a folder, from the folder's URL."""
    try:
        url = httpx.URL(web_url)
    except httpx.InvalidURL as err:
        raise SiteGrantFailed("Microsoft sent an unexpected reply. Try again.") from err
    parts = url.path.strip("/").split("/")
    if (
        not is_sharepoint_host(url)
        or len(parts) < 2
        or parts[0] not in ("sites", "teams")
        or parts[1] in ("", ".", "..")
    ):
        raise SiteGrantFailed("This channel's files are not on a SharePoint site.")
    return f"{url.host}:/{parts[0]}/{path_segment(parts[1])}"


def _page(title: str, text: str, *, status: int = 200) -> HTMLResponse:
    body = (
        f"<!doctype html><meta charset=utf-8><title>daimon</title>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
    )
    return HTMLResponse(body, status_code=status)


# Stores what a grant found and drops what was learned about the channel's files.
OnGranted = Callable[[GrantTarget, GrantedSite], Awaitable[None]]


def callback_route(
    settings: TeamsSettings, http: httpx.AsyncClient, *, on_granted: OnGranted
) -> Callable[[Request], Awaitable[HTMLResponse]]:
    """The sign-in's redirect target; the signed state is the only auth it needs."""
    secret = settings.client_secret.get_secret_value()

    async def callback(request: Request) -> HTMLResponse:
        query = request.query_params
        if "error" in query:
            text = (
                "The sign-in did not complete. The first sign-in in the organisation must be "
                "a global admin, who approves the permission for everyone."
            )
            return _page("Files not turned on", text, status=400)
        target = verify_state(query.get("state", ""), secret=secret, now=time.time())
        if target is None or not settings.public_url:
            return _page(
                "Files not turned on", "This link expired. Start again from Teams.", status=400
            )
        try:
            site = await grant_channel_site(
                http,
                code=query.get("code", ""),
                tenant_id=settings.tenant_id,
                client_id=settings.client_id,
                client_secret=secret,
                redirect_uri=redirect_uri(settings.public_url),
                target=target,
            )
        except SiteGrantFailed as err:
            log.warning("teams_site_grant.failed", group_id=target.group_id, reason=str(err))
            return _page("Files not turned on", str(err), status=502)
        log.info("teams_site_grant.granted", group_id=target.group_id)
        await on_granted(target, site)
        return _page(
            "Files are on for this channel",
            f"daimon can now read and save files in {site.name}. "
            "Close this tab and go back to Teams.",
        )

    return callback


def redirect_uri(public_url: object) -> str:
    """The callback's URL under the Teams service's public base URL."""
    return str(public_url).rstrip("/") + CALLBACK_PATH
