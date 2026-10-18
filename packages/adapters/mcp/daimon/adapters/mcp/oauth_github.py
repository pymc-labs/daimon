"""Browser-bound GitHub App repository connection flow."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode

import httpx
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.adapters.mcp.github_pages import github_page
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.web_icons import icon, platform_mark
from daimon.core.agent_pins import agent_pin_names
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_app_auth import (
    build_app_jwt,
    get_app_installation_details,
    installation_supports_write,
)
from daimon.core.github_connect_cards import (
    NEEDED,
    NEEDS_WRITE,
    audience_line,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_panel import requester_manages_agent
from daimon.core.github_requester_access import list_github_pages
from daimon.core.native_admins import AdminAttestationUnavailable, attest_discord_admin
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, Role
from daimon.core.stores.github_access import list_agent_repos
from daimon.core.stores.github_installation_reconciliation import enqueue as enqueue_reconciliation
from daimon.core.stores.github_request_actions import finish_confirmed_requests
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.teams_graph import GraphClient, GraphUnavailable
from mux.drivers import isolated_ssl_context
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

RouteHandler = Callable[[Request], Awaitable[Response]]
ClientFactory = Callable[[], httpx.AsyncClient]
_COOKIE = "daimon_gh_connect"
_log = logging.getLogger(__name__)
_ADMIN_RETRY = "Couldn't confirm you're still a server or workspace admin. Try again in a minute."
# A save posts about 40 bytes per ticked repo (`repo=<id>` plus `access_<id>=write`),
# so 256 KiB covers several thousand repos while bounding what one request can
# make the server read and parse. The cap is checked before any other work.
_MAX_CONFIRM_BODY_BYTES = 256 * 1024
# Handed to `parse_qs` so a body of nothing but `&` cannot allocate a huge field list.
_MAX_CONFIRM_FIELDS = 10_000
# One wall-clock budget for listing every installation and its repos. Each GitHub
# call already times out at 20 s, but they run one after another, so many
# installations or pages could hold the request open indefinitely. 60 s leaves
# room for large organizations and stays well inside Cloud Run's request timeout
# (the mcp service sets none, so the platform default of 300 s applies; see the
# `timeoutSeconds` fallback in .github/workflows/deploy.yml).
_INSTALLATIONS_DEADLINE_SECONDS = 60.0
# Form fields a failed save re-posts from its error page. Anything else a client
# sent is dropped rather than echoed back into HTML.
_RETRY_FIELD_NAMES = frozenset(
    {
        "state",
        "invitation",
        "receipt",
        "snapshot",
        "snapshot_sig",
        "repo",
        "remove",
        "access",
        "working",
    }
)
_RETRY_FIELD_SCOPED_ACCESS = re.compile(r"access_\d+")


async def _attest_native_admin(
    *,
    requester: AccountIdentityRow,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Read native authority for the invitation's bound principal, outside a write transaction."""
    user_id = requester.platform_user_id
    if user_id is None:
        raise AdminAttestationUnavailable("platform principal unavailable")
    if requester.platform == "discord":
        return await attest_discord_admin(
            guild_id=requester.external_id,
            platform_user_id=user_id,
            settings=settings,
            http_client=http_client,
        )
    if requester.platform == "slack":
        try:
            async with sessionmaker() as session:
                row = await get_slack_bot_token(session, team_id=requester.external_id)
        except SQLAlchemyError as exc:
            raise AdminAttestationUnavailable("workspace bot token lookup failed") from exc
        if row is None:
            raise AdminAttestationUnavailable("workspace bot token unavailable")
        try:
            token = decrypt_token(fernet, row.encrypted_token)
        except InvalidToken as exc:
            raise AdminAttestationUnavailable("workspace bot token unavailable") from exc

        async def slack_lookup(client: httpx.AsyncClient) -> object:
            response = await client.get(
                "https://slack.com/api/users.info",
                params={"user": user_id},
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=False,
                timeout=5,
            )
            response.raise_for_status()
            return cast(object, response.json())

        try:
            async with asyncio.timeout(10):
                if http_client is None:
                    async with httpx.AsyncClient(
                        trust_env=False, verify=isolated_ssl_context()
                    ) as client:
                        payload = await slack_lookup(client)
                else:
                    payload = await slack_lookup(http_client)
            if (
                not isinstance(payload, dict)
                or cast(dict[str, object], payload).get("ok") is not True
            ):
                raise ValueError("Slack lookup did not succeed")
            user = cast(dict[str, object], payload).get("user")
            if not isinstance(user, dict) or cast(dict[str, object], user).get("id") != user_id:
                raise ValueError("Unbound Slack user")
            user = cast(dict[str, object], user)
            if user.get("deleted"):
                return False
            flags = ("is_admin", "is_owner", "is_primary_owner")
            if any(type(user.get(flag)) is not bool for flag in flags):
                raise ValueError("Missing Slack admin facts")
            return any(user[flag] for flag in flags)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise AdminAttestationUnavailable("Slack lookup failed") from exc
    if requester.platform == "teams":
        teams = settings.teams
        if teams is None or teams.admin_team_id is None:
            raise AdminAttestationUnavailable("Teams admin team unavailable")
        if requester.external_id.lower() != teams.tenant_id.lower():
            return False
        admin_team_id = teams.admin_team_id

        async def teams_lookup(client: httpx.AsyncClient) -> frozenset[str]:
            bot = TeamsBotClient(
                client,
                client_id=teams.client_id,
                client_secret=teams.client_secret.get_secret_value(),
                tenant_id=teams.tenant_id,
            )
            return await GraphClient(client, bot.graph_token).list_team_owner_ids(admin_team_id)

        try:
            async with asyncio.timeout(15):
                if http_client is None:
                    async with httpx.AsyncClient(
                        trust_env=False, verify=isolated_ssl_context()
                    ) as client:
                        owners = await teams_lookup(client)
                else:
                    owners = await teams_lookup(http_client)
            return user_id.lower() in owners
        except (GraphUnavailable, httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise AdminAttestationUnavailable("Teams lookup failed") from exc
    raise AdminAttestationUnavailable("unsupported platform")


@dataclass(frozen=True)
class _Repo:
    id: int
    owner_id: int
    installation_id: int
    full_name: str
    # The signed-in person's GitHub permission on the repo; None when GitHub
    # reports none the flow can use.
    permission: github_connect.GitHubPermission | None

    @property
    def usable(self) -> bool:
        return self.permission is not None

    @property
    def max_access(self) -> Literal["read", "write"]:
        """The App grants write on any usable installed repository."""
        return "write" if self.usable else "read"


def _repo_confirmation(
    repo: _Repo,
    *,
    requested: Literal["read", "write"],
    needs_write: bool,
    installation_writes: Mapping[int, bool],
) -> github_connect.RepoConfirmation:
    """Confirm write access from the App installation for a usable repo."""
    if not repo.usable or not installation_writes.get(repo.installation_id, False):
        raise ValueError("The GitHub App needs write access to connect this repo.")
    return github_connect.RepoConfirmation(
        repo_id=repo.id,
        owner_id=repo.owner_id,
        installation_id=repo.installation_id,
        full_name=repo.full_name,
        max_access="write",
        authorizer_permission=repo.permission,
    )


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


def _retry_hidden_inputs(fields: Mapping[str, Sequence[str]]) -> str:
    """Hidden inputs that re-post the editor's own fields, repeating multi-value ones."""
    return "".join(
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
        for name, values in fields.items()
        if name in _RETRY_FIELD_NAMES or _RETRY_FIELD_SCOPED_ACCESS.fullmatch(name)
        for value in values
    )


def _error(
    message: str = "This link has expired.",
    status: int = 400,
    retry_url: str | None = None,
    *,
    retry_fields: Mapping[str, Sequence[str]] | None = None,
    action: str | None = None,
) -> Response:
    """Render a branded error page.

    `retry_url` adds a Try again link. With `retry_fields` it becomes a form that
    POSTs those fields back to `retry_url`, so a failed save does not drop what
    the person ticked. `action` replaces the default follow-up sentence.
    """
    if retry_url and retry_fields is not None:
        retry = (
            f'<form class="gh-actions" method="post" action="{html.escape(retry_url, quote=True)}">'
            f"{_retry_hidden_inputs(retry_fields)}"
            f'<button class="gh-primary" type="submit">Try again{icon("chevron-right")}</button>'
            "</form>"
        )
    elif retry_url:
        retry = (
            '<div class="gh-actions"><a class="gh-primary" '
            f'href="{html.escape(retry_url, quote=True)}">'
            f"Try again{icon('chevron-right')}</a></div>"
        )
    else:
        retry = ""
    if action is None:
        action = (
            "Try again. If it keeps failing, ask for a new setup link."
            if retry_url
            else "Ask the requester for a new setup link."
        )
    return github_page(
        title=message,
        body_html=(
            f'<div class="gh-status-icon">{icon("triangle-alert")}</div>'
            f'<p class="gh-status-line">{html.escape(message)}</p>'
            f'<p class="gh-action-line">{html.escape(action)}</p>' + retry
        ),
        status=status,
        error=True,
    )


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """Read the request body, or return None as soon as it exceeds `limit` bytes.

    The declared Content-Length is checked first so an honest oversize request
    costs nothing. The streamed total is checked too, because a chunked body has
    no Content-Length and a declared length can be wrong.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _selection_too_large() -> Response:
    return _error(
        "That selection is too large to save.",
        413,
        action="Go back, choose fewer repos, and save again.",
    )


def _back_to_chat(platform: str, workspace_id: str) -> str:
    """How to get back to chat; empty (so "You can close this tab.") for Teams."""
    if platform == "discord":
        target = f"https://discord.com/channels/{html.escape(workspace_id, quote=True)}"
        return f'<a class="gh-primary" href="{target}">{icon("discord")}Back to Discord</a>'
    if platform == "teams":
        return ""
    return "In Slack, run <code>/github</code>."


def _place_label(platform: str) -> str:
    return {"discord": "Server", "teams": "Organisation"}.get(platform, "Workspace")


def _repo_count(count: int) -> str:
    return f"{count} {'repo' if count == 1 else 'repos'}"


def _already_connected_page(
    count: int | None,
    back: str = "",
    *,
    agent_name: str | None = None,
) -> Response:
    status = (
        f"{_repo_count(count)} connected to {html.escape(agent_name)}."
        if agent_name and count is not None
        else "GitHub repos are connected."
    )
    action = (
        "Return to Discord to use the agent."
        if "discord.com/channels/" in back and agent_name
        else "In Slack, run /github to use the agent."
        if back and agent_name
        else "You can close this tab."
    )
    return github_page(
        title="Already connected",
        heading_icon=True,
        body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>'
        + f'<p class="gh-status-line">{status}</p><p class="gh-action-line">{action}</p>'
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
) -> Response:
    back = _back_to_chat(platform, external_id)
    if agent_name:
        actions = (
            '<div class="gh-actions">' + back + "</div>"
            if same_person and platform == "discord"
            else ""
        )
        return github_page(
            title="Repos connected",
            heading_icon=True,
            body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>'
            + f'<p class="gh-status-line">{_repo_count(count)} connected to '
            + f"{html.escape(agent_name)}.</p>"
            + (
                '<p class="gh-action-line">Return to Discord to use the agent.</p>'
                if actions
                else '<p class="gh-action-line">You can close this tab.</p>'
            )
            + actions,
        )
    if same_person:
        body = (
            "<p>The repos are connected. Choose which agents use them in GitHub setup.</p>"
            '<div class="gh-actions">' + back + "</div>"
            if platform == "discord"
            else "<p>The repos are connected. In Teams, ask me which agents should use them.</p>"
            if platform == "teams"
            else "<p>In Slack, run <code>/github</code> to choose which agents use them.</p>"
        )
    else:
        body = (
            f"<p>{html.escape(requester_label)} can now choose which agents use them. "
            "You can close this tab.</p>"
        )
    return github_page(
        title="Repos connected",
        heading_icon=True,
        body_html=f'<div class="gh-status-icon">{icon("circle-check")}</div>'
        f'<p class="gh-status-line">{_repo_count(count)} connected.</p>' + body,
    )


def _install_page(install_url: str, cancel_url: str) -> Response:
    return github_page(
        title="Connect GitHub",
        heading_icon=True,
        body_html=(
            '<p class="gh-status-line">Daimon needs the GitHub App to see your repos.</p>'
            '<p class="gh-action-line">Choose the GitHub organization and repos you want '
            "to connect.</p>"
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{install_url}">'
            f"{icon('github')}Continue to GitHub</a>"
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div>'
        ),
    )


def _pending_page(check_url: str, cancel_url: str, organization: str | None = None) -> Response:
    who = (
        f"{'An' if organization[0].lower() in 'aeiou' else 'A'} "
        f"{html.escape(organization)} owner must approve Daimon."
        if organization
        else "A GitHub owner must approve Daimon."
    )
    return github_page(
        title="Waiting for GitHub approval",
        heading_icon=True,
        body_html=(
            f'<div class="gh-status-icon">{icon("hourglass")}</div>'
            f'<p class="gh-status-line">{who}</p>'
            '<p class="gh-action-line">Check again after approval.</p>'
            '<div class="gh-actions">'
            f'<a class="gh-primary" href="{check_url}">Check again{icon("chevron-right")}</a>'
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div>'
        ),
    )


def _no_repos_page(link: str, install_url: str) -> Response:
    copy_arg = json.dumps(link).replace("<", "\\u003c")
    copy_button = (
        '<button class="gh-primary" type="button" id="copy-link">Copy setup link</button>'
        if link
        else ""
    )
    copy_script = (
        f'<script>document.getElementById("copy-link").addEventListener("click", '
        f"async () => {{ await navigator.clipboard.writeText({copy_arg}); "
        'document.getElementById("copy-link").textContent = "Link copied"; '
        "});</script>"
        if link
        else ""
    )
    return github_page(
        title="No repos available to connect",
        body_html=(
            '<p class="gh-status-line">No repos are available with this account.</p>'
            + (
                '<p class="gh-action-line">Send this link to someone with write access.</p>'
                if link
                else '<p class="gh-action-line">Open GitHub setup to choose repos.</p>'
            )
            + '<div class="gh-actions">'
            + copy_button
            + f'<a class="gh-link" href="{html.escape(install_url, quote=True)}">'
            "Open GitHub setup</a></div>" + copy_script
        ),
    )


def _cancelled_page(back: str = "") -> Response:
    return github_page(
        title="No changes saved.",
        body_html='<p class="gh-status-line">Your GitHub repos were not changed.</p>'
        + (
            '<p class="gh-action-line">Return to Discord when you are ready.</p>'
            if "discord.com/channels/" in back
            else '<p class="gh-action-line">In Slack, run /github when you are ready.</p>'
            if back
            else '<p class="gh-action-line">You can close this tab.</p>'
        )
        + (f'<div class="gh-actions">{back}</div>' if back else ""),
    )


def _expired_page(requester_label: str) -> Response:
    return github_page(
        title="This link has expired.",
        body_html=f'<div class="gh-status-icon">{icon("clock")}</div>'
        '<p class="gh-status-line">This setup link no longer works.</p>'
        f'<p class="gh-action-line">Ask {html.escape(requester_label)} for a new link.</p>',
        status=400,
        error=True,
    )


def _receipt_signature(state: str, invitation_hash: str, secret: str) -> str:
    message = f"{state}:{invitation_hash}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _snapshot_signature(state: str, invitation_hash: str, snapshot: str, secret: str) -> str:
    message = json.dumps([state, invitation_hash, snapshot], separators=(",", ":")).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _saved_agent_page(name: str, grants: list[github_access.AgentRepo]) -> Response:
    body = (
        '<div class="gh-status-icon">'
        + icon("circle-check")
        + "</div>"
        + f'<p class="gh-status-line">{html.escape(name)} can use '
        + f"{_repo_count(len(grants))}.</p>"
        + '<p class="gh-action-line">You can close this tab.</p>'
    )
    return github_page(title="Repos saved", body_html=body, heading_icon=True)


_EDITOR_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("github-connect-form");
  const list = document.getElementById("connected-repos");
  const dialog = document.getElementById("repo-picker");
  const options = [...dialog.querySelectorAll(".gh-picker-choice:not(.gh-picker-connected)")];
  const pickerDraft = new Set(options.filter(option =>
    option.querySelector(".gh-picker-toggle").checked
  ).map(option => option.dataset.repoId));
  const search = document.getElementById("search-repos");
  const save = document.getElementById("save-repos");
  const cancel = document.getElementById("cancel-editor");
  const saveHelp = document.getElementById("save-help");
  const stage = document.getElementById("stage-repos");
  const selectShown = document.getElementById("select-shown");
  const clearSelection = document.getElementById("clear-selection");
  const workingInput = document.getElementById("working-input");
  const workingDialog = document.getElementById("working-picker");
  const workingSelect = document.getElementById("working-choice");
  const initialWorking = form.dataset.working || null;
  const mayChangeWorking = form.dataset.mayChangeWorking === "true";
  const staged = new Map();
  const removed = new Set();
  let working = initialWorking;
  let workingExplicit = false;
  let saving = false;
  let opener = null;
  form.classList.add("is-enhanced");
  workingInput.disabled = false;
  const rows = () => [...list.querySelectorAll(".gh-agent-row")];
  const rowFor = id => rows().find(row => row.dataset.repoId === id);
  function closeMenus() {
    for (const menu of list.querySelectorAll(".gh-row-menu")) menu.hidden = true;
    for (const trigger of list.querySelectorAll(".gh-menu-trigger")) {
      trigger.setAttribute("aria-expanded", "false");
    }
  }
  function makeAddedRow(id, name, access) {
    const row = document.getElementById("added-row-template")
      .content.firstElementChild.cloneNode(true);
    row.dataset.repoId = id;
    row.dataset.name = name;
    row.dataset.added = "true";
    row.querySelector(".gh-row-name").textContent = name;
    row.querySelector(".gh-remove-action").setAttribute("aria-label", "Remove " + name);
    const repoInput = row.querySelector('input[name="repo"]');
    repoInput.value = id;
    const accessInput = row.querySelector(".gh-scoped-access");
    accessInput.name = "access_" + id;
    accessInput.value = access;
    list.appendChild(row);
    return row;
  }
  function render() {
    for (const row of rows()) {
      const id = row.dataset.repoId;
      const isRemoved = removed.has(id);
      const isAdding = staged.has(id);
      row.classList.toggle("gh-removed-row", isRemoved);
      row.classList.toggle("gh-added-row", isAdding && !isRemoved);
      const removeInput = row.querySelector('input[name="remove"]');
      if (removeInput) removeInput.disabled = !isRemoved;
      const repoInput = row.querySelector('input[name="repo"]');
      if (repoInput) repoInput.disabled = !isAdding;
      const accessInput = row.querySelector(".gh-scoped-access");
      if (accessInput) {
        accessInput.disabled = !isAdding;
        if (isAdding) accessInput.value = staged.get(id);
      }
      row.querySelector(".gh-working-badge").hidden = isRemoved || working !== id;
      row.querySelector(".gh-pending-label").textContent =
        isRemoved ? "Will be removed when you save" :
        isAdding ? "Will be added when you save" :
        working === id && working !== initialWorking ? "Working repo when you save" : "";
      const removeButton = row.querySelector(".gh-remove-action");
      const undoButton = row.querySelector(".gh-undo-action");
      if (removeButton) removeButton.hidden = isRemoved;
      if (undoButton) undoButton.hidden = !isRemoved;
      const action = row.querySelector(".gh-working-action");
      if (action) action.querySelector(".gh-working-action-label").textContent =
        working === id ? "Change working repo" :
        working === null ? "Choose working repo" : "Use as working repo";
    }
    closeMenus();
    const added = staged.size;
    const removedCount = removed.size;
    const workingChanged = working !== initialWorking;
    const parts = [];
    if (added) parts.push("add " + added + " " + plural(added));
    if (removedCount) parts.push("remove " + removedCount + " " + plural(removedCount));
    if (workingChanged) parts.push("change working repo");
    document.getElementById("selection-status").textContent =
      parts.length ? "Not saved yet: " + parts.join(", ") : "No changes to save";
    const empty = list.querySelector(".gh-editor-empty");
    if (empty) empty.hidden = rows().length > 0;
    document.getElementById("discard-repos").hidden = !parts.length;
    cancel.hidden = !!parts.length;
    save.hidden = !parts.length;
    saveHelp.hidden = !parts.length;
    save.disabled = !parts.length;
    save.textContent = "Save changes";
    workingInput.value = workingExplicit ||
      (working !== initialWorking && !removed.has(initialWorking))
      ? (working || "clear") : "keep";
  }
  const checkbox = option => option.querySelector(".gh-picker-toggle");
  const plural = n => n === 1 ? "repo" : "repos";
  function resetPicker() {
    for (const option of options) {
      const id = option.dataset.repoId;
      checkbox(option).checked = pickerDraft.has(id);
    }
    search.value = "";
    for (const option of options) option.hidden = false;
    document.getElementById("no-search-results").hidden = true;
    updatePicker();
  }
  function updatePicker() {
    const count = options.filter(o => checkbox(o).checked).length;
    const shown = options.filter(o => !o.hidden).length;
    stage.disabled = count === 0 && staged.size === 0;
    stage.textContent = "Add to list";
    selectShown.textContent = "Select all " + shown + " shown";
    selectShown.disabled = shown === 0;
    clearSelection.disabled = count === 0;
  }
  document.getElementById("open-repo-picker").addEventListener("click", event => {
    opener = event.currentTarget;
    resetPicker();
    dialog.showModal();
    search.focus();
  });
  document.getElementById("close-repo-picker").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { if (opener) opener.focus(); });
  function rememberPicker() {
    pickerDraft.clear();
    for (const option of options) {
      if (checkbox(option).checked) pickerDraft.add(option.dataset.repoId);
    }
    updatePicker();
  }
  dialog.addEventListener("change", rememberPicker);
  selectShown.addEventListener("click", () => {
    for (const option of options) if (!option.hidden) checkbox(option).checked = true;
    rememberPicker();
  });
  clearSelection.addEventListener("click", () => {
    for (const option of options) checkbox(option).checked = false;
    rememberPicker();
  });
  for (const button of dialog.querySelectorAll(".gh-owner-all")) {
    button.addEventListener("click", () => {
      search.value = "";
      for (const option of options) {
        option.hidden = false;
        if (option.dataset.installation === button.dataset.installation) {
          checkbox(option).checked = true;
        }
      }
      document.getElementById("no-search-results").hidden = true;
      rememberPicker();
    });
  }
  search.addEventListener("input", () => {
    const query = search.value.trim().toLowerCase();
    for (const option of options) {
      option.hidden = !option.dataset.name.toLowerCase().includes(query);
    }
    document.getElementById("no-search-results").hidden = options.some(o => !o.hidden);
    updatePicker();
  });
  stage.addEventListener("click", () => {
    const selected = options.filter(o => checkbox(o).checked);
    staged.clear();
    for (const option of selected) {
      const id = option.dataset.repoId;
      staged.set(id, "write");
      if (!rowFor(id)) makeAddedRow(id, option.dataset.name, staged.get(id));
      removed.delete(id);
    }
    for (const row of rows()) {
      if (row.dataset.added === "true" && !staged.has(row.dataset.repoId)) {
        if (working === row.dataset.repoId) { working = null; workingExplicit = true; }
        row.remove();
      }
    }
    dialog.close();
    render();
  });
  list.addEventListener("click", event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest(".gh-agent-row");
    const id = row.dataset.repoId;
    if (button.dataset.action === "menu") {
      const menu = row.querySelector(".gh-row-menu");
      const opening = menu.hidden;
      closeMenus();
      menu.hidden = !opening;
      button.setAttribute("aria-expanded", String(opening));
      if (opening) menu.querySelector("button").focus();
      return;
    }
    if (button.dataset.action === "working" && mayChangeWorking) {
      if (working === id) {
        workingSelect.replaceChildren();
        for (const choice of rows().filter(item =>
          !removed.has(item.dataset.repoId) && item.querySelector(".gh-working-action"))) {
          const entry = new Option(choice.dataset.name, choice.dataset.repoId);
          workingSelect.add(entry);
        }
        workingSelect.add(new Option("No working repo", "clear"));
        workingSelect.value = id;
        workingDialog.showModal();
        workingSelect.focus();
      } else {
        working = id;
        workingExplicit = true;
        button.focus();
      }
    } else if (button.dataset.action === "remove") {
      staged.delete(id);
      if (row.dataset.added === "true") {
        row.remove();
      } else {
        removed.add(id);
      }
      if (working === id) { working = null; if (id !== initialWorking) workingExplicit = true; }
    } else if (button.dataset.action === "undo") {
      removed.delete(id);
      if (!workingExplicit && id === initialWorking) working = id;
    }
    render();
  });
  document.getElementById("confirm-working").addEventListener("click", () => {
    working = workingSelect.value === "clear" ? null : workingSelect.value;
    workingExplicit = true;
    workingDialog.close();
    render();
  });
  document.getElementById("close-working").addEventListener("click", () => workingDialog.close());
  document.addEventListener("click", event => {
    if (!event.target.closest(".gh-row-menu, .gh-menu-trigger")) closeMenus();
  });
  list.addEventListener("keydown", event => {
    const menu = event.target.closest(".gh-row-menu");
    if (event.key === "Escape" && menu) {
      const row = menu.closest(".gh-agent-row");
      closeMenus();
      row.querySelector(".gh-menu-trigger").focus();
    }
  });
  document.getElementById("discard-repos").addEventListener("click", () => {
    staged.clear(); removed.clear(); working = initialWorking; workingExplicit = false;
    for (const row of rows()) if (row.dataset.added === "true") row.remove();
    render();
  });
  form.addEventListener("submit", event => {
    if (saving || save.disabled) { event.preventDefault(); return; }
    saving = true;
    save.disabled = true;
    save.textContent = "Saving…";
    form.setAttribute("aria-busy", "true");
  });
  render();
})();
</script>
"""


@dataclass(frozen=True)
class _PickerOption:
    repo_id: int
    name: str
    repair: bool
    needs_write: bool
    can_write: bool
    installation_id: int


def _agent_editor_page(
    *,
    root: str,
    state: str,
    invitation_hash: str,
    secret: str,
    agent_name: str,
    workspace: str,
    platform: str,
    grants: list[github_access.AgentRepo],
    installations: list[_Installation],
    snapshot: str,
    cancel_url: str,
    install_url: str,
    needed: Mapping[int, bool] | None = None,
    requested_repo: str | None = None,
    missing_names: tuple[str, ...] = (),
    selection_error: str | None = None,
    pending_installation: _PendingInstallationRequest | None = None,
) -> Response:
    """Render one agent's grants and an optional staged add dialog."""
    esc = html.escape
    snapshot_digest = hashlib.sha256(snapshot.encode()).hexdigest()
    needed = needed or {}
    usable = {
        repo.id: repo
        for installation in installations
        for repo in installation.repos
        if repo.usable
    }
    current = {repo.repo_id for repo in grants}
    available = sorted(
        (repo for repo_id, repo in usable.items() if repo_id not in current),
        key=lambda repo: repo.full_name.casefold(),
    )
    requested_id = next(
        (
            repo.id
            for repo in available
            if requested_repo and repo.full_name.casefold() == requested_repo.casefold()
        ),
        None,
    )
    repairable = [repo for repo in grants if repo.repo_id in usable and repo.repo_id in needed]
    working = next((repo for repo in grants if repo.is_working_repo), None)
    may_change_working = working is None or working.repo_id in usable
    place = _place_label(platform)
    options = [
        _PickerOption(
            repo_id=repo.repo_id,
            name=repo.full_name,
            repair=True,
            needs_write=bool(needed.get(repo.repo_id)),
            can_write=usable[repo.repo_id].max_access == "write",
            installation_id=usable[repo.repo_id].installation_id,
        )
        for repo in repairable
    ] + [
        _PickerOption(
            repo_id=repo.id,
            name=repo.full_name,
            repair=False,
            needs_write=bool(needed.get(repo.id)),
            can_write=repo.max_access == "write",
            installation_id=repo.installation_id,
        )
        for repo in available
    ]
    parts = [
        f'<p class="gh-status-line">{len(grants)} '
        f"{'repo' if len(grants) == 1 else 'repos'} connected.</p>"
        + (
            '<p class="gh-action-line">Add a repo, then choose its working repo.</p>'
            if not grants
            else '<p class="gh-action-line">Add repos or change the working repo.</p>'
        ),
        '<form id="github-connect-form" class="gh-editor" method="post" '
        f'data-working="{working.repo_id if working else ""}" '
        f'data-may-change-working="{str(may_change_working).lower()}" '
        f'action="{esc(root, quote=True)}/oauth/github/confirm">',
        f'<input type="hidden" name="state" value="{esc(state, quote=True)}">',
        f'<input type="hidden" name="invitation" value="{esc(invitation_hash, quote=True)}">',
        f'<input type="hidden" name="receipt" '
        f'value="{_receipt_signature(state, invitation_hash, secret)}">',
        f'<input type="hidden" name="snapshot" value="{snapshot_digest}">',
        f'<input type="hidden" name="snapshot_sig" '
        f'value="{_snapshot_signature(state, invitation_hash, snapshot_digest, secret)}">',
        '<input type="hidden" name="access" value="write">',
        '<input id="working-input" type="hidden" name="working" value="keep" disabled>',
        f'<div class="gh-context">{platform_mark(platform)}'
        f"<span>{place}: {esc(workspace)}</span></div>",
        '<div class="gh-editor-toolbar"><h2>Repos for this agent</h2>',
        '<button id="open-repo-picker" class="gh-add-button gh-js" type="button">'
        f"{icon('plus')}Add repos</button></div>",
    ]
    if pending_installation is not None and pending_installation.found:
        organization = pending_installation.account_login
        parts.append(
            '<p class="gh-editor-notice">'
            + (
                f"{'An' if organization[0].lower() in 'aeiou' else 'A'} {esc(organization)} owner"
                if organization
                else "A GitHub owner"
            )
            + ' must approve Daimon. Check again after approval. <a href="'
            + f"{esc(root, quote=True)}/oauth/github/confirm?"
            + f'{urlencode({"state": state})}">Check again</a></p>'
        )
    if selection_error:
        parts.append(f'<p class="gh-inline-error" role="alert">{esc(selection_error)}</p>')
    parts.append('<div id="connected-repos" class="gh-connected-list">')
    if not grants:
        parts.append('<p class="gh-editor-empty">No repos connected yet.</p>')
    for repo in grants:
        editable = repo.repo_id in usable
        repo_name = esc(repo.full_name) if editable else "A connected repo you cannot access"
        data_name = esc(repo.full_name, quote=True) if editable else ""
        parts.append(
            f'<article class="gh-agent-row" data-repo-id="{repo.repo_id}" '
            f'data-name="{data_name}" '
            f'data-access="{repo.ceiling_access}">'
            '<div class="gh-row-top"><strong class="gh-row-name">'
            f'{repo_name}</strong><div class="gh-row-actions gh-js">'
        )
        if editable:
            parts.append(
                '<button class="gh-row-action gh-remove-action" type="button" '
                f'data-action="remove" aria-label="Remove {esc(repo.full_name, quote=True)}">'
                f"{icon('trash-2')}Remove</button>"
                '<button class="gh-row-action gh-undo-action" type="button" '
                'data-action="undo" '
                f'aria-label="Undo removal of {esc(repo.full_name, quote=True)}" '
                f"hidden>{icon('undo-2')}Undo</button>"
            )
            if may_change_working and repo.status == "active":
                parts.append(
                    '<button class="gh-row-action gh-working-action" type="button" '
                    'data-action="working">'
                    f'{icon("folder")}<span class="gh-working-action-label">'
                    + (
                        "Change working repo"
                        if repo.is_working_repo
                        else "Choose working repo"
                        if working is None
                        else "Use as working repo"
                    )
                    + "</span></button>"
                )
        parts.append('</div></div><div class="gh-row-meta">')
        parts.append(
            f'<span class="gh-working-badge"{"" if repo.is_working_repo else " hidden"}>'
            f"{icon('folder')}Working repo</span>"
            '<span class="gh-pending-label" aria-live="polite"></span>'
        )
        if repo.status != "active":
            parts.append('<span class="gh-repo-state">Unavailable</span>')
        parts.append("</div>")
        if not editable:
            parts.append(
                '<p class="gh-repo-locked">Your GitHub account has no permission on this repo, '
                "so you can't edit it here.</p>"
            )
        if repo.repo_id in needed and needed[repo.repo_id]:
            parts.append('<p class="gh-repo-state">Needs read and write to finish setup.</p>')
        if editable:
            parts.append(f'<input type="hidden" name="remove" value="{repo.repo_id}" disabled>')
            if repo in repairable:
                parts.append(
                    f'<input type="hidden" name="repo" value="{repo.repo_id}" disabled>'
                    f'<input class="gh-scoped-access" type="hidden" '
                    f'name="access_{repo.repo_id}" value="write" disabled>'
                )
        parts.append("</article>")
    parts.append("</div>")
    if not grants or not options:
        parts.append(
            '<p class="gh-editor-install">Missing a repo? Add it to the Daimon GitHub '
            'App on GitHub. <a href="'
            + esc(install_url, quote=True)
            + '">Open GitHub setup</a></p>'
        )
    parts.append(
        '<details class="gh-access-help"><summary>'
        f"{icon('info')}How this works</summary>"
        "<p>The working repo opens in the agent's workspace. The agent can read and change "
        "connected repos. Access also depends on the person asking and their GitHub permissions."
        + (" Setup is incomplete until the required repos are connected." if missing_names else "")
        + "</p></details>"
    )
    parts.append(
        '<dialog id="repo-picker" class="gh-repo-dialog" aria-labelledby="repo-picker-title">'
        '<div class="gh-dialog-head"><h2 id="repo-picker-title">Add repos</h2>'
        '<button id="close-repo-picker" class="gh-dialog-close" type="button" '
        f'aria-label="Close add repos">{icon("x")}</button></div>'
        '<p class="gh-status-line gh-dialog-copy">The agent can read and change these repos.</p>'
        '<p class="gh-action-line gh-dialog-copy">Choose repos, then review your changes.</p>'
        f'<div class="gh-dialog-search">{icon("search")}'
        '<input id="search-repos" class="gh-search" type="search" '
        'aria-label="Search repos" placeholder="Search repos" autocomplete="off"></div>'
        '<div class="gh-dialog-link gh-dialog-bulk">'
        '<button id="select-shown" class="gh-link-button" type="button">'
        f"Select all {len(options)} shown</button>"
        '<button id="clear-selection" class="gh-link-button" type="button">Clear selection</button>'
        + "".join(
            f'<button class="gh-link-button gh-owner-all" type="button" '
            f'data-installation="{installation.id}">'
            f"Everything in {esc(installation.owner_login)}</button>"
            for installation in installations
            if any(option.installation_id == installation.id for option in options)
        )
        + "</div>"
        '<div class="gh-dialog-choices">'
    )
    if not options:
        parts.append("<p>No repos available to add.</p>")
    for option in options:
        required_write = option.needs_write
        parts.append(
            f'<div class="gh-picker-choice" data-repo-id="{option.repo_id}" '
            f'data-name="{esc(option.name, quote=True)}" '
            f'data-installation="{option.installation_id}" '
            f'data-max-access="{"write" if option.can_write else "read"}" '
            f'data-required-write="{str(required_write).lower()}">'
            '<label class="gh-picker-label"><input class="gh-picker-toggle" type="checkbox"'
            + (" checked" if option.repo_id == requested_id else "")
            + ">"
            f"<span>{esc(option.name)}"
            + ("<small>Finish setup</small>" if option.repair else "")
            + ("<small>Required to finish setup</small>" if required_write else "")
            + "</span></label></div>"
        )
    for repo in grants:
        if repo.repo_id not in usable or repo in repairable:
            continue
        parts.append(
            '<div class="gh-picker-choice gh-picker-connected">'
            f"{icon('circle-check')}<span>{esc(repo.full_name)}"
            "<small>Already connected</small></span></div>"
        )
    parts.append(
        '<p id="no-search-results" hidden>No matching repos.</p></div>'
        + '<p class="gh-dialog-link">Missing a repo? Add it to the Daimon GitHub App '
        + 'on GitHub. <a href="'
        + esc(install_url, quote=True)
        + '">Open GitHub setup</a></p>'
        '<div class="gh-dialog-footer"><span id="picker-count">Not saved yet. Save changes '
        "after adding to the list.</span>"
        '<button id="stage-repos" class="gh-primary" type="button" disabled>Add to list</button>'
        "</div></dialog>"
    )
    parts.append(
        '<dialog id="working-picker" class="gh-repo-dialog" '
        'aria-labelledby="working-picker-title">'
        '<div class="gh-dialog-head"><h2 id="working-picker-title">Change working repo</h2>'
        '<button id="close-working" class="gh-dialog-close" type="button" '
        f'aria-label="Close working repo chooser">{icon("x")}</button></div>'
        '<p class="gh-dialog-copy">Opens in the agent\'s workspace.</p>'
        '<label class="gh-dialog-copy" for="working-choice">Working repo</label>'
        '<select id="working-choice" class="gh-working-select"></select>'
        '<div class="gh-dialog-footer"><button id="confirm-working" class="gh-primary" '
        'type="button">Choose working repo</button></div></dialog>'
    )
    parts.append(
        '<template id="added-row-template"><article class="gh-agent-row">'
        '<div class="gh-row-top"><strong class="gh-row-name"></strong>'
        '<div class="gh-row-actions">'
        f'<button class="gh-row-action gh-remove-action" type="button" data-action="remove">'
        f"{icon('trash-2')}Remove</button>"
        '<button class="gh-row-action gh-undo-action" type="button" data-action="undo" hidden>'
        f"{icon('undo-2')}Undo</button>"
        + (
            '<button class="gh-row-action gh-working-action" type="button" '
            f'data-action="working">{icon("folder")}'
            '<span class="gh-working-action-label">Choose working repo</span></button>'
            if may_change_working
            else ""
        )
        + '</div></div><div class="gh-row-meta">'
        f'<span class="gh-working-badge" hidden>{icon("folder")}Working repo</span>'
        '<span class="gh-pending-label" aria-live="polite"></span></div>'
        '<input type="hidden" name="repo" disabled>'
        '<input class="gh-scoped-access" type="hidden" disabled>' + "</article></template>"
    )
    # Browsers without scripting still get a complete native form.
    parts.append('<noscript><section class="gh-fallback"><h2>Edit repos</h2>')
    for repo in grants:
        if repo.repo_id not in usable:
            continue
        parts.append(
            f'<label><input type="checkbox" name="remove" value="{repo.repo_id}">'
            f"Remove {esc(repo.full_name)}</label>"
        )
        if repo in repairable:
            parts.append(
                f'<label><input type="checkbox" name="repo" value="{repo.repo_id}">'
                f"Finish setup for {esc(repo.full_name)}</label>"
                f'<input type="hidden" name="access_{repo.repo_id}" value="write">'
            )
    for repo in available:
        parts.append(
            f'<label><input type="checkbox" name="repo" value="{repo.id}"'
            + (" checked" if repo.id == requested_id else "")
            + ">"
            f"Add {esc(repo.full_name)}</label>"
            f'<input type="hidden" name="access_{repo.id}" value="write">'
        )
    parts.append(
        '<label>Working repo <select name="working"><option value="keep">Keep current</option>'
    )
    if may_change_working:
        parts.append('<option value="clear">None</option>')
        for repo in grants:
            if repo.repo_id in usable and repo.status == "active":
                parts.append(f'<option value="{repo.repo_id}">{esc(repo.full_name)}</option>')
        for repo in available:
            parts.append(f'<option value="{repo.id}">{esc(repo.full_name)} (add above)</option>')
    parts.append("</select></label></section></noscript>")
    parts.append(
        '<div class="gh-editor-footer"><div class="gh-editor-footer-inner">'
        '<div><strong id="selection-status" aria-live="polite">No changes to save</strong>'
        '<small id="save-help">Changes apply when you save.</small></div>'
        '<div class="gh-editor-footer-actions">'
        '<button id="discard-repos" class="gh-editor-discard gh-js" type="button" hidden>'
        "Discard changes</button>"
        '<button id="save-repos" class="gh-primary" type="submit">Save changes</button>'
        f'<a id="cancel-editor" class="gh-editor-cancel" '
        f'href="{esc(cancel_url, quote=True)}">Cancel</a>'
        "</div></div></div></form>"
    )
    parts.append(_EDITOR_SCRIPT)
    return github_page(
        title=f"{agent_name}'s GitHub repos", body_html="".join(parts), kind="picker"
    )


_PICKER_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("github-connect-form");
  const flow = document.querySelector(".gh-flow");
  const all = [...form.querySelectorAll('input[name="repo"]')];
  const boxes = all.filter(box => !box.disabled);
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
      row.hidden = (selectedOnly && (box.disabled || !box.checked)) ||
        !row.textContent.toLowerCase().includes(query);
      if (!row.hidden && !box.disabled) visible++;
    }
    for (const owner of form.querySelectorAll(".gh-repo-owner")) {
      owner.hidden = !owner.querySelector(".gh-choice:not([hidden])");
    }
    const results = !!query || selectedOnly;
    count.textContent = query ? `${visible} matching ${plural(visible)}` :
      selectedOnly ? `${visible} selected ${plural(visible)}` : `${boxes.length} repos available`;
    showSelected.hidden = selected === 0;
    showSelected.textContent = selectedOnly ? "Show all" : "Show selected";
    changeAccess.hidden = true;
    const visibleBoxes = boxes.filter(box => !box.closest(".gh-choice").hidden);
    const allVisibleChecked = visibleBoxes.length > 0 && visibleBoxes.every(box => box.checked);
    bulk.textContent = allVisibleChecked ? "Clear selection" : `Select all ${visibleBoxes.length}`;
    bulk.hidden = visibleBoxes.length === 0;
    status.textContent = selected ? `${selected} selected.` : "0 selected";
    submit.disabled = selected === 0;
    submitLabel.textContent = "Connect selected repos";
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
    const doing = "Connecting";
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
    return '<input type="radio" name="access" value="write" checked hidden>'


def _confirmation_page(
    *,
    root: str,
    state: str,
    invitation_hash: str,
    secret: str,
    cancel_url: str,
    installations: list[_Installation],
    platform: str,
    workspace: str,
    agent_name: str | None,
    selection_error: str | None = None,
    already_added: frozenset[int] = frozenset(),
    needed: Mapping[int, bool] | None = None,
    requested_repo: str | None = None,
    install_url: str = "",
) -> Response:
    """Render the same picker used by the live route and screenshot capture.

    Repos in `already_added` show ticked and greyed; only new ticks are sent.
    Repos in `needed` (repo id: needs write) are what the agent still needs
    before it can drop its old GitHub token: ticked, and still changeable.
    """
    needed = needed or {}
    place = _place_label(platform)
    context = (
        f'<div class="gh-context">{platform_mark(platform)}'
        f"<span>{place}: {html.escape(workspace)}</span></div>"
    )
    parts = [
        f'<p class="gh-status-line">Connect repos to {html.escape(agent_name)}.</p>'
        if agent_name
        else '<p class="gh-status-line">Connect GitHub repos.</p>',
        '<p class="gh-action-line">Choose repos the agent can read and change.</p>',
        f'<form id="github-connect-form" method="post" '
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
            '<div class="gh-repo-list" aria-label="Repos you can connect">',
            f'<p class="gh-inline-error" role="alert">{html.escape(selection_error)}</p>'
            if selection_error
            else "",
        ]
    )
    for installation in installations:
        owned = [repo for repo in installation.repos if repo.usable]
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
            note = (
                "Already connected"
                if added
                else (NEEDS_WRITE if needed[repo.id] else NEEDED)
                if repo.id in needed
                else None
            )
            parts.append(
                f'<label class="gh-choice repo-choice{" is-added" if added else ""}">'
                f'<input type="checkbox" name="repo" value="{repo.id}"'
                + (
                    " checked disabled"
                    if added
                    else " checked"
                    if repo.id in needed
                    or (
                        requested_repo is not None
                        and repo.full_name.casefold() == requested_repo.casefold()
                    )
                    else ""
                )
                + ">"
                f'<span><span class="gh-repo-prefix">{html.escape(prefix)}/</span>'
                f'<span class="gh-repo-name">{html.escape(name)}</span>'
                + (f'<span class="gh-added">{note}</span>' if note else "")
                + "</span></label>"
            )
        parts.append("</div>")
    parts.extend(
        [
            '<p class="gh-empty" hidden>No repos match this search.</p></div>'
            + '<p class="gh-dialog-link">Missing a repo? Add it to the Daimon GitHub App '
            "on GitHub."
            + (
                f' <a href="{html.escape(install_url, quote=True)}">Open GitHub setup</a>'
                if install_url
                else ""
            )
            + "</p>"
            + "</section>",
            '<aside class="gh-side">',
            _access_choices(agent=agent_name is not None),
            "</aside></div>",
            '<details class="gh-access-help"><summary>How this works</summary>'
            "<p>The agent can read and change connected repos. People also need their own "
            "GitHub permission to use them."
            + (f" {html.escape(audience_line(agent_name, write=True))}" if agent_name else "")
            + "</p></details>",
            '<div class="gh-finish"><div class="gh-finish-status">'
            '<p id="selection-status" aria-live="polite">0 selected</p>'
            '<button class="gh-link-button" id="change-access" type="button" hidden>'
            "Change</button>" + '</div><div class="gh-finish-actions">',
            '<button class="gh-primary" id="connect-repos" type="submit">'
            f'{icon("github")}<span class="gh-button-label">'
            "Connect selected repos</span></button>"
            f'<a class="gh-link" href="{cancel_url}">Cancel</a></div></div>',
            "</form>",
            _PICKER_SCRIPT,
        ]
    )
    return github_page(
        title="Choose GitHub repos",
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
                    permission=github_connect.github_permission(repo.permissions),
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

    async def live_agent(invitation: github_connect.Invitation) -> BetaManagedAgentsAgent | None:
        """The link's agent as Managed Agents has it now; None if it can't be read."""
        if anthropic is None or invitation.agent_id is None or not invitation.agent_ma_id:
            return None
        try:
            return await find_agent_by_derived_uuid(
                anthropic, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
            )
        except APIError:
            return None

    async def may_manage_agent(
        session: AsyncSession,
        invitation: github_connect.Invitation,
        requester: AccountIdentityRow,
        live: BetaManagedAgentsAgent | None,
    ) -> bool:
        if invitation.agent_id is None:
            return True
        if invitation.operator_issued:
            return requester.tenant_id == invitation.tenant_id and requester.role == Role.ADMIN
        if deployment_default is None:
            return False
        metadata = live.metadata if live is not None else {}
        return await requester_manages_agent(
            session,
            tenant_id=invitation.tenant_id,
            account_id=invitation.requester_account_id,
            platform=requester.platform,
            platform_user_id=requester.platform_user_id,
            agent_name=invitation.agent_name,
            ma_agent_id=invitation.agent_ma_id,
            default=deployment_default,
            is_daimon_managed=(
                None if live is None else metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            ),
            other_names=agent_pin_names(live.name, metadata) if live is not None else (),
        )

    async def successful_page(
        state: str, cookie: str, invitation_hash: str = ""
    ) -> Response | None:
        async with sessionmaker() as session:
            invitation = await github_connect.successful_confirmation(
                session, state=state, cookie=cookie, invitation_hash=invitation_hash
            )
            authenticated = invitation is not None
            if invitation is None and invitation_hash:
                status, candidate = await github_connect.invitation_status(session, invitation_hash)
                if status == "used" and candidate is not None:
                    invitation = candidate
            if invitation is None:
                return None
            requester = await get_account_with_tenant(
                session, account_id=invitation.requester_account_id
            )
            if invitation.agent_id is not None:
                if not authenticated:
                    return _already_connected_page(invitation.connected_repo_count)
                live = await live_agent(invitation)
                if requester is None or not await may_manage_agent(
                    session, invitation, requester, live
                ):
                    return _error("You can no longer manage this agent.", 403)
            agent_grants = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
        if invitation.agent_id is not None:
            return _saved_agent_page(invitation.agent_name or "Agent", agent_grants)
        back = _back_to_chat(requester.platform, requester.external_id) if requester else ""
        return _already_connected_page(
            invitation.connected_repo_count,
            back,
            agent_name=invitation.agent_name,
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
            raw_body = await _read_capped_body(request, _MAX_CONFIRM_BODY_BYTES)
            if raw_body is None:
                return _selection_too_large()
            try:
                fields = parse_qs(
                    raw_body.decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=_MAX_CONFIRM_FIELDS,
                )
            except UnicodeDecodeError:
                return _error("Selection could not be verified.")
            except ValueError:  # parse_qs: more than _MAX_CONFIRM_FIELDS fields
                return _selection_too_large()
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

        def transient_error(message: str, status: int) -> Response:
            """An upstream failure the person can retry.

            After a POST the retry re-posts what they ticked: a link would be a
            GET, which renders the page afresh and drops the selection. The form
            posts to the bare confirm path, so state stays out of the URL.
            """
            if request.method == "POST":
                return _error(message, status, f"{root}/oauth/github/confirm", retry_fields=fields)
            return _error(message, status, retry_confirm_url)

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
            if requester is None:
                return _error()
            agent_repos = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
            missing_access: dict[str, bool] = {}
            missing_names: list[str] = []
            already_added = frozenset(
                repo.repo_id for repo in agent_repos if repo.status == "active"
            )
        token = decrypt_token(fernet, flow.encrypted_user_token)
        try:
            async with factory() as client, asyncio.timeout(_INSTALLATIONS_DEADLINE_SECONDS):
                installations = await _installations(client, token)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError):
            used = await successful_page(state, cookie, invitation_hash)
            if used is not None:
                return used
            return transient_error("Couldn't reach GitHub", 502)
        needed = {
            repo.id: missing_access[repo.full_name.casefold()]
            for installation in installations
            for repo in installation.repos
            if repo.usable and repo.full_name.casefold() in missing_access
        }
        if invitation.agent_id is not None:
            install_url = (
                f"https://github.com/apps/{config.app_slug}/installations/new?"
                f"{urlencode({'state': state})}"
            )
            live = await live_agent(invitation)
            async with sessionmaker() as session:
                await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                current_requester = await get_account_with_tenant(
                    session, account_id=invitation.requester_account_id
                )
                if current_requester is None or not await may_manage_agent(
                    session, invitation, current_requester, live
                ):
                    return _error("You can no longer manage this agent.", 403)
                grants = await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                snapshot = await github_connect.agent_repo_snapshot(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
            cancel_url = f"{root}/oauth/github/confirm?{urlencode({'state': state, 'cancel': '1'})}"
            if request.method == "GET":
                pending_installation = _PendingInstallationRequest(False)
                if not any(repo.usable for item in installations for repo in item.repos):
                    try:
                        async with factory() as client:
                            pending_installation = await has_pending_installation_request(
                                client,
                                app_id=app_id,
                                private_key=private_key,
                                github_user_id=flow.github_user_id,
                            )
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        return transient_error("Couldn't reach GitHub", 502)
                return _agent_editor_page(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=secret,
                    agent_name=invitation.agent_name or "Agent",
                    workspace=invitation.workspace_label,
                    platform=requester.platform,
                    grants=grants,
                    installations=installations,
                    snapshot=snapshot,
                    cancel_url=cancel_url,
                    install_url=install_url,
                    needed=needed,
                    requested_repo=invitation.requested_repo,
                    missing_names=tuple(missing_names),
                    pending_installation=pending_installation,
                )
            posted_snapshot = fields.get("snapshot", [""])[0]
            posted_signature = fields.get("snapshot_sig", [""])[0]
            if not hmac.compare_digest(
                posted_signature,
                _snapshot_signature(state, flow.invitation_hash, posted_snapshot, secret),
            ):
                return _error("Selection could not be verified.", 400, retry_confirm_url)
            try:
                selected_ids = [int(value) for value in fields.get("repo", [])]
                removed_ids = [int(value) for value in fields.get("remove", [])]
            except ValueError:
                return _error("Selection could not be verified.", 400, retry_confirm_url)
            access_values = fields.get("access", [])
            working_values = fields.get("working", [])
            if (
                (
                    bool(access_values)
                    and (len(access_values) != 1 or access_values[0] not in ("read", "write"))
                )
                or len(working_values) != 1
                or len(selected_ids) != len(set(selected_ids))
                or len(removed_ids) != len(set(removed_ids))
            ):
                return _error("Selection could not be verified.", 400, retry_confirm_url)
            selected_access: dict[int, str] = {}
            for repo_id in selected_ids:
                scoped = fields.get(f"access_{repo_id}", [])
                if scoped:
                    if len(scoped) != 1 or scoped[0] not in ("read", "write"):
                        return _error("Selection could not be verified.", 400, retry_confirm_url)
                    selected_access[repo_id] = scoped[0]
                elif access_values:
                    selected_access[repo_id] = access_values[0]
                else:
                    return _error("Selection could not be verified.", 400, retry_confirm_url)
            visible = {
                repo.id: repo
                for installation in installations
                for repo in installation.repos
                if repo.usable
            }
            old = {repo.repo_id: repo for repo in grants}
            repairable = {repo.repo_id for repo in grants if repo.repo_id in needed}
            working_before = next((repo.repo_id for repo in grants if repo.is_working_repo), None)
            working_choice = working_values[0]
            if working_choice not in ("keep", "clear"):
                try:
                    working_id = int(working_choice)
                except ValueError:
                    return _error("Selection could not be verified.", 400, retry_confirm_url)
            else:
                working_id = None
            if (
                any(
                    repo_id not in visible or (repo_id in old and repo_id not in repairable)
                    for repo_id in selected_ids
                )
                or any(repo_id not in old or repo_id not in visible for repo_id in removed_ids)
                or bool(set(selected_ids) & set(removed_ids))
                or (
                    working_choice != "keep"
                    and working_before is not None
                    and working_before not in visible
                )
                or (
                    working_id is not None
                    and (
                        working_id not in visible
                        or (working_id not in old and working_id not in selected_ids)
                        or working_id in removed_ids
                    )
                )
            ):
                return _error("GitHub access could not be verified.", 403, retry_confirm_url)
            if not selected_ids and not removed_ids and working_choice == "keep":
                return _agent_editor_page(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=secret,
                    agent_name=invitation.agent_name or "Agent",
                    workspace=invitation.workspace_label,
                    platform=requester.platform,
                    grants=grants,
                    installations=installations,
                    snapshot=snapshot,
                    cancel_url=cancel_url,
                    install_url=install_url,
                    needed=needed,
                    requested_repo=invitation.requested_repo,
                    missing_names=tuple(missing_names),
                    selection_error="Choose a change to save.",
                )
            if posted_snapshot != hashlib.sha256(snapshot.encode()).hexdigest():
                return _error(
                    "The agent's repos changed. Reload and try again.", 409, retry_confirm_url
                )
            app_jwt = build_app_jwt(private_key, app_id, now=int(time.time()))
            by_installation = {install.id: install for install in installations}
            try:
                async with factory() as client:
                    details = [
                        await get_app_installation_details(
                            client, jwt=app_jwt, installation_id=installation_id
                        )
                        for installation_id in sorted(
                            {visible[repo_id].installation_id for repo_id in selected_ids}
                        )
                    ]
            except (httpx.HTTPError, ValueError):
                return transient_error("Couldn't reach GitHub", 502)
            if any(
                detail.account_id != by_installation[detail.installation_id].owner_id
                or detail.account_login.casefold()
                != by_installation[detail.installation_id].owner_login.casefold()
                or detail.suspended_at is not None
                for detail in details
            ):
                return _error("GitHub access could not be verified.", 403, retry_confirm_url)
            installation_writes = {
                detail.installation_id: installation_supports_write(detail.permissions)
                for detail in details
            }
            if any(
                not installation_writes.get(visible[repo_id].installation_id, False)
                for repo_id in selected_ids
            ):
                return _error(
                    "The GitHub App needs write access to connect these repos.",
                    403,
                    retry_confirm_url,
                )
            repos = [
                _repo_confirmation(
                    visible[repo_id],
                    requested=cast(Literal["read", "write"], selected_access[repo_id]),
                    needs_write=bool(needed.get(repo_id)),
                    installation_writes=installation_writes,
                )
                for repo_id in selected_ids
            ]
            if not invitation.operator_issued:
                if (
                    invitation.requester_platform_user_id is not None
                    and invitation.requester_platform_user_id != requester.platform_user_id
                ):
                    return _error("You can no longer manage this agent.", 403, retry_confirm_url)
                try:
                    still_admin = await _attest_native_admin(
                        requester=requester,
                        settings=settings,
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                    )
                except AdminAttestationUnavailable:
                    return transient_error(_ADMIN_RETRY, 503)
                if not still_admin:
                    return _error("You can no longer manage this agent.", 403, retry_confirm_url)
            try:
                async with sessionmaker.begin() as session:
                    await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    transaction_requester = await get_account_with_tenant(
                        session, account_id=invitation.requester_account_id
                    )
                    manages = transaction_requester is not None and await may_manage_agent(
                        session, invitation, transaction_requester, live
                    )
                    if not manages:
                        return _error("You can no longer manage this agent.", 403)
                    saved = await github_connect.confirm(
                        session,
                        state=state,
                        cookie=cookie,
                        github_user_id=flow.github_user_id,
                        repos=repos,
                        requester_manages_agent=manages,
                        expected_agent_snapshot=snapshot,
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
                            await enqueue_reconciliation(
                                session,
                                installation_id=detail.installation_id,
                                delivery_id=f"connect:{uuid.uuid4()}",
                                event="connect",
                                deleted=False,
                                now=datetime.now(UTC),
                            )
                        for repo_id in removed_ids:
                            await github_access.remove_agent_repo(
                                session,
                                tenant_id=invitation.tenant_id,
                                agent_id=invitation.agent_id,
                                repo_id=repo_id,
                                account_id=invitation.requester_account_id,
                            )
                        if repos:
                            await github_connect.activate_confirmed_agent(
                                session, invitation=invitation, repos=repos
                            )
                        if working_choice != "keep":
                            selected_name = (
                                None if working_id is None else visible[working_id].full_name
                            )
                            await github_access.set_working_repo(
                                session,
                                tenant_id=invitation.tenant_id,
                                agent_id=invitation.agent_id,
                                repo_name=selected_name,
                                account_id=invitation.requester_account_id,
                            )
                        if repos:
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
                            reason="agent repos saved",
                            github_repo_ids=selected_ids + removed_ids,
                        )
            except github_connect.StaleAgentReposError:
                return _error(
                    "The agent's repos changed. Reload and try again.", 409, retry_confirm_url
                )
            except github_access.SecondInstallationError as exc:
                return _error(str(exc), 400, retry_confirm_url)
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) in ("40001", "40P01"):
                    return _error(
                        "The agent's repos changed. Reload and try again.", 409, retry_confirm_url
                    )
                raise
            except github_connect.PinnedAgentConnectionError:
                return _error(github_connect.PINNED_AGENT_MESSAGE)
            except ValueError:
                return _error(
                    "This change could not be saved. Reload and try again.", 400, retry_confirm_url
                )
            if not saved:
                used = await successful_page(state, cookie, invitation_hash)
                return used if used is not None else _error()
            await revoke_user_token(token)
            response = await successful_page(state, cookie)
            if response is None:
                return _error()
            response.set_cookie(
                _COOKIE,
                cookie,
                max_age=7 * 24 * 60 * 60,
                httponly=True,
                secure=root.startswith("https://"),
                samesite="lax",
            )
            return response
        if request.method == "POST":
            try:
                selected_ids = [int(value) for value in fields.get("repo", [])]
            except ValueError:
                return _error("Selection could not be verified.", retry_url=retry_confirm_url)
            # Repos already on the agent keep their access; only new ticks count.
            selected_ids = [repo_id for repo_id in selected_ids if repo_id not in already_added]
            if not selected_ids:
                selection_error = "Select at least one repo"
                return _confirmation_page(
                    root=root,
                    state=state,
                    invitation_hash=flow.invitation_hash,
                    secret=secret,
                    cancel_url=(
                        f"{root}/oauth/github/confirm?{urlencode({'state': state, 'cancel': '1'})}"
                    ),
                    installations=installations,
                    platform=requester.platform,
                    workspace=invitation.workspace_label,
                    agent_name=invitation.agent_name,
                    selection_error=selection_error,
                    already_added=already_added,
                    needed=needed,
                    requested_repo=invitation.requested_repo,
                    install_url=(
                        f"https://github.com/apps/{config.app_slug}/installations/new?"
                        f"{urlencode({'state': state})}"
                    ),
                )
            visible = {
                repo.id: repo for install in installations for repo in install.repos if repo.usable
            }
            if len(set(selected_ids)) != len(selected_ids):
                return _error("Selection could not be verified.", retry_url=retry_confirm_url)
            if any(repo_id not in visible for repo_id in selected_ids):
                return _error("Selection could not be verified.", 403, retry_confirm_url)
            access = fields.get("access", ["read"])[0]
            if access not in ("read", "write"):
                return _error("Selection could not be verified.", retry_url=retry_confirm_url)
            app_jwt = build_app_jwt(private_key, app_id, now=int(time.time()))
            by_installation = {installation.id: installation for installation in installations}
            try:
                async with factory() as client:
                    details = [
                        await get_app_installation_details(
                            client, jwt=app_jwt, installation_id=installation_id
                        )
                        for installation_id in sorted(
                            {visible[repo_id].installation_id for repo_id in selected_ids}
                        )
                    ]
            except (httpx.HTTPError, ValueError):
                return transient_error("Couldn't reach GitHub", 502)
            if any(
                detail.account_id != by_installation[detail.installation_id].owner_id
                or detail.account_login.casefold()
                != by_installation[detail.installation_id].owner_login.casefold()
                or detail.suspended_at is not None
                for detail in details
            ):
                return _error("GitHub access could not be verified.", 403, retry_confirm_url)
            installation_writes = {
                detail.installation_id: installation_supports_write(detail.permissions)
                for detail in details
            }
            if any(
                not installation_writes.get(visible[repo_id].installation_id, False)
                for repo_id in selected_ids
            ):
                return _error(
                    "The GitHub App needs write access to connect these repos.",
                    403,
                    retry_confirm_url,
                )
            repos: list[github_connect.RepoConfirmation] = [
                _repo_confirmation(
                    visible[repo_id],
                    requested=access,
                    needs_write=bool(needed.get(repo_id)),
                    installation_writes=installation_writes,
                )
                for repo_id in selected_ids
            ]
            live = await live_agent(invitation)
            is_daimon_managed = (
                None if live is None else live.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            )
            if not invitation.operator_issued:
                if (
                    invitation.requester_platform_user_id is not None
                    and invitation.requester_platform_user_id != requester.platform_user_id
                ):
                    return _error("You can no longer manage this agent.", 403, retry_confirm_url)
                try:
                    still_admin = await _attest_native_admin(
                        requester=requester,
                        settings=settings,
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                    )
                except AdminAttestationUnavailable:
                    return transient_error(_ADMIN_RETRY, 503)
                if not still_admin:
                    return _error("You can no longer manage this agent.", 403, retry_confirm_url)
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
                            other_names=agent_pin_names(live.name, live.metadata)
                            if live is not None
                            else (),
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
                            await enqueue_reconciliation(
                                session,
                                installation_id=detail.installation_id,
                                delivery_id=f"connect:{uuid.uuid4()}",
                                event="connect",
                                deleted=False,
                                now=datetime.now(UTC),
                            )
                        await github_connect.activate_confirmed_agent(
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
            except github_connect.PinnedAgentConnectionError:
                await revoke_user_token(token)
                return _error(github_connect.PINNED_AGENT_MESSAGE)
            except github_access.SecondInstallationError as exc:
                await revoke_user_token(token)
                return _error(str(exc))
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
                await github_connect.successful_confirmation(session, state=state, cookie=cookie)
            response = _done_page(
                count=len(repos),
                platform=requester.platform,
                external_id=requester.external_id,
                requester_label=invitation.requester_label,
                same_person=requester_github_id == flow.github_user_id,
                agent_name=invitation.agent_name,
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
        usable_repos = [
            repo for installation in installations for repo in installation.repos if repo.usable
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
        if not usable_repos and config.app_id is not None and config.private_key is not None:
            try:
                async with factory() as client:
                    pending = await has_pending_installation_request(
                        client,
                        app_id=config.app_id,
                        private_key=config.private_key.get_secret_value(),
                        github_user_id=flow.github_user_id,
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return transient_error("Couldn't reach GitHub", 502)
        if pending.found:
            return _pending_page(
                html.escape(retry_confirm_url, quote=True),
                cancel_url,
                pending.account_login,
            )
        if not installations:
            return _install_page(install_url, cancel_url)
        if not usable_repos:
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
            platform=requester.platform,
            workspace=workspace or f"this {_place_label(requester.platform).lower()}",
            agent_name=invitation.agent_name,
            already_added=already_added,
            needed=needed,
            requested_repo=invitation.requested_repo,
            install_url=install_url,
        )

    return connect, callback, setup, confirm
