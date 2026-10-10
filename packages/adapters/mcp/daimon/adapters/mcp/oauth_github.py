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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode

import httpx
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.github_pages import github_page
from daimon.adapters.mcp.web_icons import icon, platform_mark
from daimon.core.agent_pins import agent_pin_names
from daimon.core.channel_admins import GroupMembers
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_app_auth import build_app_jwt, get_app_installation_details
from daimon.core.github_connect_cards import (
    ADD_REPOS_LABEL,
    ALREADY_ADDED,
    CLOSE_TAB,
    NEEDED,
    NEEDS_WRITE,
    added_line,
    audience_line,
    old_token_line,
    picker_title,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.github_panel import requester_manages_agent
from daimon.core.github_requester_access import list_github_pages
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access, github_app_installations, github_connect
from daimon.core.stores.accounts import get_account_with_tenant, has_external_accounts
from daimon.core.stores.domain import AccountIdentityRow, Role
from daimon.core.stores.github_access import list_agent_repos
from daimon.core.stores.github_request_actions import finish_confirmed_requests
from daimon.core.stores.security_audit import append_event
from pydantic import BaseModel, Field
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError
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
    missing_repos: tuple[tuple[str, bool], ...] = (),
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
        _still_uses_token(agent_name, missing_repos)
        if agent_name and update_pending and missing_repos
        else f"<p>An operator will finish switching {html.escape(agent_name)}.</p>"
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


def _still_uses_token(agent_name: str, missing: tuple[tuple[str, bool], ...]) -> str:
    return (
        f"<p>{html.escape(agent_name)} still uses its old GitHub token. "
        f"Add {html.escape(_missing_phrase(missing))} to finish switching.</p>"
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
            _still_uses_token(agent_name, missing_repos)
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


def _snapshot_signature(state: str, invitation_hash: str, snapshot: str, secret: str) -> str:
    message = json.dumps([state, invitation_hash, snapshot], separators=(",", ":")).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _saved_agent_page(
    name: str,
    grants: list[github_access.AgentRepo],
    missing: tuple[tuple[str, bool], ...] = (),
    pending: bool = False,
) -> Response:
    items = "".join(
        f"<li><strong>{html.escape(repo.full_name)}</strong> "
        f"<span>{'Read and write' if repo.ceiling_access == 'write' else 'Read only'}"
        f"{'</span><span>Working repo' if repo.is_working_repo else ''}"
        f"{'</span><span>Setup pending' if repo.staged else ''}"
        f"{'</span><span>Unavailable' if not repo.staged and repo.status != 'active' else ''}"
        "</span></li>"
        for repo in grants
    )
    body = (
        '<div class="gh-status-icon">'
        + icon("circle-check")
        + "</div>"
        + (
            f'<ul class="gh-saved-repos">{items}</ul>'
            if items
            else "<p>No repos on this agent.</p>"
        )
        + (
            f"<p>Still using the old GitHub key. Add "
            f"{html.escape(_missing_phrase(missing))} to finish switching.</p>"
            if missing
            else "<p>An operator will finish switching the old GitHub key.</p>"
            if pending
            else ""
        )
        + "<p>You can close this tab.</p>"
    )
    return github_page(title=f"{name}'s repos saved", body_html=body, heading_icon=True)


_EDITOR_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("github-connect-form");
  const list = document.getElementById("connected-repos");
  const dialog = document.getElementById("repo-picker");
  const options = [...dialog.querySelectorAll(".gh-picker-choice")];
  const search = document.getElementById("search-repos");
  const save = document.getElementById("save-repos");
  const stage = document.getElementById("stage-repos");
  const workingInput = document.getElementById("working-input");
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
    const trigger = row.querySelector(".gh-menu-trigger");
    if (trigger) trigger.setAttribute("aria-label", "More options for " + name);
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
      const access = isAdding ? staged.get(id) : row.dataset.access;
      const indicator = row.querySelector(".gh-access-indicator");
      indicator.classList.toggle("gh-access-write", access === "write");
      indicator.classList.toggle("gh-access-read", access !== "write");
      indicator.querySelector(".gh-access-label").textContent =
        access === "write" ? "Read and write" : "Read only";
      indicator.querySelector(".gh-access-eye").hidden = access === "write";
      indicator.querySelector(".gh-access-pencil").hidden = access !== "write";
      row.querySelector(".gh-working-badge").hidden = isRemoved || working !== id;
      row.querySelector(".gh-pending-label").textContent =
        isRemoved ? "Removing" : isAdding ? "Adding" : "";
      const removeButton = row.querySelector(".gh-remove-action");
      const undoButton = row.querySelector(".gh-undo-action");
      if (removeButton) removeButton.hidden = isRemoved;
      if (undoButton) undoButton.hidden = !isRemoved;
      const trigger = row.querySelector(".gh-menu-trigger");
      if (trigger) trigger.hidden = isRemoved;
      const action = row.querySelector(".gh-working-action");
      if (action) action.querySelector(".gh-working-action-label").textContent =
        working === id ? "Clear working repo" : "Use as working repo";
    }
    closeMenus();
    const added = staged.size;
    const removedCount = removed.size;
    const workingChanged = working !== initialWorking;
    const parts = [];
    if (added) parts.push(added + (added === 1 ? " addition" : " additions"));
    if (removedCount) parts.push(removedCount + (removedCount === 1 ? " removal" : " removals"));
    if (workingChanged) parts.push("Working repo changed");
    document.getElementById("selection-status").textContent =
      parts.length ? parts.join(", ") : "No changes";
    document.getElementById("repo-count").textContent =
      String(rows().length - removedCount);
    const empty = list.querySelector(".gh-editor-empty");
    if (empty) empty.hidden = rows().length > 0;
    document.getElementById("discard-repos").hidden = !parts.length;
    save.disabled = !parts.length;
    workingInput.value = workingExplicit ||
      (working !== initialWorking && !removed.has(initialWorking))
      ? (working || "clear") : "keep";
  }
  function resetPicker() {
    for (const option of options) {
      const id = option.dataset.repoId;
      option.querySelector(".gh-picker-toggle").checked = staged.has(id);
      option.querySelector(".gh-picker-access").value = staged.get(id) ||
        (option.dataset.requiredWrite === "true" ? "write" : "read");
      option.querySelector(".gh-choice-access").hidden = !staged.has(id);
    }
    search.value = "";
    for (const option of options) option.hidden = false;
    updatePicker();
  }
  function updatePicker() {
    const count = options.filter(o => o.querySelector(".gh-picker-toggle").checked).length;
    stage.disabled = count === 0 || count > 10;
    stage.textContent = count ? "Add " + count + (count === 1 ? " repo" : " repos") : "Add repos";
    document.getElementById("picker-count").textContent =
      count > 10 ? "Choose up to 10 repos." : "Changes apply when you save.";
  }
  document.getElementById("open-repo-picker").addEventListener("click", event => {
    opener = event.currentTarget;
    resetPicker();
    dialog.showModal();
    search.focus();
  });
  document.getElementById("close-repo-picker").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { resetPicker(); if (opener) opener.focus(); });
  dialog.addEventListener("change", event => {
    if (event.target.classList.contains("gh-picker-toggle")) {
      event.target.closest(".gh-picker-choice").querySelector(".gh-choice-access").hidden =
        !event.target.checked;
    }
    updatePicker();
  });
  search.addEventListener("input", () => {
    const query = search.value.trim().toLowerCase();
    for (const option of options) {
      option.hidden = !option.dataset.name.toLowerCase().includes(query);
    }
    document.getElementById("no-search-results").hidden = options.some(o => !o.hidden);
  });
  stage.addEventListener("click", () => {
    const selected = options.filter(o => o.querySelector(".gh-picker-toggle").checked);
    if (!selected.length || selected.length > 10) return;
    staged.clear();
    for (const option of selected) {
      const id = option.dataset.repoId;
      const access = option.querySelector(".gh-picker-access").value;
      staged.set(id, option.dataset.requiredWrite === "true" ? "write" : access);
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
      working = working === id ? null : id;
      workingExplicit = true;
      row.querySelector(".gh-menu-trigger").focus();
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
    missing_names: tuple[str, ...] = (),
    selection_error: str | None = None,
    clients_present: bool = False,
    pending_installation: _PendingInstallationRequest | None = None,
) -> Response:
    """Render one agent's grants and an optional staged add dialog."""
    esc = html.escape
    needed = needed or {}
    admin = {
        repo.id: repo for installation in installations for repo in installation.repos if repo.admin
    }
    current = {repo.repo_id for repo in grants}
    available = sorted(
        (repo for repo_id, repo in admin.items() if repo_id not in current),
        key=lambda repo: repo.full_name.casefold(),
    )
    repairable = [
        repo for repo in grants if repo.repo_id in admin and (repo.staged or repo.repo_id in needed)
    ]
    working = next((repo for repo in grants if repo.is_working_repo), None)
    may_change_working = working is None or working.repo_id in admin
    place = "Server" if platform == "discord" else "Workspace"
    options: list[tuple[int, str, bool, bool]] = [
        (repo.repo_id, repo.full_name, True, bool(needed.get(repo.repo_id))) for repo in repairable
    ] + [(repo.id, repo.full_name, False, bool(needed.get(repo.id))) for repo in available]
    parts = [
        '<form id="github-connect-form" class="gh-editor" method="post" '
        f'data-working="{working.repo_id if working else ""}" '
        f'data-may-change-working="{str(may_change_working).lower()}" '
        f'action="{esc(root, quote=True)}/oauth/github/confirm">',
        f'<input type="hidden" name="state" value="{esc(state, quote=True)}">',
        f'<input type="hidden" name="invitation" value="{esc(invitation_hash, quote=True)}">',
        f'<input type="hidden" name="receipt" '
        f'value="{_receipt_signature(state, invitation_hash, secret)}">',
        f'<input type="hidden" name="snapshot" value="{esc(snapshot, quote=True)}">',
        f'<input type="hidden" name="snapshot_sig" '
        f'value="{_snapshot_signature(state, invitation_hash, snapshot, secret)}">',
        '<input type="hidden" name="access" value="read">',
        '<input id="working-input" type="hidden" name="working" value="keep" disabled>',
        f'<div class="gh-context">{platform_mark(platform)}'
        f"<span>{place}: {esc(workspace)}</span></div>",
        '<div class="gh-editor-toolbar"><h2>Connected repos '
        f'<span id="repo-count" class="gh-repo-count">{len(grants)}</span></h2>',
        '<button id="open-repo-picker" class="gh-add-button gh-js" type="button">'
        f"{icon('plus')}Add repos</button></div>",
    ]
    if pending_installation is not None and pending_installation.found:
        organization = pending_installation.account_login
        parts.append(
            '<p class="gh-editor-notice">Waiting for GitHub approval'
            + (f" from {esc(organization)}" if organization else "")
            + '. <a href="'
            + f"{esc(root, quote=True)}/oauth/github/confirm?"
            + f'{urlencode({"state": state})}">Check again</a></p>'
        )
    if missing_names:
        parts.append(
            '<p class="gh-editor-notice">Still needed to finish setup: '
            + ", ".join(esc(name) for name in missing_names)
            + ".</p>"
        )
    if clients_present:
        parts.append('<p class="gh-editor-notice">Clients can see what this agent shares.</p>')
    if selection_error:
        parts.append(f'<p class="gh-inline-error" role="alert">{esc(selection_error)}</p>')
    parts.append('<div id="connected-repos" class="gh-connected-list">')
    if not grants:
        parts.append('<p class="gh-editor-empty">No repos connected yet.</p>')
    for repo in grants:
        repo_name = esc(repo.full_name)
        editable = repo.repo_id in admin
        parts.append(
            f'<article class="gh-agent-row" data-repo-id="{repo.repo_id}" '
            f'data-name="{esc(repo.full_name, quote=True)}" '
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
                    '<button class="gh-row-action gh-menu-trigger" type="button" '
                    'data-action="menu" aria-haspopup="menu" aria-expanded="false" '
                    f'aria-label="More options for {esc(repo.full_name, quote=True)}">'
                    f"{icon('ellipsis')}</button>"
                )
        parts.append('</div></div><div class="gh-row-meta">')
        parts.append(
            f'<span class="gh-access-indicator gh-access-{repo.ceiling_access}">'
            f'<span class="gh-access-eye"{" hidden" if repo.ceiling_access == "write" else ""}>'
            f"{icon('eye')}</span>"
            f'<span class="gh-access-pencil"{" hidden" if repo.ceiling_access != "write" else ""}>'
            f"{icon('pencil')}</span>"
            '<span class="gh-access-label">'
            + ("Read and write" if repo.ceiling_access == "write" else "Read only")
            + "</span></span>"
        )
        parts.append(
            f'<span class="gh-working-badge"{"" if repo.is_working_repo else " hidden"}>'
            f"{icon('folder')}Working repo</span>"
            '<span class="gh-pending-label" aria-live="polite"></span>'
        )
        if repo.staged:
            parts.append('<span class="gh-repo-state">Setup pending</span>')
        elif repo.status != "active":
            parts.append('<span class="gh-repo-state">Unavailable</span>')
        parts.append("</div>")
        if not editable:
            parts.append('<p class="gh-repo-locked">GitHub admin access required to edit.</p>')
        if repo.repo_id in needed and needed[repo.repo_id]:
            parts.append('<p class="gh-repo-state">Needs read and write to finish setup.</p>')
        if editable:
            parts.append(f'<input type="hidden" name="remove" value="{repo.repo_id}" disabled>')
            if repo in repairable:
                parts.append(
                    f'<input type="hidden" name="repo" value="{repo.repo_id}" disabled>'
                    f'<input class="gh-scoped-access" type="hidden" '
                    f'name="access_{repo.repo_id}" value="read" disabled>'
                )
            if may_change_working and repo.status == "active":
                parts.append(
                    '<div class="gh-row-menu" role="menu" hidden>'
                    '<button class="gh-working-action" type="button" role="menuitem" '
                    f'data-action="working">{icon("folder")}'
                    '<span class="gh-working-action-label">'
                    + ("Clear working repo" if repo.is_working_repo else "Use as working repo")
                    + "</span></button></div>"
                )
        parts.append("</article>")
    parts.append("</div>")
    if not grants or not options:
        parts.append(
            '<p class="gh-editor-install"><a href="'
            + esc(install_url, quote=True)
            + '">Choose repos on GitHub</a></p>'
        )
    parts.append(
        '<details class="gh-access-help"><summary>'
        f"{icon('info')}How access works</summary>"
        "<p>The working repo opens in the agent's workspace. Access also depends on "
        "the person asking and their GitHub permissions.</p></details>"
    )
    parts.append(
        '<dialog id="repo-picker" class="gh-repo-dialog" aria-labelledby="repo-picker-title">'
        '<div class="gh-dialog-head"><h2 id="repo-picker-title">Add repos</h2>'
        '<button id="close-repo-picker" class="gh-dialog-close" type="button" '
        f'aria-label="Close add repos">{icon("x")}</button></div>'
        f'<div class="gh-dialog-search">{icon("search")}'
        '<input id="search-repos" class="gh-search" type="search" '
        'aria-label="Search repos" placeholder="Search repos" autocomplete="off"></div>'
        '<div class="gh-dialog-choices">'
    )
    if not options:
        parts.append("<p>No repos available to add.</p>")
    for repo_id, name, repair, required_write in options:
        parts.append(
            f'<div class="gh-picker-choice" data-repo-id="{repo_id}" '
            f'data-name="{esc(name, quote=True)}" '
            f'data-required-write="{str(required_write).lower()}">'
            '<label class="gh-picker-label"><input class="gh-picker-toggle" type="checkbox">'
            f"<span>{esc(name)}"
            + ("<small>Finish setup</small>" if repair else "")
            + '</span></label><div class="gh-choice-access" hidden>'
            f'<label for="picker-access-{repo_id}">Access</label>'
            f'<select id="picker-access-{repo_id}" class="gh-picker-access" '
            f'aria-label="Access for {esc(name, quote=True)}">'
            + ("" if required_write else '<option value="read">Read only</option>')
            + '<option value="write">Read and write</option></select>'
            + ("<small>Required to finish setup</small>" if required_write else "")
            + "</div></div>"
        )
    parts.append(
        '<p id="no-search-results" hidden>No matching repos.</p></div>'
        '<p class="gh-dialog-link"><a href="'
        + esc(install_url, quote=True)
        + '">Missing a repo? Choose repos on GitHub</a></p>'
        '<div class="gh-dialog-footer"><span id="picker-count">Changes apply when you save.</span>'
        '<button id="stage-repos" class="gh-primary" type="button" disabled>Add repos</button>'
        "</div></dialog>"
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
            '<button class="gh-row-action gh-menu-trigger" type="button" data-action="menu" '
            f'aria-haspopup="menu" aria-expanded="false">{icon("ellipsis")}</button>'
            if may_change_working
            else ""
        )
        + '</div></div><div class="gh-row-meta">'
        f'<span class="gh-access-indicator gh-access-read">'
        f'<span class="gh-access-eye">{icon("eye")}</span>'
        f'<span class="gh-access-pencil" hidden>{icon("pencil")}</span>'
        '<span class="gh-access-label">Read only</span></span>'
        f'<span class="gh-working-badge" hidden>{icon("folder")}Working repo</span>'
        '<span class="gh-pending-label" aria-live="polite"></span></div>'
        '<input type="hidden" name="repo" disabled>'
        '<input class="gh-scoped-access" type="hidden" disabled>'
        + (
            '<div class="gh-row-menu" role="menu" hidden>'
            '<button class="gh-working-action" type="button" role="menuitem" '
            f'data-action="working">{icon("folder")}'
            '<span class="gh-working-action-label">Use as working repo</span></button></div>'
            if may_change_working
            else ""
        )
        + "</article></template>"
    )
    # Browsers without scripting still get a complete native form.
    parts.append('<noscript><section class="gh-fallback"><h2>Edit repos</h2>')
    for repo in grants:
        if repo.repo_id not in admin:
            continue
        parts.append(
            f'<label><input type="checkbox" name="remove" value="{repo.repo_id}">'
            f"Remove {esc(repo.full_name)}</label>"
        )
        if repo in repairable:
            parts.append(
                f'<label><input type="checkbox" name="repo" value="{repo.repo_id}">'
                f"Finish setup for {esc(repo.full_name)}</label>"
                f'<select name="access_{repo.repo_id}" '
                f'aria-label="Access for {esc(repo.full_name, quote=True)}">'
                + ("" if needed.get(repo.repo_id) else '<option value="read">Read only</option>')
                + '<option value="write">Read and write</option></select>'
            )
    for repo in available:
        parts.append(
            f'<label><input type="checkbox" name="repo" value="{repo.id}">'
            f"Add {esc(repo.full_name)}</label>"
            f'<select name="access_{repo.id}" '
            f'aria-label="Access for {esc(repo.full_name, quote=True)}">'
            + ("" if needed.get(repo.id) else '<option value="read">Read only</option>')
            + '<option value="write">Read and write</option></select>'
        )
    parts.append(
        '<label>Working repo <select name="working"><option value="keep">Keep current</option>'
    )
    if may_change_working:
        parts.append('<option value="clear">None</option>')
        for repo in grants:
            if repo.repo_id in admin and repo.status == "active":
                parts.append(f'<option value="{repo.repo_id}">{esc(repo.full_name)}</option>')
        for repo in available:
            parts.append(f'<option value="{repo.id}">{esc(repo.full_name)} (add above)</option>')
    parts.append("</select></label></section></noscript>")
    parts.append(
        '<div class="gh-editor-footer"><div class="gh-editor-footer-inner">'
        '<div><strong id="selection-status" aria-live="polite">No changes</strong>'
        "<small>Changes apply when you save.</small></div>"
        '<div class="gh-editor-footer-actions">'
        '<button id="discard-repos" class="gh-editor-discard gh-js" type="button" hidden>'
        "Discard</button>"
        '<button id="save-repos" class="gh-primary" type="submit">Save changes</button>'
        f'<a class="gh-editor-cancel" href="{esc(cancel_url, quote=True)}">Cancel</a>'
        "</div></div></div></form>"
    )
    parts.append(_EDITOR_SCRIPT)
    return github_page(title=f"{agent_name}'s repos", body_html="".join(parts), kind="picker")


#: A safety net on one Add action; a person rarely needs more at once.
MAX_REPOS_PER_ADD = 10

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
    // No select-all: one stray click ticked 75 repos on 2026-10-10.
    bulk.hidden = true;
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
    needed: Mapping[int, bool] | None = None,
) -> Response:
    """Render the same picker used by the live route and screenshot capture.

    Repos in `already_added` show ticked and greyed; only new ticks are sent.
    Repos in `needed` (repo id: needs write) are what the agent still needs
    before it can drop its old GitHub token: ticked, and still changeable.
    """
    needed = needed or {}
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
            note = (
                ALREADY_ADDED
                if added
                else (NEEDS_WRITE if needed[repo.id] else NEEDED)
                if repo.id in needed
                else None
            )
            parts.append(
                f'<label class="gh-choice repo-choice{" is-added" if added else ""}">'
                f'<input type="checkbox" name="repo" value="{repo.id}"'
                + (" checked disabled" if added else " checked" if repo.id in needed else "")
                + ">"
                f'<span><span class="gh-repo-prefix">{html.escape(prefix)}/</span>'
                f'<span class="gh-repo-name">{html.escape(name)}</span>'
                + (f'<span class="gh-added">{note}</span>' if note else "")
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
            return (
                requester.tenant_id == invitation.tenant_id
                and requester.role == Role.ADMIN
                and not requester.is_external
            )
        if deployment_default is None:
            return False
        metadata = live.metadata if live is not None else {}
        members = (
            group_members(requester.platform, requester.external_id)
            if group_members is not None
            else None
        )
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
            members=members,
            other_names=agent_pin_names(live.name, metadata) if live is not None else (),
        )

    async def _missing(
        session: AsyncSession, invitation: github_connect.Invitation
    ) -> tuple[tuple[str, bool], ...]:
        """The working and skill repos the agent still needs before it can drop its old key."""
        if invitation.agent_id is None:
            return ()
        if (
            invitation.activation_status != "update_pending"
            and not await github_connect.has_saved_github_state(
                session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
            )
        ):
            return ()
        return tuple(
            (missing.full_name, missing.needs_write)
            for missing in await github_connect.missing_required_repos(
                session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
            )
        )

    async def _key_pending(session: AsyncSession, invitation: github_connect.Invitation) -> bool:
        if invitation.agent_id is None:
            return False
        if invitation.activation_status == "update_pending":
            return True
        return await github_access.get_agent_mode(
            session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
        ) == "legacy" and await github_connect.has_saved_github_state(
            session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
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
                if (
                    requester is None
                    or requester.is_external
                    or not await may_manage_agent(session, invitation, requester, live)
                ):
                    return _error("You can no longer manage this agent.", 403)
            missing = await _missing(session, invitation)
            pending = await _key_pending(session, invitation)
            agent_grants = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
        if invitation.agent_id is not None:
            return _saved_agent_page(
                invitation.agent_name or "Agent", agent_grants, missing, pending
            )
        back = _back_to_chat(requester.platform, requester.external_id) if requester else ""
        return _already_connected_page(
            invitation.connected_repo_count,
            back,
            agent_name=invitation.agent_name,
            update_pending=invitation.activation_status == "update_pending",
            missing_repos=missing,
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
            agent_repos = (
                await list_agent_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            )
            # A staged or inactive repo, or one the agent still needs more of,
            # can be ticked again; only a repo it fully has is settled.
            missing_access: dict[str, bool] = {}
            missing_names: list[str] = []
            for missing in (
                await github_connect.missing_required_repos(
                    session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id
                )
                if invitation.agent_id is not None
                else []
            ):
                # Needing write for any use of the repo means it needs write.
                key = missing.full_name.casefold()
                missing_access[key] = missing_access.get(key, False) or missing.needs_write
                if missing.full_name not in missing_names:
                    missing_names.append(missing.full_name)
            already_added = frozenset(
                repo.repo_id
                for repo in agent_repos
                if not repo.staged
                and repo.status == "active"
                and repo.full_name.casefold() not in missing_access
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
        needed = {
            repo.id: missing_access[repo.full_name.casefold()]
            for installation in installations
            for repo in installation.repos
            if repo.admin and repo.full_name.casefold() in missing_access
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
                if (
                    current_requester is None
                    or current_requester.is_external
                    or not await may_manage_agent(session, invitation, current_requester, live)
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
                if not any(repo.admin for item in installations for repo in item.repos):
                    try:
                        async with factory() as client:
                            pending_installation = await has_pending_installation_request(
                                client,
                                app_id=app_id,
                                private_key=private_key,
                                github_user_id=flow.github_user_id,
                            )
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        return _error("Couldn't reach GitHub", 502, retry_confirm_url)
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
                    missing_names=tuple(missing_names),
                    clients_present=clients_present,
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
                or len(selected_ids) > MAX_REPOS_PER_ADD
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
                if repo.admin
            }
            old = {repo.repo_id: repo for repo in grants}
            repairable = {repo.repo_id for repo in grants if repo.staged or repo.repo_id in needed}
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
                return _error("GitHub admin access could not be verified.", 403, retry_confirm_url)
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
                    missing_names=tuple(missing_names),
                    clients_present=clients_present,
                    selection_error="Choose a change to save.",
                )
            if posted_snapshot != snapshot:
                return _error(
                    "The agent's repos changed. Reload and try again.", 409, retry_confirm_url
                )
            repos = [
                github_connect.RepoConfirmation(
                    repo_id=visible[repo_id].id,
                    owner_id=visible[repo_id].owner_id,
                    installation_id=visible[repo_id].installation_id,
                    full_name=visible[repo_id].full_name,
                    max_access=cast(
                        Literal["read", "write"],
                        "write" if needed.get(repo_id) else selected_access[repo_id],
                    ),
                )
                for repo_id in selected_ids
            ]
            app_jwt = build_app_jwt(private_key, app_id, now=int(time.time()))
            by_installation = {install.id: install for install in installations}
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
            activation: github_connect.ConfirmedActivation | None = None
            try:
                async with sessionmaker.begin() as session:
                    await session.execute(sql_text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    transaction_requester = await get_account_with_tenant(
                        session, account_id=invitation.requester_account_id
                    )
                    manages = (
                        transaction_requester is not None
                        and not transaction_requester.is_external
                        and await may_manage_agent(session, invitation, transaction_requester, live)
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
                        expected_agent_snapshot=posted_snapshot,
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
                        for repo_id in removed_ids:
                            await github_access.remove_agent_repo(
                                session,
                                tenant_id=invitation.tenant_id,
                                agent_id=invitation.agent_id,
                                repo_id=repo_id,
                                account_id=invitation.requester_account_id,
                            )
                        if repos:
                            activation = await github_connect.activate_confirmed_agent(
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
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) in ("40001", "40P01"):
                    return _error(
                        "The agent's repos changed. Reload and try again.", 409, retry_confirm_url
                    )
                raise
            except github_connect.ClientAgentConnectionError:
                return _error(github_connect.CLIENT_AGENT_MESSAGE)
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
            if len(selected_ids) > MAX_REPOS_PER_ADD:
                selection_error: str | None = f"Pick up to {MAX_REPOS_PER_ADD} repos at a time"
            elif not selected_ids:
                selection_error = "Select at least one repo"
            else:
                selection_error = None
            if selection_error is not None:
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
                    selection_error=selection_error,
                    already_added=already_added,
                    needed=needed,
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
                # A repo the agent needs write on is added with write; never lower.
                if needed.get(repo_id):
                    access = "write"
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
            live = await live_agent(invitation)
            is_daimon_managed = (
                None if live is None else live.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            )
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
            needed=needed,
        )

    return connect, callback, setup, confirm
