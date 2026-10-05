"""Browser-bound GitHub App repository connection flow."""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.branded_pages import branded_page
from daimon.core.config import Settings
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_requester_access import list_github_pages
from daimon.core.stores import github_connect
from daimon.core.stores.accounts import get_account_with_tenant
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
    owner_type: str
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


class _MembershipPayload(BaseModel):
    organization: _OwnerPayload
    state: str
    role: str


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
                owner_type=parsed.account.type,
                repos=tuple(repos),
            )
        )
    return result


async def _owned_orgs(client: httpx.AsyncClient, token: str) -> set[int]:
    rows = await list_github_pages(
        client, "/user/memberships/orgs", token, "memberships", params={"state": "active"}
    )
    result: set[int] = set()
    for row in rows:
        membership = _MembershipPayload.model_validate(row)
        if membership.state == "active" and membership.role == "admin":
            result.add(membership.organization.id)
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
    ):
        raise ValueError("GitHub connection is not configured")
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=20.0, follow_redirects=False))
    client_id = config.client_id
    secret = config.client_secret.get_secret_value()
    callback_url = f"{root}/oauth/github/callback"

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
        else:
            fields = {}
            state = request.query_params.get("state", "")
        cookie = request.cookies.get(_COOKIE, "")
        async with sessionmaker() as session:
            flow = await github_connect.get_flow(session, state=state, cookie=cookie)
            if flow is None or flow.encrypted_user_token is None or flow.github_user_id is None:
                return _error()
            invitation = await github_connect.get_invitation(session, flow.invitation_hash)
            if invitation is None:
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
                owners = await _owned_orgs(client, token)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return _error("GitHub could not verify repository access.", 502)
        if request.method == "POST":
            try:
                selected_ids = [int(value) for value in fields.get("repo", [])]
                org_ids = [int(value) for value in fields.get("org", [])]
            except ValueError:
                return _error("Selection could not be verified.")
            if not selected_ids and not org_ids:
                return _error("Select at least one repository or organization.")
            visible = {
                repo.id: repo for install in installations for repo in install.repos if repo.admin
            }
            orgs = {
                install.owner_id: install
                for install in installations
                if install.owner_type == "Organization" and install.owner_id in owners
            }
            if len(set(selected_ids)) != len(selected_ids) or len(set(org_ids)) != len(org_ids):
                return _error("Selection could not be verified.")
            if any(repo_id not in visible for repo_id in selected_ids) or any(
                owner_id not in orgs for owner_id in org_ids
            ):
                return _error("Selection could not be verified.", 403)
            repos: list[github_connect.RepoConfirmation] = []
            scopes: list[github_connect.OrgConfirmation] = []
            for repo_id in selected_ids:
                repo = visible[repo_id]
                access = fields.get(f"access_{repo_id}", ["read"])[0]
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
            for owner_id in org_ids:
                org = orgs[owner_id]
                access = fields.get(f"org_access_{owner_id}", ["read"])[0]
                if access not in ("read", "write"):
                    return _error("Selection could not be verified.")
                for repo in org.repos:
                    if repo.admin and repo.id not in selected_ids:
                        repos.append(
                            github_connect.RepoConfirmation(
                                repo_id=repo.id,
                                owner_id=repo.owner_id,
                                installation_id=repo.installation_id,
                                full_name=repo.full_name,
                                max_access=access,
                            )
                        )
                scopes.append(
                    github_connect.OrgConfirmation(
                        owner_id=org.owner_id,
                        installation_id=org.id,
                        owner_login=org.owner_login,
                        max_access=access,
                    )
                )
            async with sessionmaker.begin() as session:
                saved = await github_connect.confirm(
                    session,
                    state=state,
                    cookie=cookie,
                    github_user_id=flow.github_user_id,
                    repos=repos,
                    orgs=scopes,
                )
                if saved:
                    from daimon.core.stores.security_audit import append_event

                    await append_event(
                        session,
                        tenant_id=invitation.tenant_id,
                        account_id=invitation.requester_account_id,
                        agent_id=None,
                        platform=requester.platform,
                        platform_user_id=requester.platform_user_id,
                        tool_name="github_connect",
                        operation="github_connect",
                        outcome="allowed",
                        reason="confirmed",
                        github_repo_ids=[repo.repo_id for repo in repos],
                    )
            if not saved:
                return _error()
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
            response = branded_page(
                title="GitHub connected",
                state_bar="",
                body_html=f"<h1>Connected: {len(repos)} repos</h1>",
            )
            response.delete_cookie(_COOKIE)
            return response

        workspace = invitation.workspace_label
        requested_by = invitation.requester_label
        heading = html.escape(f"Connect to workspace {workspace}, requested by {requested_by}")
        parts = [
            f"<h1>{heading}</h1>",
            "<p>Select repositories and choose their maximum access.</p>",
            f'<form method="post" action="{html.escape(root, quote=True)}/oauth/github/confirm">',
            f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">',
        ]
        access_options = '<option value="read">Read</option><option value="write">Write</option>'
        for installation in installations:
            admin_repos = [repo for repo in installation.repos if repo.admin]
            if not admin_repos and installation.owner_id not in owners:
                continue
            parts.append(f"<h2>{html.escape(installation.owner_login)}</h2>")
            for repo in admin_repos:
                parts.append(
                    f'<label><input type="checkbox" name="repo" value="{repo.id}">'
                    f"{html.escape(repo.full_name)}</label>"
                    f'<select name="access_{repo.id}">{access_options}</select><br>'
                )
            if installation.owner_type == "Organization" and installation.owner_id in owners:
                parts.append(
                    f'<label><input type="checkbox" name="org" value="{installation.owner_id}">'
                    "All repos in this org, including future ones</label>"
                    f'<select name="org_access_{installation.owner_id}">'
                    f"{access_options}</select><br>"
                )
        parts.append('<button type="submit">Connect selected</button></form>')
        install_url = (
            f"https://github.com/apps/{html.escape(config.app_slug or '', quote=True)}"
            f"/installations/new?{urlencode({'state': state})}"
        )
        parts.append(f'<p><a href="{install_url}">Install the GitHub App</a></p>')
        return branded_page(title="Connect GitHub", state_bar="", body_html="".join(parts))

    return connect, callback, setup, confirm
