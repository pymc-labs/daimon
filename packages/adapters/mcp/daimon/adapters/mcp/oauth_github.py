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
from anthropic import APIError, AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.github_pages import github_page
from daimon.adapters.mcp.web_icons import icon, platform_mark
from daimon.core.channel_admins import GroupMembers
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_app_auth import build_app_jwt, get_app_installation_details
from daimon.core.github_connect_cards import (
    ADD_REPOS_LABEL,
    ALREADY_ADDED,
    CLOSE_TAB,
    added_line,
    audience_line,
    old_token_line,
    picker_title,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_panel import requester_manages_agent
from daimon.core.github_requester_access import list_github_pages
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant, has_external_accounts
from daimon.core.stores.github_access import list_agent_repos
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
    owner_type: str = ""


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


class _AccountLoginPayload(BaseModel):
    login: str = ""


class _InstallationRequestPayload(BaseModel):
    requester: _UserPayload
    account: _AccountLoginPayload | None = None


@dataclass(frozen=True)
class _PendingInstallationRequest:
    found: bool
    account_login: str | None = None


def _error(
    message: str = "This link has expired.",
    status: int = 400,
    retry_url: str | None = None,
) -> Response:
    retry = (
        '<div class="gh-actions"><a class="gh-primary" '
        f'href="{html.escape(retry_url, quote=True)}">Try again{icon("chevron-right")}</a></div>'
        if retry_url
        else ""
    )
    return github_page(
        title=message,
        body_html=f'<div class="gh-status-icon">{icon("triangle-alert")}</div>' + retry,
        status=status,
        error=True,
    )


def _back_to_chat(platform: str, workspace_id: str) -> str:
    if platform == "discord":
        target = f"https://discord.com/channels/{html.escape(workspace_id, quote=True)}"
        return f'<a class="gh-primary" href="{target}">{icon("discord")}Back to Discord</a>'
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
        added_line(count, agent_name)
        if agent_name and count
        else (
            f"Already connected: {_repo_count(count)}."
            if count is not None
            else "Already connected."
        )
    )
    detail = (
        f"<p>An operator will finish switching {html.escape(agent_name)}.</p>"
        if agent_name and update_pending
        else (
            "" if agent_name else "<p>The repos are connected. Choose an agent in GitHub setup.</p>"
        )
    )
    return github_page(
        title=label,
        heading_icon=True,
        body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>'
        + detail
        + "<p>You can close this tab.</p>"
        + (f'<div class="gh-actions">{back}</div>' if back else ""),
    )


def _missing_phrase(missing: tuple[tuple[str, bool], ...]) -> str:
    """`a/b with read and write and c/d`: each repo the switch still needs."""
    names = [f"{name} with read and write" if write else name for name, write in missing]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _done_page(
    *,
    count: int,
    platform: str,
    external_id: str,
    requester_label: str,
    same_person: bool,
    agent_name: str | None,
    update_pending: bool,
    retired_saved_key: bool = False,
    missing_repos: tuple[tuple[str, bool], ...] = (),
) -> Response:
    back = _back_to_chat(platform, external_id)
    if update_pending and agent_name and not missing_repos:
        return github_page(
            title=f"Repos connected. An operator will finish switching {agent_name}.",
            heading_icon=True,
            body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div><p>{CLOSE_TAB}</p>',
        )
    if agent_name:
        # The old-token line only once the switch has finished, never while pending.
        token = (
            f"<p>{html.escape(agent_name)} still uses its old GitHub token. "
            f"Add {html.escape(_missing_phrase(missing_repos))} to finish switching.</p>"
            if update_pending
            else (f"<p>{html.escape(old_token_line(agent_name))}</p>" if retired_saved_key else "")
        )
        actions = (
            '<div class="gh-actions">' + back + "</div>"
            if same_person and platform == "discord"
            else ""
        )
        return github_page(
            title=added_line(count, agent_name),
            heading_icon=True,
            body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>'
            + token
            + f"<p>{CLOSE_TAB}</p>"
            + actions,
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
    return github_page(
        title=f"Connected {_repo_count(count)}",
        heading_icon=True,
        body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>' + body,
    )


def _install_page(install_url: str, cancel_url: str) -> Response:
    return github_page(
        title="Install Daimon on GitHub",
        heading_icon=True,
        body_html=(
            "<p>Pick the account or organization with your repos. "
            "You'll choose which repos to connect after GitHub.</p>"
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{install_url}">'
            f"{icon('github')}Continue to GitHub</a>"
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div>'
        ),
    )


def _pending_page(check_url: str, cancel_url: str, organization: str | None = None) -> Response:
    who = (
        f"An owner of {html.escape(organization)} must approve Daimon."
        if organization
        else "A GitHub owner must approve Daimon."
    )
    return github_page(
        title="Waiting for GitHub approval",
        heading_icon=True,
        body_html=(
            f'<div class="gh-status-icon">{icon("hourglass")}</div>'
            f"<p>{who} Check again after they approve.</p>"
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{check_url}">Check again{icon("chevron-right")}</a>'
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
        body_html=f'<div class="gh-status-icon">{icon("clock")}</div>'
        f"<p>Ask {html.escape(requester_label)} for a new one.</p>",
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
  const all = [...form.querySelectorAll('input[name="repo"]')];
  const boxes = all.filter(box => !box.disabled);
  const verb = form.dataset.verb;
  const audience = document.getElementById("audience");
  const search = document.getElementById("search-repos");
  search.closest(".gh-search-wrap").hidden = false;
  const changeAccess = document.getElementById("change-access");
  const bulk = document.getElementById("select-all-repos");
  const count = document.getElementById("repo-count");
  const showSelected = document.getElementById("show-selected");
  const status = document.getElementById("selection-status");
  const submit = document.getElementById("connect-repos");
  const submitLabel = submit.querySelector(".gh-button-label");
  let selectedOnly = false;
  let connecting = false;
  const plural = n => n === 1 ? "repo" : "repos";
  function render() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    let selected = 0;
    for (const box of all) {
      if (box.checked && !box.disabled) selected++;
      const row = box.closest(".gh-choice");
      row.hidden = (selectedOnly && !box.checked) ||
        !row.textContent.toLowerCase().includes(query);
      if (!row.hidden) visible++;
    }
    for (const owner of form.querySelectorAll(".gh-repo-owner")) {
      owner.hidden = !owner.querySelector(".gh-choice:not([hidden])");
    }
    const results = !!query || selectedOnly;
    count.textContent = query ? `${visible} matching ${plural(visible)}` :
      selectedOnly ? `${visible} selected ${plural(visible)}` : `${all.length} repos available`;
    showSelected.hidden = selected === 0;
    showSelected.textContent = selectedOnly ? "Show all" : "Show selected";
    changeAccess.hidden = selected === 0;
    const visibleBoxes = boxes.filter(box => !box.closest(".gh-choice").hidden);
    const allVisibleChecked = visibleBoxes.length > 0 && visibleBoxes.every(box => box.checked);
    bulk.textContent = allVisibleChecked ? "Clear selection" : `Select all ${visibleBoxes.length}`;
    bulk.hidden = visibleBoxes.length === 0;
    const access = form.querySelector('input[name="access"]:checked').value;
    const accessLabel = access === "write" ? "Read and write" : "Read only";
    status.textContent = selected ? `${selected} selected. ${accessLabel}.` : "0 selected";
    submit.disabled = selected === 0;
    submitLabel.textContent = selected ?
      `${verb} ${selected} ${plural(selected)}` : `${verb} repos`;
    if (audience) {
      const can = access === "write" ? "read and change" : "read";
      audience.textContent =
        `Anyone who talks to ${audience.dataset.agent} can ask it to ${can} them.`;
    }
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
    for (const box of boxes.filter(box => box.checked)) {
      const input = document.createElement("input");
      input.type = "hidden"; input.name = "repo"; input.value = box.value;
      form.appendChild(input);
    }
    const selectedAccess = form.querySelector('input[name="access"]:checked');
    const accessInput = document.createElement("input");
    accessInput.type = "hidden"; accessInput.name = "access";
    accessInput.value = selectedAccess.value; form.appendChild(accessInput);
    form.querySelectorAll('input[type="checkbox"], input[type="radio"]').forEach(input => {
      input.disabled = true;
    });
    search.disabled = true;
    bulk.disabled = true;
    showSelected.disabled = true;
    changeAccess.disabled = true;
    flow.classList.add("is-connecting");
    const doing = verb === "Add" ? "Adding" : "Connecting";
    status.textContent = `${doing} ${selected} ${plural(selected)}…`;
    submit.disabled = true;
    submit.innerHTML = `<span class="gh-spinner" aria-hidden="true"></span>${doing}…`;
    form.setAttribute("aria-busy", "true");
  });
  render();
})();
</script>
"""


def _access_choices(*, agent: bool) -> str:
    applies = '<p class="gh-access-note">For the repos you add now.</p>' if agent else ""
    return (
        '<section class="gh-side-card gh-access" id="github-access" '
        'aria-label="Access"><h2>Access</h2>'
        '<label><input type="radio" name="access" value="read" checked>'
        f'<span class="gh-access-copy"><strong>{icon("eye")}Read only</strong>'
        "<small>Read code, issues and pull requests.</small></span></label>"
        '<label><input type="radio" name="access" value="write">'
        f'<span class="gh-access-copy"><strong>{icon("pencil")}Read and write</strong>'
        "<small>Push branches, open issues and pull requests.</small></span></label>"
        f"{applies}</section>"
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
    already_added: frozenset[int] = frozenset(),
) -> Response:
    """Render the same picker used by the live route and screenshot capture.

    Repos in `already_added` show ticked and greyed; only new ticks are sent.
    """
    place = "Server" if platform == "discord" else "Workspace"
    audience = (
        f'<p class="gh-audience" id="audience" data-agent="{html.escape(agent_name, quote=True)}">'
        f"{html.escape(audience_line(agent_name, write=False))}</p>"
        if agent_name
        else ""
    )
    context = (
        audience + f'<div class="gh-context">{platform_mark(platform)}'
        f"<span>{place}: {html.escape(workspace)}</span></div>"
    )
    parts = [
        f'<form id="github-connect-form" method="post" '
        f'data-verb="{"Add" if agent_name else "Connect"}" '
        f'action="{html.escape(root, quote=True)}/oauth/github/confirm">',
        f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">',
        '<input type="hidden" name="invitation" '
        f'value="{html.escape(invitation_hash, quote=True)}">',
        '<input type="hidden" name="receipt" '
        f'value="{_receipt_signature(state, invitation_hash, secret)}">',
        context,
    ]
    parts.extend(
        [
            '<div class="gh-picker-grid"><section class="gh-results">',
            f'<div class="gh-search-wrap" hidden>{icon("search")}'
            '<input class="gh-search" type="search" '
            'id="search-repos" placeholder="Search repos" aria-label="Search repos" '
            'autocomplete="off"></div>',
            '<div class="gh-list-toolbar"><span id="repo-count" aria-live="polite"></span>'
            '<button class="gh-link-button" id="show-selected" type="button" hidden>'
            "Show selected</button>"
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
        owner_label = (
            "Personal account" if installation.owner_type.lower() == "user" else "Organization"
        )
        parts.append(
            f'<div class="gh-repo-group">{icon("github")}'
            f'<span class="web-sr-only">{owner_label}</span>'
            f"{html.escape(installation.owner_login)}</div>"
        )
        for repo in owned:
            prefix, _, name = repo.full_name.rpartition("/")
            added = repo.id in already_added
            parts.append(
                f'<label class="gh-choice repo-choice{" is-added" if added else ""}">'
                f'<input type="checkbox" name="repo" value="{repo.id}"'
                + (" checked disabled" if added else "")
                + ">"
                f'<span><span class="gh-repo-prefix">{html.escape(prefix)}/</span>'
                f'<span class="gh-repo-name">{html.escape(name)}</span>'
                + (f'<span class="gh-added">{ALREADY_ADDED}</span>' if added else "")
                + "</span></label>"
            )
        parts.append("</div>")
    parts.extend(
        [
            '<p class="gh-empty" hidden>No repos match this search.</p></div></section>',
            '<aside class="gh-side">',
            _access_choices(agent=agent_name is not None),
            "</aside></div>",
            '<div class="gh-finish"><div class="gh-finish-status">'
            '<p id="selection-status" aria-live="polite">0 selected</p>'
            '<button class="gh-link-button" id="change-access" type="button" hidden>'
            "Change</button>"
            + (
                f'<p class="gh-client-note">{icon("triangle-alert")}'
                + (
                    f"Clients can see what {html.escape(agent_name)} shares."
                    if agent_name
                    else "Clients can see what connected agents share."
                )
                + "</p>"
                if clients_present
                else ""
            )
            + (
                f'<p class="gh-inline-error" role="alert">{html.escape(selection_error)}</p>'
                if selection_error
                else ""
            )
            + '</div><div class="gh-finish-actions">',
            '<button class="gh-primary" id="connect-repos" type="submit">'
            f'{icon("github")}<span class="gh-button-label">'
            f"{ADD_REPOS_LABEL if agent_name else 'Connect repos'}</span></button>"
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div></div>',
            "</form>",
            _PICKER_SCRIPT,
        ]
    )
    return github_page(
        title=picker_title(agent_name) if agent_name else "Choose repos for your server",
        body_html="".join(parts),
        kind="picker",
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
                repository_selection=parsed.repository_selection,
                repos=tuple(repos),
                owner_type=parsed.account.type,
            )
        )
    return result


async def has_pending_installation_request(
    client: httpx.AsyncClient, *, app_id: str, private_key: str, github_user_id: int
) -> _PendingInstallationRequest:
    app_token = build_app_jwt(private_key, app_id, now=int(time.time()))
    requests = await list_github_pages(
        client, "/app/installation-requests", app_token, "installation_requests"
    )
    matching = [
        parsed
        for request in requests
        if (parsed := _InstallationRequestPayload.model_validate(request)).requester.id
        == github_user_id
    ]
    if len(matching) == 1 and matching[0].account and matching[0].account.login:
        return _PendingInstallationRequest(True, matching[0].account.login)
    return _PendingInstallationRequest(bool(matching))


def build_oauth_github_routes(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    client_factory: ClientFactory | None = None,
    deployment_default: DeploymentDefault | None = None,
    anthropic: AsyncAnthropic | None = None,
    group_members: Callable[[str, str], GroupMembers | None] | None = None,
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

    async def revoke_user_token(token: str) -> bool:
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
            return False
        return True

    async def live_is_daimon_managed(invitation: github_connect.Invitation) -> bool | None:
        """Whether the link's agent is a Daimon default agent now; None if it can't be read."""
        if anthropic is None or invitation.agent_id is None or not invitation.agent_ma_id:
            return None
        try:
            live = await find_agent_by_derived_uuid(
                anthropic, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
            )
        except APIError:
            return None
        if live is None:
            return None
        return live.metadata.get(MA_METADATA_KEY_MANAGED) == "true"

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
        if request.method == "GET" and request.query_params.get("cancel") == "1":
            async with sessionmaker.begin() as session:
                cancelled = await github_connect.cancel_flow(session, state=state, cookie=cookie)
            if cancelled is None:
                used = await successful_page(state, cookie)
                return used if used is not None else _cancelled_page()
            async with sessionmaker() as session:
                invitation = await github_connect.get_invitation(session, cancelled.invitation_hash)
                requester = (
                    await get_account_with_tenant(
                        session, account_id=invitation.requester_account_id
                    )
                    if invitation is not None
                    else None
                )
            if cancelled.encrypted_user_token is not None:
                token = decrypt_token(fernet, cancelled.encrypted_user_token)
                if await revoke_user_token(token):
                    async with sessionmaker.begin() as session:
                        await github_connect.finish_cancel_revocation(
                            session, state=state, cookie=cookie
                        )
            return _cancelled_page(
                _back_to_chat(requester.platform, requester.external_id)
                if requester is not None
                else ""
            )
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
            already_added = (
                frozenset(
                    repo.repo_id
                    for repo in await list_agent_repos(
                        session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                    )
                )
                if invitation.agent_id is not None
                else frozenset[int]()
            )
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
            # Repos already on the agent keep their access; only new ticks count.
            selected_ids = [repo_id for repo_id in selected_ids if repo_id not in already_added]
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
                    already_added=already_added,
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
                access = fields.get("access", ["read"])[0]
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
            is_daimon_managed = await live_is_daimon_managed(invitation)
            members = (
                group_members(requester.platform, requester.external_id)
                if group_members is not None
                else None
            )
            activation: github_connect.ConfirmedActivation | None = None
            try:
                async with sessionmaker.begin() as session:
                    saved = await github_connect.confirm(
                        session,
                        state=state,
                        cookie=cookie,
                        github_user_id=flow.github_user_id,
                        repos=repos,
                        requester_manages_agent=deployment_default is not None
                        and await requester_manages_agent(
                            session,
                            tenant_id=invitation.tenant_id,
                            account_id=invitation.requester_account_id,
                            platform=requester.platform,
                            platform_user_id=requester.platform_user_id,
                            agent_name=invitation.agent_name,
                            ma_agent_id=invitation.agent_ma_id,
                            default=deployment_default,
                            is_daimon_managed=is_daimon_managed,
                            members=members,
                        ),
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
                        activation = await github_connect.activate_confirmed_agent(
                            session, invitation=invitation, repos=repos
                        )
                        resumed_requests = await finish_confirmed_requests(
                            session,
                            tenant_id=invitation.tenant_id,
                            approved_by_account_id=invitation.requester_account_id,
                        )
                        if not resumed_requests:
                            await github_connect.queue_connect_followup(
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
                retired_saved_key=activation is not None and activation.retired_saved_key,
                missing_repos=tuple(
                    (missing.full_name, missing.needs_write)
                    for missing in (activation.missing_repos if activation else ())
                ),
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
        pending = _PendingInstallationRequest(False)
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
        if pending.found:
            return _pending_page(
                html.escape(retry_confirm_url, quote=True),
                cancel_url,
                pending.account_login,
            )
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
            already_added=already_added,
        )

    return connect, callback, setup, confirm
