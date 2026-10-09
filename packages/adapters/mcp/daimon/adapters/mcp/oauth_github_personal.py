"""Verified Discord/Slack identity before a personal GitHub App link."""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.branded_pages import branded_page
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.github_request_delivery import deliver_private_request_card
from daimon.core.config import Settings
from daimon.core.github_app_session import effective_repo_state
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_request_cards import RequestCard
from daimon.core.stores.github_access import list_authorized_repos
from daimon.core.stores.github_access_requests import (
    AccessRequest,
    get_delivery,
    list_asker_requests,
    ready_and_continue,
)
from daimon.core.stores.github_links import save_verified_link, verified_platform_account
from daimon.core.stores.github_personal_links import digest, find_by_state, get_intent, mint_link
from daimon.core.stores.security_audit import append_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

RouteHandler = Callable[[Request], Awaitable[Response]]
OptionalRouteHandler = Callable[[Request], Awaitable[Response | None]]
ClientFactory = Callable[[], httpx.AsyncClient]
_COOKIE = "daimon_gh_personal"
_log = logging.getLogger(__name__)


def _page(
    message: str,
    *,
    status: int = 400,
    back: str | None = None,
    action: tuple[str, str] | None = None,
) -> Response:
    link = f"<p>{back}</p>" if back else ""
    if action is not None:
        label, url = action
        link = f'<p><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></p>' + link
    return branded_page(
        title="GitHub",
        state_bar=" status-bar--rose" if status >= 400 else "",
        body_html=f"<h1>{html.escape(message)}</h1>{link}",
        status=status,
    )


def _back(platform: str, workspace_id: str) -> str:
    if platform == "discord":
        return (
            f'<a href="https://discord.com/channels/{html.escape(workspace_id, quote=True)}">'
            "Back to Discord</a>"
        )
    return "In Slack, run <code>/github</code>."


async def _resume_linked_requests(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    fernet: MultiFernet,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> tuple[list[AccessRequest], set[uuid.UUID]]:
    async with sessionmaker() as session:
        pending = await list_asker_requests(session, tenant_id=tenant_id, account_id=account_id)
        repos = await list_authorized_repos(session, tenant_id=tenant_id)
    by_name = {repo.repo_full_name.casefold(): repo.repo_id for repo in repos}
    ready: set[uuid.UUID] = set()
    for request in pending:
        try:
            names, permissions = await effective_repo_state(
                sessionmaker,
                tenant_id=tenant_id,
                agent_id=request.agent_id,
                account_id=account_id,
                is_external=False,
                config=settings.github_app,
                fernet=fernet,
            )
        except (ValueError, httpx.HTTPError):
            continue
        available = {name.removeprefix("https://github.com/").casefold() for name in names}
        if not all(name.casefold() in available for name in request.repo_names):
            continue
        if request.required_ability == "write" and not all(
            permissions.get(by_name.get(name.casefold(), -1), {}).get("contents") == "write"
            for name in request.repo_names
        ):
            continue
        async with sessionmaker.begin() as session:
            if await ready_and_continue(session, tenant_id=tenant_id, request_id=request.id):
                ready.add(request.id)
    return pending, ready


def build_personal_link_routes(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    runtime: McpRuntime | None = None,
    client_factory: ClientFactory | None = None,
) -> tuple[RouteHandler, RouteHandler, OptionalRouteHandler]:
    root = settings.mcp.app_root_url
    github = settings.github_app
    if root is None or github.client_id is None or github.client_secret is None:
        raise ValueError("GitHub linking is not configured")
    github_client_secret = github.client_secret.get_secret_value()
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=20.0, follow_redirects=False))
    platform_callback_url = f"{root}/oauth/github/link/platform-callback"
    github_callback_url = f"{root}/oauth/github/callback"

    async def retry_link(
        *, tenant_id: uuid.UUID, account_id: uuid.UUID, platform: str, user_id: str
    ) -> str | None:
        try:
            async with sessionmaker.begin() as session:
                return await mint_link(
                    session,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    platform=platform,
                    platform_user_id=user_id,
                    root_url=str(root),
                )
        except ValueError:
            return None

    async def update_failed_cards(
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        platform: str,
        workspace_id: str,
        user_id: str,
        link_url: str | None,
        message: str = "GitHub wasn't linked.",
        action_label: str = "Try again",
    ) -> None:
        if runtime is None or link_url is None:
            return
        async with sessionmaker() as session:
            pending = await list_asker_requests(session, tenant_id=tenant_id, account_id=account_id)
        for waiting in pending:
            async with sessionmaker() as session:
                delivery = await get_delivery(
                    session,
                    tenant_id=tenant_id,
                    request_id=waiting.id,
                    recipient_account_id=account_id,
                )
            if delivery is None or delivery.message_id is None:
                continue
            await deliver_private_request_card(
                runtime,
                tenant_id=tenant_id,
                platform=platform,
                workspace_id=workspace_id,
                request_id=waiting.id,
                recipient_account_id=account_id,
                platform_user_id=user_id,
                card=RequestCard(message, action_label, ("Cancel request",)),
                link_url=link_url,
            )

    async def start(request: Request) -> Response:
        token = request.path_params["token"]
        cookie = secrets.token_urlsafe(32)
        state = "link_" + secrets.token_urlsafe(32)
        platform = ""
        workspace_id = ""
        async with sessionmaker.begin() as session:
            intent = await get_intent(
                session, token_hash=digest(token), lock=True, include_expired=True
            )
            if intent is None:
                return _page("This link expired. Ask again in chat.")
            if intent.expires_at <= datetime.now(UTC):
                return _page("This link expired. Ask again in chat.")
            else:
                if intent.phase != "new":
                    return _page("This link expired. Ask again in chat.")
                platform = intent.platform
                workspace_id = intent.platform_workspace_id
                configured = (
                    settings.hub.discord_configured
                    if platform == "discord"
                    else settings.hub.slack_configured
                    or bool(
                        settings.slack and settings.slack.client_id and settings.slack.client_secret
                    )
                )
                if not configured:
                    return _page("GitHub didn't answer. Try again in a minute.", status=503)
                intent.browser_cookie_hash = digest(cookie)
                intent.platform_state = state
                intent.phase = "platform"
        if platform == "discord":
            client_id = settings.hub.discord_client_id
            secret = settings.hub.discord_client_secret
            endpoint = "https://discord.com/oauth2/authorize"
            params = {
                "client_id": client_id or "",
                "redirect_uri": platform_callback_url,
                "response_type": "code",
                "scope": "identify",
                "state": state,
            }
        else:
            client_id = settings.hub.slack_client_id or (
                settings.slack.client_id if settings.slack else None
            )
            secret = settings.hub.slack_client_secret or (
                settings.slack.client_secret if settings.slack else None
            )
            endpoint = "https://slack.com/openid/connect/authorize"
            params = {
                "client_id": client_id or "",
                "redirect_uri": platform_callback_url,
                "response_type": "code",
                "scope": "openid",
                "state": state,
                "team": workspace_id,
            }
        if not client_id or secret is None:
            return _page("GitHub didn't answer. Try again in a minute.", status=503)
        response = RedirectResponse(f"{endpoint}?{urlencode(params)}")
        response.set_cookie(
            _COOKIE,
            cookie,
            max_age=600,
            httponly=True,
            secure=root.startswith("https://"),
            samesite="lax",
        )
        return response

    async def platform_callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        cookie_hash = digest(request.cookies.get(_COOKIE, ""))
        async with sessionmaker() as session:
            intent = await find_by_state(session, state=state, phase="platform")
            if intent is None or intent.browser_cookie_hash != cookie_hash:
                return _page("This link expired. Ask again in chat.")
            platform, expected_user, expected_workspace = (
                intent.platform,
                intent.platform_user_id,
                intent.platform_workspace_id,
            )
        if not code:
            fresh = await retry_link(
                tenant_id=intent.tenant_id,
                account_id=intent.account_id,
                platform=platform,
                user_id=expected_user,
            )
            await update_failed_cards(
                tenant_id=intent.tenant_id,
                account_id=intent.account_id,
                platform=platform,
                workspace_id=expected_workspace,
                user_id=expected_user,
                link_url=fresh,
            )
            return _page(
                "GitHub wasn't linked.",
                status=200,
                back=_back(platform, expected_workspace),
                action=("Try again", fresh) if fresh else None,
            )
        if platform == "discord":
            client_id = settings.hub.discord_client_id
            client_secret = settings.hub.discord_client_secret
        else:
            client_id = settings.hub.slack_client_id or (
                settings.slack.client_id if settings.slack else None
            )
            client_secret = settings.hub.slack_client_secret or (
                settings.slack.client_secret if settings.slack else None
            )
        if not client_id or client_secret is None:
            return _page("GitHub didn't answer. Try again in a minute.", status=503)
        try:
            async with factory() as client:
                if platform == "discord":
                    response = await client.post(
                        "https://discord.com/api/oauth2/token",
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret.get_secret_value(),
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": platform_callback_url,
                        },
                    )
                    response.raise_for_status()
                    token = response.json()["access_token"]
                    identity = await client.get(
                        "https://discord.com/api/users/@me",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    identity.raise_for_status()
                    actual_user = str(identity.json()["id"])
                    actual_workspace = expected_workspace
                else:
                    response = await client.post(
                        "https://slack.com/api/openid.connect.token",
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret.get_secret_value(),
                            "code": code,
                            "redirect_uri": platform_callback_url,
                        },
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not body.get("ok"):
                        raise ValueError("Slack sign in failed")
                    identity = await client.post(
                        "https://slack.com/api/openid.connect.userInfo",
                        headers={"Authorization": f"Bearer {body['access_token']}"},
                    )
                    identity.raise_for_status()
                    info = identity.json()
                    if not info.get("ok"):
                        raise ValueError("Slack identity failed")
                    actual_user = str(info["https://slack.com/user_id"])
                    actual_workspace = str(info["https://slack.com/team_id"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return _page("GitHub didn't answer. Try again in a minute.", status=502)
        if actual_user != expected_user or actual_workspace != expected_workspace:
            return _page("This link was made for another person.", status=403)
        github_state = "link_" + secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        async with sessionmaker.begin() as session:
            intent = await find_by_state(session, state=state, phase="platform", lock=True)
            if intent is None or intent.browser_cookie_hash != cookie_hash:
                return _page("This link expired. Ask again in chat.")
            intent.github_state = github_state
            intent.encrypted_verifier = encrypt_token(fernet, verifier)
            intent.phase = "github"
        return RedirectResponse(
            "https://github.com/login/oauth/authorize?"
            + urlencode(
                {
                    "client_id": github.client_id,
                    "redirect_uri": github_callback_url,
                    "state": github_state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    async def github_callback(request: Request) -> Response | None:
        state = request.query_params.get("state", "")
        if not state.startswith("link_"):
            return None
        code = request.query_params.get("code", "")
        cookie_hash = digest(request.cookies.get(_COOKIE, ""))
        async with sessionmaker() as session:
            intent = await find_by_state(session, state=state, phase="github")
            if (
                intent is None
                or intent.browser_cookie_hash != cookie_hash
                or intent.encrypted_verifier is None
            ):
                return _page("This link expired. Ask again in chat.")
            platform, workspace_id = intent.platform, intent.platform_workspace_id
            verifier = decrypt_token(fernet, intent.encrypted_verifier)
        if not code:
            fresh = await retry_link(
                tenant_id=intent.tenant_id,
                account_id=intent.account_id,
                platform=platform,
                user_id=intent.platform_user_id,
            )
            await update_failed_cards(
                tenant_id=intent.tenant_id,
                account_id=intent.account_id,
                platform=platform,
                workspace_id=workspace_id,
                user_id=intent.platform_user_id,
                link_url=fresh,
            )
            return _page(
                "GitHub wasn't linked.",
                status=200,
                back=_back(platform, workspace_id),
                action=("Try again", fresh) if fresh else None,
            )
        try:
            async with factory() as client:
                response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": github.client_id,
                        "client_secret": github_client_secret,
                        "code": code,
                        "redirect_uri": github_callback_url,
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                body = response.json()
                access_token = str(body["access_token"])
                refresh_token = str(body["refresh_token"])
                expires_in = int(body["expires_in"])
                refresh_expires_in = int(body["refresh_token_expires_in"])
                if min(expires_in, refresh_expires_in) <= 0:
                    raise ValueError("GitHub token expiry invalid")
                user_response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                user_response.raise_for_status()
                github_user_id = int(user_response.json()["id"])
                login = str(user_response.json()["login"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            fresh = await retry_link(
                tenant_id=intent.tenant_id,
                account_id=intent.account_id,
                platform=platform,
                user_id=intent.platform_user_id,
            )
            return _page(
                "GitHub didn't answer. Try again in a minute.",
                status=502,
                action=("Try again", fresh) if fresh else None,
            )
        try:
            async with sessionmaker.begin() as session:
                intent = await find_by_state(session, state=state, phase="github", lock=True)
                if intent is None or intent.browser_cookie_hash != cookie_hash:
                    return _page("This link expired. Ask again in chat.")
                if not await verified_platform_account(
                    session,
                    tenant_id=intent.tenant_id,
                    account_id=intent.account_id,
                    platform=intent.platform,
                    platform_user_id=intent.platform_user_id,
                ):
                    return _page("This link expired. Ask again in chat.")
                await save_verified_link(
                    session,
                    intent_account_id=intent.account_id,
                    platform=intent.platform,
                    platform_user_id=intent.platform_user_id,
                    github_user_id=github_user_id,
                    login=login,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_in=expires_in,
                    refresh_expires_in=refresh_expires_in,
                    fernet=fernet,
                )
                intent.phase = "used"
                await append_event(
                    session,
                    tenant_id=intent.tenant_id,
                    account_id=intent.account_id,
                    agent_id=None,
                    platform=intent.platform,
                    platform_user_id=intent.platform_user_id,
                    tool_name="github_link",
                    operation="github_link",
                    outcome="allowed",
                    reason="personal GitHub account linked",
                )
        except ValueError:
            _log.exception("Personal GitHub link failed")
            return _page("GitHub didn't answer. Try again in a minute.", status=502)
        pending, resumed_ids = await _resume_linked_requests(
            sessionmaker,
            settings=settings,
            fernet=fernet,
            tenant_id=intent.tenant_id,
            account_id=intent.account_id,
        )
        if runtime is not None:
            for waiting in pending:
                async with sessionmaker() as session:
                    delivery = await get_delivery(
                        session,
                        tenant_id=intent.tenant_id,
                        request_id=waiting.id,
                        recipient_account_id=intent.account_id,
                    )
                if delivery is None or delivery.message_id is None:
                    continue
                link_url: str | None = None
                card = RequestCard(f"✓ Linked as @{login}", None)
                if waiting.id not in resumed_ids:
                    async with sessionmaker.begin() as session:
                        link_url = await mint_link(
                            session,
                            tenant_id=intent.tenant_id,
                            account_id=intent.account_id,
                            platform=intent.platform,
                            platform_user_id=intent.platform_user_id,
                            root_url=str(root),
                        )
                    card = RequestCard(
                        "This GitHub account doesn't have access to what this request needs.",
                        "Use another GitHub account",
                        ("Cancel request",),
                    )
                await deliver_private_request_card(
                    runtime,
                    tenant_id=intent.tenant_id,
                    platform=intent.platform,
                    workspace_id=workspace_id,
                    request_id=waiting.id,
                    recipient_account_id=intent.account_id,
                    platform_user_id=intent.platform_user_id,
                    card=card,
                    link_url=link_url,
                )
        message = (
            f"Linked as @{login}. You can close this tab; your request is continuing."
            if resumed_ids
            else "This GitHub account doesn't have access to what this request needs."
            if pending
            else f"Linked as @{login}."
        )
        response = _page(
            message,
            status=200,
            back=_back(platform, workspace_id),
        )
        response.delete_cookie(_COOKIE)
        return response

    return start, platform_callback, github_callback
