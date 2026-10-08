"""Browser-bound GitHub App repository connection flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.github_pages import github_page
from daimon.core.config import Settings
from daimon.core.github_app_auth import build_app_jwt, get_app_installation_details
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_requester_access import list_github_pages
from daimon.core.stores import github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant, has_external_accounts
from daimon.core.stores.github_request_actions import finish_confirmed_requests
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
    repository_selection: str
    repos: tuple[_Repo, ...]


class _OwnerPayload(BaseModel):
    id: int
    login: str = ""
    type: str = ""


class _InstallationPayload(BaseModel):
    id: int
    account: _OwnerPayload
    repository_selection: str = "all"


class _RepoPayload(BaseModel):
    id: int
    owner: _OwnerPayload
    full_name: str = ""
    permissions: dict[str, bool] = Field(default_factory=dict)


class _TokenPayload(BaseModel):
    access_token: str


class _UserPayload(BaseModel):
    id: int


class _InstallationRequestPayload(BaseModel):
    requester: _UserPayload


def _error(
    message: str = "This link has expired.",
    status: int = 400,
    retry_url: str | None = None,
) -> Response:
    retry = (
        '<div class="gh-actions"><a class="gh-primary" '
        f'href="{html.escape(retry_url, quote=True)}">Try again</a></div>'
        if retry_url
        else ""
    )
    return github_page(title=message, body_html=retry, status=status, error=True)


def _back_to_chat(platform: str, workspace_id: str) -> str:
    if platform == "discord":
        target = f"https://discord.com/channels/{html.escape(workspace_id, quote=True)}"
        return f'<a class="gh-primary" href="{target}">Back to Discord</a>'
    return "In Slack, run <code>/github</code>."


def _repo_count(count: int) -> str:
    return f"{count} {'repo' if count == 1 else 'repos'}"


def _already_connected_page(
    count: int | None,
    back: str = "",
    *,
    agent_name: str | None = None,
    update_pending: bool = False,
) -> Response:
    label = (
        f"Already connected: {_repo_count(count)}." if count is not None else "Already connected."
    )
    detail = (
        f"<p>An operator will finish switching {html.escape(agent_name)}.</p>"
        if agent_name and update_pending
        else (
            f"<p>{html.escape(agent_name)} can use these repos.</p>"
            if agent_name
            else "<p>The repos are connected. Choose an agent in GitHub setup.</p>"
        )
    )
    return github_page(
        title=label,
        body_html=detail
        + "<p>You can close this tab.</p>"
        + (f'<div class="gh-actions">{back}</div>' if back else ""),
    )


def _done_page(
    *,
    count: int,
    platform: str,
    external_id: str,
    requester_label: str,
    same_person: bool,
    agent_name: str | None,
    update_pending: bool,
) -> Response:
    back = _back_to_chat(platform, external_id)
    if update_pending and agent_name:
        return github_page(
            title=f"Repos connected. An operator will finish switching {agent_name}.",
            body_html="<p>You can close this tab.</p>",
        )
    if agent_name:
        ready = (
            f"<p>{html.escape(agent_name)} can use them from your next message.</p>"
            if same_person
            else ""
        )
        body = (
            '<div class="gh-actions">' + back + "</div>"
            if same_person and platform == "discord"
            else (
                f"<p>In Slack, run <code>/github</code> to see "
                f"{html.escape(agent_name)}'s repos.</p>"
                if same_person
                else f"<p>{html.escape(requester_label)} can now use these repos with "
                f"{html.escape(agent_name)}. You can close this tab.</p>"
            )
        )
        return github_page(
            title=f"Connected {_repo_count(count)} to {agent_name}", body_html=ready + body
        )
    if same_person:
        body = (
            "<p>The repos are connected. Choose which agents use them in GitHub setup.</p>"
            '<div class="gh-actions">' + back + "</div>"
            if platform == "discord"
            else "<p>In Slack, run <code>/github</code> to choose which agents use them.</p>"
        )
    else:
        body = (
            f"<p>{html.escape(requester_label)} can now choose which agents use them. "
            "You can close this tab.</p>"
        )
    return github_page(title=f"Connected {_repo_count(count)}.", body_html=body)


def _install_page(install_url: str, cancel_url: str) -> Response:
    return github_page(
        title="Install Daimon on GitHub",
        body_html=(
            "<p>Pick the account or organization with your repos. "
            "You'll choose which repos to connect after GitHub.</p>"
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{install_url}">Continue to GitHub</a>'
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div>'
        ),
    )


def _pending_page(check_url: str, cancel_url: str) -> Response:
    return github_page(
        title="Waiting for GitHub approval",
        body_html=(
            "<p>An owner of this GitHub account must approve Daimon. "
            "GitHub has the request. Check again after they approve.</p>"
            "<p>Cancel closes this check. The approval request stays on GitHub.</p>"
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{check_url}">Check again</a>'
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div>'
        ),
    )


def _no_repos_page(link: str, install_url: str) -> Response:
    copy_arg = json.dumps(link).replace("<", "\\u003c")
    return github_page(
        title="No repos available to connect",
        body_html=(
            "<p>This GitHub account does not manage any repos Daimon can see. "
            "Send the link to someone who manages them, or choose another account.</p>"
            '<div class="gh-actions"><button class="gh-primary" '
            'type="button" id="copy-link">Copy link</button>'
            f'<a class="gh-link" href="{install_url}">Choose another account</a></div>'
            f'<script>document.getElementById("copy-link").addEventListener("click", '
            f"async () => {{ await navigator.clipboard.writeText({copy_arg}); "
            'document.getElementById("copy-link").textContent = "Link copied"; '
            "});</script>"
        ),
    )


def _cancelled_page(back: str = "") -> Response:
    return github_page(
        title="Nothing was connected.",
        body_html="<p>You can close this tab.</p>"
        + (f'<div class="gh-actions">{back}</div>' if back else ""),
    )


def _expired_page(requester_label: str) -> Response:
    return github_page(
        title="This link has expired.",
        body_html=f"<p>Ask {html.escape(requester_label)} for a new one.</p>",
        status=400,
        error=True,
    )


def _receipt_signature(state: str, invitation_hash: str, secret: str) -> str:
    message = f"{state}:{invitation_hash}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


_PICKER_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("github-connect-form");
  const flow = document.querySelector(".gh-flow");
  const boxes = [...form.querySelectorAll('input[name="repo"]')];
  const search = document.getElementById("search-repos");
  search.closest(".gh-search-wrap").hidden = false;
  const changeAccess = document.getElementById("change-access");
  const bulk = document.getElementById("select-all-repos");
  const count = document.getElementById("repo-count");
  const selectedCount = document.getElementById("selected-count");
  const showSelected = document.getElementById("show-selected");
  const status = document.getElementById("selection-status");
  const submit = document.getElementById("connect-repos");
  let selectedOnly = false;
  let connecting = false;
  const plural = n => n === 1 ? "repo" : "repos";
  function render() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    let selected = 0;
    for (const box of boxes) {
      if (box.checked) selected++;
      const row = box.closest(".gh-choice");
      row.hidden = (selectedOnly && !box.checked) ||
        !row.textContent.toLowerCase().includes(query);
      if (!row.hidden) visible++;
    }
    for (const owner of form.querySelectorAll(".gh-repo-owner")) {
      owner.hidden = !owner.querySelector(".gh-choice:not([hidden])");
    }
    const results = !!query || selectedOnly;
    count.textContent = results ? `${visible} results` : `${boxes.length} repos available`;
    selectedCount.textContent = `${selected} selected`;
    showSelected.hidden = selected === 0;
    showSelected.textContent = selectedOnly ? "Show all" : "Show selected";
    changeAccess.hidden = selected === 0;
    const visibleBoxes = boxes.filter(box => !box.closest(".gh-choice").hidden);
    const allVisibleChecked = visibleBoxes.length > 0 && visibleBoxes.every(box => box.checked);
    bulk.textContent = allVisibleChecked ? "Clear selection" :
      results ? `Select ${visible} results` : `Select all ${boxes.length}`;
    bulk.hidden = visible === 0;
    const access = form.querySelector('input[name="access"]:checked').value;
    const accessLabel = access === "write" ? "Read and write" : "Read only";
    status.textContent = selected ? `${selected} selected. ${accessLabel}.` :
      "Select at least one repo";
    submit.disabled = selected === 0;
    submit.textContent = selected ? `Connect ${selected} ${plural(selected)}` : "Connect repos";
    form.querySelector(".gh-empty").hidden = visible !== 0;
  }
  search.addEventListener("input", render);
  changeAccess.addEventListener("click", () => {
    const access = document.getElementById("github-access");
    access.scrollIntoView({behavior: "smooth", block: "center"});
    access.querySelector('input[name="access"]:checked').focus({preventScroll: true});
  });
  showSelected.addEventListener("click", () => { selectedOnly = !selectedOnly; render(); });
  form.addEventListener("change", render);
  bulk.addEventListener("click", () => {
    const visibleBoxes = boxes.filter(box => !box.closest(".gh-choice").hidden);
    const allChecked = visibleBoxes.every(box => box.checked);
    for (const box of visibleBoxes) box.checked = !allChecked;
    render();
  });
  form.addEventListener("submit", event => {
    if (connecting || !boxes.some(box => box.checked)) { event.preventDefault(); return; }
    connecting = true;
    const selected = boxes.filter(box => box.checked).length;
    flow.classList.add("is-connecting");
    status.textContent = `Connecting ${selected} ${plural(selected)}…`;
    submit.disabled = true;
    submit.textContent = "Connecting…";
    form.setAttribute("aria-busy", "true");
  });
  render();
})();
</script>
"""


def _access_choices() -> str:
    return (
        '<section class="gh-side-card gh-access" id="github-access" '
        'aria-label="Access"><h2>Access</h2>'
        '<label><input type="radio" name="access" value="write" checked>'
        "<strong>Read and write</strong>"
        "<small>Push branches, open issues and pull requests.</small></label>"
        '<label><input type="radio" name="access" value="read">'
        "<strong>Read only</strong>"
        "<small>Read code, issues and pull requests.</small></label></section>"
    )


def _confirmation_page(
    *,
    root: str,
    state: str,
    invitation_hash: str,
    secret: str,
    cancel_url: str,
    installations: list[_Installation],
    clients_present: bool,
    platform: str,
    workspace: str,
    agent_name: str | None,
    selection_error: str | None = None,
) -> Response:
    """Render the same picker used by the live route and screenshot capture."""
    destination = f"For {agent_name}" if agent_name else "For your server"
    place = "Server" if platform == "discord" else "Workspace"
    context = (
        '<div class="gh-context">'
        f"<span>{html.escape(destination)}</span>"
        f"<span>{place}: {html.escape(workspace)}</span></div>"
    )
    parts = [
        f'<form id="github-connect-form" method="post" '
        f'action="{html.escape(root, quote=True)}/oauth/github/confirm">',
        f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">',
        '<input type="hidden" name="invitation" '
        f'value="{html.escape(invitation_hash, quote=True)}">',
        '<input type="hidden" name="receipt" '
        f'value="{_receipt_signature(state, invitation_hash, secret)}">',
        context,
    ]
    if selection_error:
        parts.append(f'<p class="gh-warning" role="alert">{html.escape(selection_error)}</p>')
    if clients_present:
        parts.append(
            '<p class="gh-warning">Clients use this server. Pick only the repos they may see.</p>'
        )
    parts.extend(
        [
            '<div class="gh-picker-grid"><section class="gh-results">',
            '<div class="gh-search-wrap" hidden><input class="gh-search" type="search" '
            'id="search-repos" placeholder="Search repos" aria-label="Search repos" '
            'autocomplete="off"></div>',
            '<div class="gh-list-toolbar"><span id="repo-count" aria-live="polite"></span>'
            '<button class="gh-link-button" type="button" '
            'id="select-all-repos" hidden></button></div>',
            '<div class="gh-repo-list" aria-label="Repos you manage">',
        ]
    )
    for installation in installations:
        owned = [repo for repo in installation.repos if repo.admin]
        if not owned:
            continue
        parts.append('<div class="gh-repo-owner">')
        parts.append(f'<div class="gh-repo-group">{html.escape(installation.owner_login)}</div>')
        for repo in owned:
            prefix, _, name = repo.full_name.rpartition("/")
            selected = ""
            parts.append(
                '<label class="gh-choice repo-choice">'
                f'<input type="checkbox" name="repo" value="{repo.id}"{selected}>'
                f'<span><span class="gh-repo-prefix">{html.escape(prefix)}/</span>'
                f'<span class="gh-repo-name">{html.escape(name)}</span></span></label>'
            )
        parts.append("</div>")
    parts.extend(
        [
            '<p class="gh-empty" hidden>No repos match this search.</p></div></section>',
            '<aside class="gh-side">',
            _access_choices(),
            '<section class="gh-side-card"><h2>Selection</h2>'
            '<p class="gh-selection-count" id="selected-count" aria-live="polite">0 selected</p>'
            '<button class="gh-link-button" id="show-selected" type="button" hidden>'
            "Show selected</button></section></aside></div>",
            '<div class="gh-finish"><div class="gh-finish-status">'
            '<p id="selection-status" aria-live="polite">Select at least one repo</p>'
            '<button class="gh-link-button" id="change-access" type="button" hidden>'
            'Change</button></div><div class="gh-finish-actions">'
            '<button class="gh-primary" id="connect-repos" type="submit">'
            "Connect repos</button>"
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div></div>',
            "</form>",
            _PICKER_SCRIPT,
        ]
    )
    return github_page(title="Choose repos", body_html="".join(parts), kind="picker")


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
                repository_selection=parsed.repository_selection,
                repos=tuple(repos),
            )
        )
    return result


async def has_pending_installation_request(
    client: httpx.AsyncClient, *, app_id: str, private_key: str, github_user_id: int
) -> bool:
    app_token = build_app_jwt(private_key, app_id, now=int(time.time()))
    requests = await list_github_pages(
        client, "/app/installation-requests", app_token, "installation_requests"
    )
    return any(
        _InstallationRequestPayload.model_validate(request).requester.id == github_user_id
        for request in requests
    )


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

    async def successful_page(
        state: str, cookie: str, invitation_hash: str = ""
    ) -> Response | None:
        async with sessionmaker() as session:
            invitation = await github_connect.successful_confirmation(
                session, state=state, cookie=cookie, invitation_hash=invitation_hash
            )
            if invitation is None and invitation_hash:
                status, candidate = await github_connect.invitation_status(session, invitation_hash)
                if status == "used" and candidate is not None:
                    invitation = candidate
            if invitation is None:
                return None
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
        back = _back_to_chat(requester.platform, requester.external_id) if requester else ""
        return _already_connected_page(
            invitation.connected_repo_count,
            back,
            agent_name=invitation.agent_name,
            update_pending=invitation.activation_status == "update_pending",
        )

    async def connect(request: Request) -> Response:
        token = request.path_params["token"]
        invitation_hash = github_connect.digest(token)
        async with sessionmaker() as session:
            status, invitation = await github_connect.invitation_status(session, invitation_hash)
            if status != "active":
                if invitation is None:
                    return _error()
                requester = await get_account_with_tenant(
                    session, account_id=invitation.requester_account_id
                )
                back = (
                    _back_to_chat(requester.platform, requester.external_id)
                    if requester is not None
                    else ""
                )
                if status == "used":
                    return _already_connected_page(
                        invitation.connected_repo_count,
                        back,
                        agent_name=invitation.agent_name,
                        update_pending=invitation.activation_status == "update_pending",
                    )
                if status == "requester_left":
                    return _error(
                        "This link was made by someone who's no longer an admin here. "
                        "Ask an admin for a new link."
                    )
                if status == "expired":
                    return _expired_page(invitation.requester_label)
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
                encrypted_invitation_token=encrypt_token(fernet, token),
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
            async with sessionmaker() as session:
                refused_flow = await github_connect.get_flow(session, state=state, cookie=cookie)
                refused_invitation = (
                    await github_connect.get_invitation(session, refused_flow.invitation_hash)
                    if refused_flow is not None
                    else None
                )
                refused_requester = (
                    await get_account_with_tenant(
                        session, account_id=refused_invitation.requester_account_id
                    )
                    if refused_invitation is not None
                    else None
                )
            return _cancelled_page(
                _back_to_chat(refused_requester.platform, refused_requester.external_id)
                if refused_requester is not None
                else ""
            )
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
            retry_token = (
                decrypt_token(fernet, flow.encrypted_invitation_token)
                if flow.encrypted_invitation_token is not None
                else None
            )
            retry_url = f"{root}/oauth/github/connect/{retry_token}" if retry_token else None
            return _error("Couldn't reach GitHub", 502, retry_url)
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
        used = await successful_page(state, cookie, invitation_hash)
        if used is not None:
            return used
        retry_confirm_url = f"{root}/oauth/github/confirm?{urlencode({'state': state})}"
        async with sessionmaker() as session:
            flow = await github_connect.get_flow(session, state=state, cookie=cookie)
            if flow is None or flow.encrypted_user_token is None or flow.github_user_id is None:
                used = await successful_page(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            invitation = await github_connect.get_invitation(session, flow.invitation_hash)
            if invitation is None:
                used = await successful_page(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if requester is None or requester.is_external:
                return _error()
            clients_present = await has_external_accounts(session, tenant_id=invitation.tenant_id)
        if request.query_params.get("cancel") == "1":
            return _cancelled_page(_back_to_chat(requester.platform, requester.external_id))
        token = decrypt_token(fernet, flow.encrypted_user_token)
        try:
            async with factory() as client:
                installations = await _installations(client, token)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            used = await successful_page(state, cookie, invitation_hash)
            if used is not None:
                return used
            return _error("Couldn't reach GitHub", 502, str(request.url))
        if request.method == "POST":
            try:
                selected_ids = [int(value) for value in fields.get("repo", [])]
            except ValueError:
                return _error("Selection could not be verified.", retry_url=retry_confirm_url)
            if not selected_ids:
                return _confirmation_page(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=secret,
                    cancel_url=(
                        f"{root}/oauth/github/confirm?{urlencode({'state': state, 'cancel': '1'})}"
                    ),
                    installations=installations,
                    clients_present=clients_present,
                    platform=requester.platform,
                    workspace=invitation.workspace_label,
                    agent_name=invitation.agent_name,
                    selection_error="Select at least one repo",
                )
            visible = {
                repo.id: repo for install in installations for repo in install.repos if repo.admin
            }
            if len(set(selected_ids)) != len(selected_ids):
                return _error("Selection could not be verified.", retry_url=retry_confirm_url)
            if any(repo_id not in visible for repo_id in selected_ids):
                return _error("Selection could not be verified.", 403, retry_confirm_url)
            repos: list[github_connect.RepoConfirmation] = []
            for repo_id in selected_ids:
                repo = visible[repo_id]
                access = fields.get("access", ["write"])[0]
                if access not in ("read", "write"):
                    return _error("Selection could not be verified.", retry_url=retry_confirm_url)
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
                return _error("Couldn't reach GitHub", 502, retry_confirm_url)
            if any(
                detail.account_id != by_installation[detail.installation_id].owner_id
                or detail.account_login.casefold()
                != by_installation[detail.installation_id].owner_login.casefold()
                or detail.suspended_at is not None
                for detail in details
            ):
                return _error("GitHub access could not be verified.", 403, retry_confirm_url)
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
                        await github_connect.activate_confirmed_agent(
                            session, invitation=invitation, repos=repos
                        )
                        await finish_confirmed_requests(
                            session,
                            tenant_id=invitation.tenant_id,
                            approved_by_account_id=invitation.requester_account_id,
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
                used = await successful_page(state, cookie, invitation_hash)
                if used is not None:
                    return used
                return _error()
            async with sessionmaker() as session:
                requester_github_id = await github_connect.requester_linked_github_user_id(
                    session, account_id=invitation.requester_account_id
                )
            await revoke_user_token(token)
            async with sessionmaker() as session:
                confirmed = await github_connect.successful_confirmation(
                    session, state=state, cookie=cookie
                )
            response = _done_page(
                count=len(repos),
                platform=requester.platform,
                external_id=requester.external_id,
                requester_label=invitation.requester_label,
                same_person=requester_github_id == flow.github_user_id,
                agent_name=invitation.agent_name,
                update_pending=confirmed is not None
                and confirmed.activation_status == "update_pending",
            )
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
        admin_repos = [
            repo for installation in installations for repo in installation.repos if repo.admin
        ]
        install_url = (
            f"https://github.com/apps/{html.escape(config.app_slug or '', quote=True)}"
            f"/installations/new?{urlencode({'state': state})}"
        )
        cancel_url = (
            f"{html.escape(root, quote=True)}/oauth/github/confirm?"
            f"{urlencode({'state': state, 'cancel': '1'})}"
        )
        pending = False
        if not admin_repos and config.app_id is not None and config.private_key is not None:
            try:
                async with factory() as client:
                    pending = await has_pending_installation_request(
                        client,
                        app_id=config.app_id,
                        private_key=config.private_key.get_secret_value(),
                        github_user_id=flow.github_user_id,
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return _error("Couldn't reach GitHub", 502, retry_confirm_url)
        if pending:
            return _pending_page(html.escape(retry_confirm_url, quote=True), cancel_url)
        if not installations:
            return _install_page(install_url, cancel_url)
        if not admin_repos:
            link = (
                f"{root}/oauth/github/connect/"
                f"{decrypt_token(fernet, flow.encrypted_invitation_token)}"
                if flow.encrypted_invitation_token is not None
                else ""
            )
            return _no_repos_page(link, install_url)
        return _confirmation_page(
            root=root,
            state=state,
            invitation_hash=flow.invitation_hash,
            secret=secret,
            cancel_url=cancel_url,
            installations=installations,
            clients_present=clients_present,
            platform=requester.platform,
            workspace=workspace
            or ("this server" if requester.platform == "discord" else "this workspace"),
            agent_name=invitation.agent_name,
        )

    return connect, callback, setup, confirm
