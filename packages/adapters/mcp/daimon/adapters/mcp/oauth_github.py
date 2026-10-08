"""Browser-bound GitHub App repository connection flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.branded_pages import branded_page
from daimon.core.config import Settings
from daimon.core.github_app_auth import build_app_jwt, get_app_installation_details
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_requester_access import list_github_pages
from daimon.core.stores import github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.security_audit import append_event
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

RouteHandler = Callable[[Request], Awaitable[Response]]
ClientFactory = Callable[[], httpx.AsyncClient]
_COOKIE = "daimon_gh_connect"
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Repo:
    id: int
    owner_id: int
    installation_id: int
    full_name: str
    admin: bool


@dataclass(frozen=True)
class _Installation:
    id: int
    owner_id: int
    owner_login: str
    repos: tuple[_Repo, ...]


class _OwnerPayload(BaseModel):
    id: int
    login: str = ""
    type: str = ""


class _InstallationPayload(BaseModel):
    id: int
    account: _OwnerPayload


class _RepoPayload(BaseModel):
    id: int
    owner: _OwnerPayload
    full_name: str = ""
    permissions: dict[str, bool] = Field(default_factory=dict)


class _TokenPayload(BaseModel):
    access_token: str


class _UserPayload(BaseModel):
    id: int


def _error(
    message: str = "This connection link expired or is invalid.", status: int = 400
) -> Response:
    return branded_page(
        title="GitHub connection",
        state_bar=" status-bar--rose",
        body_html=f"<h1>Connection unavailable</h1><p>{html.escape(message)}</p>",
        status=status,
    )


def _github_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _receipt_signature(state: str, invitation_hash: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), f"{state}:{invitation_hash}".encode(), hashlib.sha256
    ).hexdigest()


async def _installations(client: httpx.AsyncClient, token: str) -> list[_Installation]:
    raw = await list_github_pages(client, "/user/installations", token, "installations")
    result: list[_Installation] = []
    for item in raw:
        parsed = _InstallationPayload.model_validate(item)
        installation_id = parsed.id
        owner_id = parsed.account.id
        repos_raw = await list_github_pages(
            client, f"/user/installations/{installation_id}/repositories", token, "repositories"
        )
        repos: list[_Repo] = []
        for raw_repo in repos_raw:
            repo = _RepoPayload.model_validate(raw_repo)
            if repo.owner.id != owner_id:
                continue
            repos.append(
                _Repo(
                    id=repo.id,
                    owner_id=owner_id,
                    installation_id=installation_id,
                    full_name=repo.full_name,
                    admin=repo.permissions.get("admin") is True,
                )
            )
        result.append(
            _Installation(
                id=installation_id,
                owner_id=owner_id,
                owner_login=parsed.account.login,
                repos=tuple(repos),
            )
        )
    return result


def build_oauth_github_routes(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    client_factory: ClientFactory | None = None,
) -> tuple[RouteHandler, RouteHandler, RouteHandler, RouteHandler]:
    """Build connect, callback, setup and confirmation handlers."""
    config = settings.github_app
    root = settings.mcp.app_root_url
    if (
        root is None
        or config.client_id is None
        or config.client_secret is None
        or config.app_slug is None
        or config.app_id is None
        or config.private_key is None
    ):
        raise ValueError("GitHub connection is not configured")
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=20.0, follow_redirects=False))
    client_id = config.client_id
    secret = config.client_secret.get_secret_value()
    app_id = config.app_id
    private_key = config.private_key.get_secret_value()
    callback_url = f"{root}/oauth/github/callback"

    async def revoke_user_token(token: str) -> None:
        try:
            async with factory() as client:
                revocation = await client.request(
                    "DELETE",
                    f"https://api.github.com/applications/{client_id}/token",
                    auth=(client_id, secret),
                    json={"access_token": token},
                    headers={"Accept": "application/vnd.github+json"},
                )
                revocation.raise_for_status()
        except httpx.HTTPError:
            _log.warning("GitHub connection token revocation failed")

    async def already_connected(
        state: str, cookie: str, invitation_hash: str = ""
    ) -> Response | None:
        async with sessionmaker() as session:
            receipt = await github_connect.successful_confirmation(
                session, state=state, cookie=cookie, invitation_hash=invitation_hash
            )
        if receipt is None:
            return None
        count = receipt.connected_repo_count
        return branded_page(
            title="GitHub connected",
            state_bar="",
            body_html=f"<h1>Already connected: {count} repos</h1>",
        )

    async def connect(request: Request) -> Response:
        token = request.path_params["token"]
        invitation_hash = github_connect.digest(token)
        async with sessionmaker() as session:
            if await github_connect.get_invitation(session, invitation_hash) is None:
                return _error()
        state = secrets.token_urlsafe(32)
        cookie = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        async with sessionmaker.begin() as session:
            if await github_connect.get_invitation(session, invitation_hash) is None:
                return _error()
            await github_connect.create_flow(
                session,
                invitation_hash=invitation_hash,
                state=state,
                cookie=cookie,
                encrypted_verifier=encrypt_token(fernet, verifier),
            )
        params = urlencode(
            {
                "client_id": config.client_id,
                "redirect_uri": callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
        response.set_cookie(
            _COOKIE,
            cookie,
            max_age=900,
            httponly=True,
            secure=root.startswith("https://"),
            samesite="lax",
        )
        return response

    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        cookie = request.cookies.get(_COOKIE, "")
        if not code:
            return _error()
        async with sessionmaker() as session:
            flow = await github_connect.get_flow(session, state=state, cookie=cookie)
        if flow is None or flow.encrypted_user_token is not None:
            return _error()
        verifier = decrypt_token(fernet, flow.encrypted_verifier)
        try:
            async with factory() as client:
                token_response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": config.client_id,
                        "client_secret": secret,
                        "code": code,
                        "redirect_uri": callback_url,
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                user_token = _TokenPayload.model_validate(token_response.json()).access_token
                user_response = await client.get(
                    "https://api.github.com/user", headers=_github_headers(user_token)
                )
                user_response.raise_for_status()
                user_id = _UserPayload.model_validate(user_response.json()).id
        except (httpx.HTTPError, ValueError):
            return _error("GitHub could not complete the connection.", 502)
        async with sessionmaker.begin() as session:
            if await github_connect.get_flow(session, state=state, cookie=cookie) is None:
                return _error()
            saved = await github_connect.set_user_token(
                session,
                state=state,
                encrypted_token=encrypt_token(fernet, user_token),
                github_user_id=user_id,
            )
            if not saved:
                return _error()
        return RedirectResponse(f"{root}/oauth/github/confirm?{urlencode({'state': state})}")

    async def setup(request: Request) -> Response:
        # GitHub's installation_id is untrusted. Confirmation always re-lists as the user.
        state = request.query_params.get("state", "")
        async with sessionmaker() as session:
            flow = await github_connect.get_flow(
                session, state=state, cookie=request.cookies.get(_COOKIE, "")
            )
        if flow is None or flow.encrypted_user_token is None:
            return _error()
        return RedirectResponse(f"{root}/oauth/github/confirm?{urlencode({'state': state})}")

    async def confirm(request: Request) -> Response:
        if request.method == "POST":
            try:
                fields = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            except UnicodeDecodeError:
                return _error("Selection could not be verified.")
            state = fields.get("state", [""])[0]
            invitation_hash = fields.get("invitation", [""])[0]
            signature = fields.get("receipt", [""])[0]
            expected = _receipt_signature(state, invitation_hash, secret)
            if not hmac.compare_digest(signature, expected):
                invitation_hash = ""
        else:
            fields = {}
            state = request.query_params.get("state", "")
            invitation_hash = ""
        cookie = request.cookies.get(_COOKIE, "")
        used = await already_connected(state, cookie, invitation_hash)
        if used is not None:
            return used
        async with sessionmaker() as session:
            flow = await github_connect.get_flow(session, state=state, cookie=cookie)
            if flow is None or flow.encrypted_user_token is None or flow.github_user_id is None:
                used = await already_connected(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            invitation = await github_connect.get_invitation(session, flow.invitation_hash)
            if invitation is None:
                used = await already_connected(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if requester is None or requester.is_external:
                return _error()
        token = decrypt_token(fernet, flow.encrypted_user_token)
        try:
            async with factory() as client:
                installations = await _installations(client, token)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            used = await already_connected(state, cookie, invitation_hash)
            if used is not None:
                return used
            return _error("GitHub could not verify repository access.", 502)
        if request.method == "POST":
            try:
                selected_ids = [int(value) for value in fields.get("repo", [])]
            except ValueError:
                return _error("Selection could not be verified.")
            if not selected_ids:
                return _error("Select at least one repository.")
            visible = {
                repo.id: repo for install in installations for repo in install.repos if repo.admin
            }
            if len(set(selected_ids)) != len(selected_ids):
                return _error("Selection could not be verified.")
            if any(repo_id not in visible for repo_id in selected_ids):
                return _error("Selection could not be verified.", 403)
            repos: list[github_connect.RepoConfirmation] = []
            for repo_id in selected_ids:
                repo = visible[repo_id]
                access = fields.get(f"access_{repo_id}", ["write"])[0]
                if access not in ("read", "write"):
                    return _error("Selection could not be verified.")
                repos.append(
                    github_connect.RepoConfirmation(
                        repo_id=repo.id,
                        owner_id=repo.owner_id,
                        installation_id=repo.installation_id,
                        full_name=repo.full_name,
                        max_access=access,
                    )
                )
            app_jwt = build_app_jwt(private_key, app_id, now=int(time.time()))
            by_installation = {installation.id: installation for installation in installations}
            try:
                async with factory() as client:
                    details = [
                        await get_app_installation_details(
                            client, jwt=app_jwt, installation_id=installation_id
                        )
                        for installation_id in sorted({repo.installation_id for repo in repos})
                    ]
            except (httpx.HTTPError, ValueError):
                used = await already_connected(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error("GitHub could not verify the App installation.", 502)
            if any(
                detail.account_id != by_installation[detail.installation_id].owner_id
                or detail.account_login.casefold()
                != by_installation[detail.installation_id].owner_login.casefold()
                or detail.suspended_at is not None
                for detail in details
            ):
                return _error("GitHub App installation is unavailable.", 403)
            activation_status = None
            try:
                async with sessionmaker.begin() as session:
                    saved = await github_connect.confirm(
                        session,
                        state=state,
                        cookie=cookie,
                        github_user_id=flow.github_user_id,
                        repos=repos,
                    )
                    if saved:
                        for detail in details:
                            await github_app_installations.upsert_github_app(
                                session,
                                installation_id=detail.installation_id,
                                account_id=detail.account_id,
                                account_login=detail.account_login,
                                account_type=detail.account_type,
                                repository_selection=detail.repository_selection,
                                suspended_at=detail.suspended_at,
                            )
                        activation_status = await github_connect.activate_confirmed_agent(
                            session, invitation=invitation, repos=repos
                        )
                        await append_event(
                            session,
                            tenant_id=invitation.tenant_id,
                            account_id=invitation.requester_account_id,
                            agent_id=invitation.agent_id,
                            platform=requester.platform,
                            platform_user_id=requester.platform_user_id,
                            tool_name="github_connect",
                            operation="github_connect",
                            outcome="allowed",
                            reason="confirmed",
                            github_repo_ids=[repo.repo_id for repo in repos],
                        )
            except github_connect.ClientAgentConnectionError:
                await revoke_user_token(token)
                return _error(github_connect.CLIENT_AGENT_MESSAGE)
            except ValueError:
                await revoke_user_token(token)
                return _error(
                    "This connection could not be completed. Start a new GitHub connection."
                )
            if not saved:
                await revoke_user_token(token)
                used = await already_connected(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            await revoke_user_token(token)
            if activation_status == "activated":
                name = html.escape(invitation.agent_name or "agent")
                body = f"<h1>Connected {len(repos)} repos to {name}.</h1>"
            elif activation_status == "update_pending":
                name = html.escape(invitation.agent_name or "This agent")
                body = (
                    f"<h1>Connected {len(repos)} repos.</h1>"
                    f"<p>{name} still uses a saved key. "
                    "An admin must run /github connect in Discord or Slack, then confirm "
                    "Update and restart chats.</p>"
                )
            else:
                body = f"<h1>Connected: {len(repos)} repos</h1>"
            response = branded_page(title="GitHub connected", state_bar="", body_html=body)
            response.set_cookie(
                _COOKIE,
                cookie,
                max_age=7 * 24 * 60 * 60,
                httponly=True,
                secure=root.startswith("https://"),
                samesite="lax",
            )
            return response

        workspace = invitation.workspace_label
        requested_by = invitation.requester_label
        heading = html.escape(f"Connect to workspace {workspace}, requested by {requested_by}")
        parts = [
            f"<h1>{heading}</h1>",
            "<p>Select repositories and choose their maximum access.</p>",
            '<form id="github-connect-form" method="post" '
            f'action="{html.escape(root, quote=True)}/oauth/github/confirm">',
            f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">',
            '<input type="hidden" name="invitation" '
            f'value="{html.escape(flow.invitation_hash, quote=True)}">',
            '<input type="hidden" name="receipt" '
            f'value="{_receipt_signature(state, flow.invitation_hash, secret)}">',
            '<button type="button" id="select-all-repos">Select all repos you administer</button>',
            '<script>document.getElementById("select-all-repos").addEventListener("click", '
            '() => document.querySelectorAll("input[name=repo]").forEach(box => '
            "{ box.checked = true; }));</script>",
        ]
        access_options = (
            '<option value="write" selected>Read and write</option>'
            '<option value="read">Read only</option>'
        )
        parts.append(
            "<p>Read and write: Push branches, open issues and pull requests. "
            "Read only: Read code, issues and pull requests.</p>"
        )
        for installation in installations:
            admin_repos = [repo for repo in installation.repos if repo.admin]
            if not admin_repos:
                continue
            parts.append(f"<h2>{html.escape(installation.owner_login)}</h2>")
            for repo in admin_repos:
                parts.append(
                    f'<label><input type="checkbox" name="repo" value="{repo.id}">'
                    f"{html.escape(repo.full_name)}</label>"
                    f'<select name="access_{repo.id}">{access_options}</select><br>'
                )
        parts.append('<button id="connect-repos" type="submit">Connect repos</button></form>')
        parts.append(
            '<script>let connecting = false; document.getElementById("github-connect-form")'
            '.addEventListener("submit", event => {'
            "if (connecting) { event.preventDefault(); return; }"
            'connecting = true; const button = document.getElementById("connect-repos");'
            'button.disabled = true; button.textContent = "Connecting…"; });</script>'
        )
        install_url = (
            f"https://github.com/apps/{html.escape(config.app_slug or '', quote=True)}"
            f"/installations/new?{urlencode({'state': state})}"
        )
        parts.append(f'<p><a href="{install_url}">Install the GitHub App</a></p>')
        return branded_page(title="Connect GitHub", state_bar="", body_html="".join(parts))

    return connect, callback, setup, confirm
