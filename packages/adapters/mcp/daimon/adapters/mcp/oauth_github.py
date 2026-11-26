"""Server-rendered pages for the GitHub App repository connection flow.

The flow itself is `daimon.core.github_connect_flow`; this module maps its
routes to it and renders the views it returns as HTML.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.github_pages import github_page
from daimon.adapters.mcp.oauth_github_follow import follow_offers_html, following_note_html
from daimon.adapters.mcp.web_icons import icon, platform_mark
from daimon.core.config import Settings
from daimon.core.github_connect_admin import teams_admin_owner_ids
from daimon.core.github_connect_cards import (
    NEEDED,
    NEEDS_WRITE,
    audience_line,
)
from daimon.core.github_connect_flow import ClientFactory, GitHubConnectFlow
from daimon.core.github_connect_views import (
    FLOW_COOKIE,
    MAX_CONFIRM_BODY_BYTES,
    SELECTION_FIELDS,
    STATE_COOKIE,
    AgentEditorView,
    AlreadyConnectedView,
    CancelledView,
    ChatReturn,
    ConnectErrorView,
    ConnectInstallation,
    ConnectOutcome,
    ConnectRedirect,
    ConnectTransientErrorView,
    ConnectView,
    DoneView,
    ExpiredView,
    InstallView,
    NoReposView,
    PendingView,
    SavedAgentView,
    WorkspacePickerView,
    read_capped,
    selection_from_form,
    selection_too_large,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import github_access
from daimon.core.web_links import WebLinks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

RouteHandler = Callable[[Request], Awaitable[Response]]
_RETRY_FIELD_SCOPED_ACCESS = re.compile(r"access_\d+")


def _retry_hidden_inputs(fields: Mapping[str, Sequence[str]]) -> str:
    """Hidden inputs that re-post the editor's own fields, repeating multi-value ones."""
    return "".join(
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(value, quote=True)}">'
        for name, values in fields.items()
        if name in SELECTION_FIELDS or _RETRY_FIELD_SCOPED_ACCESS.fullmatch(name)
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
            f'<p class="gh-action-line">{html.escape(action)}</p>' + retry
        ),
        status=status,
        error=True,
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


def _agent_only_line(count: int, agent_name: str) -> str:
    """Say a connected repo belongs to this agent alone and stays until removed."""
    if count == 1:
        return f"1 repo connected to {html.escape(agent_name)} only. It stays until removed."
    return f"{count} repos connected to {html.escape(agent_name)} only. They stay until removed."


def _already_connected_page(
    count: int | None,
    back: str = "",
    *,
    agent_name: str | None = None,
) -> Response:
    status = (
        _agent_only_line(count, agent_name)
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
            + f'<p class="gh-status-line">{_agent_only_line(count, agent_name)}</p>'
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
            f'<a class="gh-primary" href="{html.escape(install_url, quote=True)}">'
            f"{icon('github')}Continue to GitHub</a>"
            f'<a class="gh-link" href="{html.escape(cancel_url, quote=True)}">Cancel</a></div>'
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
            f'<a class="gh-primary" href="{html.escape(check_url, quote=True)}">'
            f"Check again{icon('chevron-right')}</a>"
            f'<a class="gh-link" href="{html.escape(cancel_url, quote=True)}">Cancel</a></div>'
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


def _owner_avatar(item: ConnectInstallation | None) -> str:
    is_person = item is not None and item.owner_type.lower() == "user"
    if (
        item is not None
        and item.avatar_url
        and item.avatar_url.startswith("https://avatars.githubusercontent.com/")
    ):
        shape = " gh-owner-avatar--person" if is_person else ""
        return (
            f'<img class="gh-owner-avatar{shape}" '
            f'src="{html.escape(item.avatar_url, quote=True)}" alt="">'
        )
    shape = " gh-owner-avatar--person" if is_person else ""
    fallback_icon = icon("user" if is_person else "building-2")
    return f'<span class="gh-owner-avatar-fallback{shape}">{fallback_icon}</span>'


def _saved_agent_page(
    name: str,
    grants: list[github_access.AgentRepo],
    visible_ids: set[int] | None = None,
    back_url: str | None = "https://discord.com/app",
    installations: Sequence[ConnectInstallation] = (),
    following: bool = False,
) -> Response:
    owner_installations = {item.owner_login.casefold(): item for item in installations}
    groups: dict[str, list[github_access.AgentRepo]] = {}
    for repo in grants:
        groups.setdefault(repo.full_name.partition("/")[0], []).append(repo)
    ordered = sorted(
        groups.items(),
        key=lambda item: (not any(repo.is_working_repo for repo in item[1]), item[0].casefold()),
    )
    body = (
        '<div class="gh-status-icon">'
        + icon("circle-check")
        + "</div>"
        + f'<p class="gh-status-line">{_agent_only_line(len(grants), name)}</p>'
        + (following_note_html(agent_name=name) if following else "")
        + '<div class="gh-saved-groups">'
        + "".join(
            f'<section class="gh-owner-group"><h2 class="gh-owner-heading">'
            f"{_owner_avatar(owner_installations.get(owner.casefold()))}"
            f"{html.escape(owner)}</h2>"
            + "".join(
                '<div class="gh-saved-row">'
                + '<span class="gh-row-icon">'
                + icon("book-marked" if repo.repo_id in (visible_ids or set()) else "lock")
                + "</span>"
                + '<span class="gh-saved-name">'
                + (
                    html.escape(repo.full_name.partition("/")[2])
                    if repo.repo_id in (visible_ids or set())
                    else "Hidden repo"
                )
                + "</span>"
                + (
                    f'<span class="gh-working-badge">{icon("pin")}Working repo</span>'
                    if repo.is_working_repo
                    else ""
                )
                + "</div>"
                for repo in repos
            )
            + "</section>"
            for owner, repos in ordered
        )
        + "</div>"
        + (
            '<a class="gh-primary gh-back-discord" href="'
            + html.escape(back_url, quote=True)
            + '">Back to Discord</a>'
            if back_url
            else ""
        )
        + '<p class="gh-action-line">You can close this tab.</p>'
    )
    return github_page(title="Repos saved", body_html=body, kind="saved")


_EDITOR_SCRIPT = """
<script>
(() => {
  const form = document.getElementById("github-connect-form");
  const list = document.getElementById("connected-repos");
  const dialog = document.getElementById("repo-picker");
  const options = [...dialog.querySelectorAll(".gh-picker-choice:not(.gh-picker-connected)")];
  const connected = [...dialog.querySelectorAll(".gh-picker-connected")];
  const ownerChoice = document.getElementById("repo-owner-choice");
  const ownerButtons = [...ownerChoice.querySelectorAll(".gh-owner-button[data-owner]")];
  const readyOwners = () => ownerButtons.filter(button => !button.disabled);
  const ownerSheet = document.getElementById("repo-owner-sheet");
  const backOwners = document.getElementById("back-repo-owners");
  const ownerTitle = document.getElementById("repo-picker-title");
  let activeOwner = null;
  const pickerDraft = new Set(options.filter(option =>
    option.querySelector(".gh-picker-toggle").checked
  ).map(option => option.dataset.repoId));
  const search = document.getElementById("search-repos");
  const save = document.getElementById("save-repos");
  const cancel = document.getElementById("cancel-editor");
  const saveHelp = document.getElementById("save-help");
  const stage = document.getElementById("stage-repos");
  const selectShown = document.getElementById("select-shown");
  const showSelected = document.getElementById("show-selected-repos");
  const clearSelection = document.getElementById("clear-selection");
  let selectedOnly = false;
  const workingInput = document.getElementById("working-input");
  const workingDialog = document.getElementById("working-picker");
  const workingSelect = document.getElementById("working-choice");
  const workingCard = document.getElementById("working-card");
  const workingCardName = document.getElementById("working-card-name");
  const changeWorking = document.getElementById("change-working");
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
  function makeAddedRow(id, name, installation) {
    const row = document.getElementById("added-row-template")
      .content.firstElementChild.cloneNode(true);
    row.dataset.repoId = id;
    row.dataset.name = name;
    row.dataset.added = "true";
    row.dataset.installation = installation || "";
    row.dataset.workingEligible = "true";
    row.querySelector(".gh-row-name").textContent = name.split("/").slice(1).join("/");
    row.querySelector(".gh-remove-action").setAttribute("aria-label", "Remove " + name);
    const repoInput = row.querySelector('input[name="repo"]');
    repoInput.value = id;
    const owner = name.split("/")[0];
    const group = [...list.querySelectorAll(".gh-owner-group")]
      .find(group => group.dataset.owner.toLowerCase() === owner.toLowerCase());
    if (group) { group.querySelector(".gh-owner-rows").appendChild(row); group.hidden = false; }
    return row;
  }
  const followOffers = [...form.querySelectorAll(".gh-follow-org")];
  // The offer shows once the agent would hold every repo of that one org.
  function renderFollow() {
    const live = rows().filter(row => !removed.has(row.dataset.repoId));
    const heldIds = new Set(live.map(row => row.dataset.repoId));
    let on = false;
    for (const offer of followOffers) {
      const owner = offer.dataset.owner.toLowerCase();
      const ready = live.length > 0 &&
        live.every(row => (row.dataset.name || "").split("/")[0].toLowerCase() === owner) &&
        offer.dataset.ids.split(",").every(id => heldIds.has(id));
      const box = offer.querySelector("input");
      offer.hidden = !ready;
      box.disabled = !ready;
      if (!ready) box.checked = false;
      on = on || box.checked;
    }
    return on;
  }
  form.addEventListener("change", event => {
    if (event.target.name === "follow_org") render();
  });
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
      const pending = row.querySelector(".gh-pending-label");
      pending.textContent = isRemoved ? "− To remove" : isAdding ? "+ To add" : "";
      pending.hidden = !isRemoved && !isAdding;
      const removeButton = row.querySelector(".gh-remove-action");
      const undoButton = row.querySelector(".gh-undo-action");
      if (removeButton) removeButton.hidden = isRemoved;
      if (undoButton) undoButton.hidden = !isRemoved;
    }
    for (const group of list.querySelectorAll(".gh-owner-group")) {
      group.hidden = !group.querySelector(".gh-agent-row");
    }
    workingCard.hidden = rows().length === 0;
    const chosenRow = rowFor(working);
    workingCardName.replaceChildren();
    if (chosenRow?.dataset.name) {
      const [owner, ...repo] = chosenRow.dataset.name.split("/");
      const ownerName = document.createElement("span");
      ownerName.className = "gh-working-owner";
      ownerName.textContent = owner + "/";
      const repoName = document.createElement("span");
      repoName.className = "gh-working-repo";
      repoName.textContent = repo.join("/");
      workingCardName.append(ownerName, repoName);
    } else {
      workingCardName.textContent = chosenRow ? "Hidden repo" : "None chosen";
    }
    changeWorking.textContent = chosenRow ? "Change" : "Choose";
    changeWorking.disabled = !rows().some(row =>
      !removed.has(row.dataset.repoId) && row.dataset.workingEligible === "true");
    const workingOwner = chosenRow?.closest(".gh-owner-group")?.dataset.owner;
    const groups = [...list.querySelectorAll(".gh-owner-group")];
    groups.sort((a, b) => {
      const rank = group => group.dataset.owner === workingOwner ? 0 :
        group.dataset.person === "true" ? 1 : 2;
      return rank(a) - rank(b) ||
        a.dataset.owner.localeCompare(b.dataset.owner, undefined, {sensitivity: "base"});
    });
    for (const group of groups) list.appendChild(group);
    closeMenus();
    const added = staged.size;
    const removedCount = removed.size;
    const workingChanged = working !== initialWorking;
    const parts = [];
    if (added) parts.push("add " + added + " " + plural(added));
    if (removedCount) parts.push("remove " + removedCount + " " + plural(removedCount));
    if (workingChanged) parts.push("change working repo");
    if (renderFollow()) parts.push("follow the org");
    document.getElementById("selection-status").textContent =
      parts.length ? "Not saved yet" : "No changes to save";
    document.querySelector(".gh-editor-footer").hidden = !parts.length;
    document.getElementById("dock-add").textContent = added ? "+ " + added + " to add" : "";
    document.getElementById("dock-add").hidden = !added;
    document.getElementById("dock-remove").textContent = removedCount ?
      "− " + removedCount + " to remove" : "";
    document.getElementById("dock-remove").hidden = !removedCount;
    document.querySelector("#dock-working span").textContent = workingChanged ?
      "New working repo: " + (chosenRow?.dataset.name?.split("/").pop() || "None") : "";
    document.getElementById("dock-working").hidden = !workingChanged;
    const empty = list.querySelector(".gh-editor-empty");
    if (empty) empty.hidden = rows().length > 0;
    document.getElementById("discard-repos").hidden = !parts.length;
    cancel.hidden = !!parts.length;
    save.hidden = !parts.length;
    saveHelp.hidden = !parts.length;
    // The server enforces the same limit by App installation; counting the same ids
    // keeps the ticks instead of a dead end. The owner name is only a fallback.
    const accounts = new Set(rows().filter(row => !removed.has(row.dataset.repoId))
      .map(row => row.dataset.installation ? "i:" + row.dataset.installation :
        "o:" + (row.closest(".gh-owner-group")?.dataset.owner || "").toLowerCase()));
    const limit = Number(form.dataset.ownerLimit);
    const over = accounts.size - limit;
    const ownerLimit = document.getElementById("owner-limit");
    ownerLimit.hidden = !(over > 0 && parts.length);
    const limitText = over > 0 ? form.dataset.agent +
      " can use repos from up to " + limit + " GitHub accounts or orgs. These changes use " +
      accounts.size + ". Remove the repos from " + over + " " +
      (over === 1 ? "account" : "accounts") + " to save." : "";
    const limitSpan = ownerLimit.querySelector("span");
    if (limitSpan.textContent !== limitText) limitSpan.textContent = limitText;
    save.disabled = !parts.length || over > 0;
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
      checkbox(option).checked = pickerDraft.has(id) || staged.has(id);
    }
    search.value = "";
    selectedOnly = false;
    for (const option of options) option.hidden = activeOwner !== option.dataset.owner;
    for (const option of connected) option.hidden = activeOwner !== option.dataset.owner;
    for (const note of dialog.querySelectorAll(".gh-owner-truncated")) {
      note.hidden = activeOwner !== note.dataset.owner;
    }
    showSelected.setAttribute("aria-pressed", "false");
    updatePicker();
  }
  function updatePicker() {
    if (selectedOnly && !options.some(option => checkbox(option).checked)) {
      selectedOnly = false;
      showSelected.setAttribute("aria-pressed", "false");
    }
    ownerTitle.textContent = selectedOnly ? "Selected repos" :
      activeOwner ? "Add " + activeOwner + " repos" : "Add repos";
    const query = search.value.trim().toLowerCase();
    for (const option of options) {
      option.hidden = selectedOnly ? !checkbox(option).checked :
        option.dataset.owner !== activeOwner || !option.dataset.name.toLowerCase().includes(query);
    }
    for (const option of connected) option.hidden = selectedOnly ||
      option.dataset.owner !== activeOwner || !option.dataset.name.toLowerCase().includes(query);
    for (const heading of dialog.querySelectorAll(".gh-selected-owner")) heading.remove();
    if (selectedOnly) {
      let previousOwner = null;
      for (const option of options.filter(option => !option.hidden)) {
        if (option.dataset.owner !== previousOwner) {
          const heading = document.createElement("h3");
          heading.className = "gh-selected-owner";
          const ownerButton = ownerButtons.find(button =>
            button.dataset.owner === option.dataset.owner);
          heading.innerHTML = (ownerButton?.querySelector(
            ".gh-owner-avatar, .gh-owner-avatar-fallback")?.outerHTML || "") +
            '<span></span>';
          heading.querySelector("span:last-child").textContent = option.dataset.owner;
          option.before(heading);
          previousOwner = option.dataset.owner;
        }
      }
    }
    const count = options.filter(o => checkbox(o).checked).length;
    const shown = options.filter(o => !o.hidden).length;
    const noResults = document.getElementById("no-search-results");
    const ownerRepos = options.concat(connected).filter(o => o.dataset.owner === activeOwner);
    if (activeOwner !== null && !selectedOnly && ownerRepos.length === 0) {
      noResults.textContent = "No repos to add from " + activeOwner + ".";
      noResults.hidden = false;
    } else {
      noResults.textContent = "No matching repos.";
      noResults.hidden = !query || shown > 0 || connected.some(o => !o.hidden);
    }
    stage.disabled = count === 0 && staged.size === 0;
    stage.textContent = "Add " + count + " " + plural(count);
    document.getElementById("picker-count").textContent = count + " selected";
    document.getElementById("owner-picker-count").textContent = count + " selected";
    document.getElementById("owner-stage-repos").disabled = count === 0;
    document.getElementById("owner-stage-repos").textContent = stage.textContent;
    ownerChoice.querySelector(".gh-dialog-footer").hidden = count === 0;
    for (const button of ownerButtons) {
      const n = options.filter(o => o.dataset.owner === button.dataset.owner &&
        checkbox(o).checked).length;
      button.querySelector(".gh-owner-selected").textContent = n ? n + " selected" : "";
    }
    selectShown.textContent = "Select all " + shown;
    document.getElementById("shown-count").textContent = selectedOnly ?
      "Showing " + shown + " selected" : "Showing " + shown + " of " +
      options.filter(o => o.dataset.owner === activeOwner).length;
    selectShown.hidden = selectedOnly;
    selectShown.disabled = shown === 0;
    clearSelection.disabled = count === 0;
    showSelected.disabled = count === 0;
    showSelected.textContent = "Selected (" + count + ")";
    showSelected.setAttribute("aria-pressed", String(selectedOnly));
  }
  document.getElementById("open-repo-picker").addEventListener("click", event => {
    opener = event.currentTarget;
    activeOwner = readyOwners().length === 1 ? readyOwners()[0].dataset.owner : null;
    ownerTitle.textContent = activeOwner ? "Add " + activeOwner + " repos" : "Add repos";
    resetPicker();
    ownerChoice.hidden = activeOwner !== null;
    ownerSheet.hidden = activeOwner === null;
    backOwners.hidden = activeOwner === null || readyOwners().length <= 1;
    dialog.showModal();
    if (activeOwner && window.innerWidth >= 768) search.focus();
  });
  function openOwner(owner) {
    activeOwner = owner;
    ownerChoice.hidden = true;
    ownerSheet.hidden = false;
    backOwners.hidden = readyOwners().length <= 1;
    ownerTitle.textContent = "Add " + owner + " repos";
    resetPicker();
    if (window.innerWidth >= 768) search.focus();
  }
  for (const button of ownerButtons) {
    button.addEventListener("click", () => openOwner(button.dataset.owner));
  }
  for (const link of ownerChoice.querySelectorAll(".gh-owner-retry")) {
    link.addEventListener("click", async event => {
      event.preventDefault();
      link.textContent = "Loading repos";
      try {
        const response = await fetch(link.href, {credentials: "same-origin"});
        if (!response.ok) throw new Error("GitHub did not answer");
        const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
        const nextButton = [...fresh.querySelectorAll(".gh-owner-button")]
          .find(button => button.dataset.owner === link.dataset.owner);
        if (!nextButton || nextButton.disabled) throw new Error("Owner still unavailable");
        const oldSnapshot = form.querySelector('input[name="snapshot"]').value;
        const nextSnapshot = fresh.querySelector('input[name="snapshot"]')?.value;
        const nextOptions = [...fresh.querySelectorAll(
          ".gh-picker-choice:not(.gh-picker-connected)"
        )];
        const otherIds = entries => entries.filter(option =>
          option.dataset.owner !== link.dataset.owner
        ).map(option => option.dataset.repoId).sort().join(",");
        if (nextSnapshot !== oldSnapshot || otherIds(nextOptions) !== otherIds(options)) {
          throw new Error("Repos changed. Reload and try again.");
        }
        for (const name of ["visibility", "visibility_sig"]) {
          form.querySelector(`input[name="${name}"]`).value =
            fresh.querySelector(`input[name="${name}"]`).value;
        }
        for (const option of nextOptions.filter(option =>
          option.dataset.owner === link.dataset.owner)) {
          const copied = document.importNode(option, true);
          dialog.querySelector("#no-search-results").before(copied);
          options.push(copied);
        }
        for (const option of fresh.querySelectorAll(".gh-picker-connected")) {
          if (option.dataset.owner !== link.dataset.owner) continue;
          const copied = document.importNode(option, true);
          dialog.querySelector("#no-search-results").before(copied);
          connected.push(copied);
        }
        const button = ownerButtons.find(item => item.dataset.owner === link.dataset.owner);
        button.innerHTML = nextButton.innerHTML;
        button.disabled = false;
        link.remove();
        updatePicker();
      } catch (error) {
        link.textContent = error.message || "Try again";
      }
    });
  }
  backOwners.addEventListener("click", () => {
    activeOwner = null; ownerChoice.hidden = false; ownerSheet.hidden = true;
    backOwners.hidden = true;
    ownerTitle.textContent = "Add repos"; updatePicker();
  });
  document.getElementById("close-repo-picker").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    pickerDraft.clear();
    for (const option of options) checkbox(option).checked = false;
    if (opener) opener.focus();
  });
  function rememberPicker() {
    pickerDraft.clear();
    for (const option of options) {
      if (checkbox(option).checked) pickerDraft.add(option.dataset.repoId);
    }
    let changed = false;
    for (const id of staged.keys()) {
      if (pickerDraft.has(id)) continue;
      staged.delete(id);
      const row = rowFor(id);
      if (row?.dataset.added === "true") {
        row.remove();
        if (working === id) { working = null; workingExplicit = true; }
      }
      changed = true;
    }
    if (changed) render();
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
  document.getElementById("owner-clear-selection").addEventListener("click", () => {
    for (const option of options) checkbox(option).checked = false;
    rememberPicker();
  });
  search.addEventListener("input", updatePicker);
  showSelected.addEventListener("click", () => {
    selectedOnly = !selectedOnly;
    showSelected.setAttribute("aria-pressed", String(selectedOnly));
    updatePicker();
  });
  stage.addEventListener("click", () => {
    const selected = options.filter(o => checkbox(o).checked);
    for (const option of selected) {
      const id = option.dataset.repoId;
      staged.set(id, "write");
      if (!rowFor(id)) makeAddedRow(id, option.dataset.name, option.dataset.installation);
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
  document.getElementById("owner-stage-repos").addEventListener("click", () => stage.click());
  function showWorkingPicker(selectedId) {
    workingSelect.replaceChildren();
    for (const choice of rows().filter(item =>
      !removed.has(item.dataset.repoId) && item.dataset.workingEligible === "true")) {
      workingSelect.add(new Option(choice.dataset.name, choice.dataset.repoId));
    }
    workingSelect.add(new Option("No working repo", "clear"));
    workingSelect.value = selectedId || "clear";
    workingDialog.showModal();
    workingSelect.focus();
  }
  changeWorking.addEventListener("click", () => showWorkingPicker(working));
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
        showWorkingPicker(id);
      } else {
        working = id;
        workingExplicit = true;
        button.focus();
      }
    } else if (button.dataset.action === "remove") {
      staged.delete(id);
      if (row.dataset.added === "true") {
        pickerDraft.delete(id);
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
    pickerDraft.clear();
    for (const box of form.querySelectorAll(".gh-follow-org input")) box.checked = false;
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


def _agent_editor_page(view: AgentEditorView) -> Response:
    """Render one agent's grants and an optional staged add dialog."""
    esc = html.escape
    agent_name = view.agent_name
    grants = view.grants
    installations = view.installations
    needed = view.needs_write
    usable = {repo.id: repo for repo in view.visible_repos}
    available = view.available
    requested_id = view.requested_repo_id
    repairable_ids = set(view.repairable_repo_ids)
    repairable = [repo for repo in grants if repo.repo_id in repairable_ids]
    working = next((repo for repo in grants if repo.repo_id == view.working_repo_id), None)
    may_change_working = True
    owner_by_login = {item.owner_login.casefold(): item for item in installations}
    past_cap_ids = set(view.past_cap_repo_ids)
    github_user_id = view.github_user_id
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
        f'<div class="gh-editor-chips"><span>Can read and change</span><span>{len(grants)} '
        f"{'repo' if len(grants) == 1 else 'repos'}</span></div>",
        '<form id="github-connect-form" class="gh-editor" method="post" '
        f'data-working="{working.repo_id if working else ""}" '
        f'data-may-change-working="{str(may_change_working).lower()}" '
        f'data-agent="{esc(agent_name, quote=True)}" '
        f'data-owner-limit="{view.owner_limit}" '
        f'action="{esc(view.confirm_url, quote=True)}">',
        f'<input type="hidden" name="state" value="{esc(view.state, quote=True)}">',
        f'<input type="hidden" name="invitation" value="{esc(view.invitation_hash, quote=True)}">',
        f'<input type="hidden" name="receipt" value="{view.receipt}">',
        f'<input type="hidden" name="snapshot" value="{view.snapshot}">',
        f'<input type="hidden" name="snapshot_sig" value="{view.snapshot_sig}">',
        f'<input type="hidden" name="visibility" value="{view.visibility}">',
        f'<input type="hidden" name="visibility_sig" value="{view.visibility_sig}">',
        '<input type="hidden" name="access" value="write">',
        '<input id="working-input" type="hidden" name="working" value="keep" disabled>',
    ]
    if view.pending_installation is not None and view.pending_installation.found:
        organization = view.pending_installation.account_login
        parts.append(
            '<p class="gh-editor-notice">'
            + (
                f"{'An' if organization[0].lower() in 'aeiou' else 'A'} {esc(organization)} owner"
                if organization
                else "A GitHub owner"
            )
            + ' must approve Daimon. Check again after approval. <a href="'
            + f'{esc(view.check_url, quote=True)}">Check again</a></p>'
        )
    if view.selection_error:
        parts.append(
            f'<p class="gh-inline-error" role="alert">{icon("triangle-alert")}'
            f"{esc(view.selection_error)}</p>"
        )
    working_name = (
        '<span class="gh-working-owner">'
        + esc(working.full_name.partition("/")[0])
        + '/</span><span class="gh-working-repo">'
        + esc(working.full_name.partition("/")[2])
        + "</span>"
        if working and working.repo_id in usable
        else "Hidden repo"
        if working
        else "None chosen"
    )
    parts.append(
        '<section id="working-card" class="gh-working-card" aria-label="Working repo"'
        + ("" if grants else " hidden")
        + f">{icon('pin')}<span>Working repo</span>"
        + f'<strong id="working-card-name">{working_name}</strong>'
        + '<button id="change-working" class="gh-add-button gh-js" type="button">'
        + ("Change" if working else "Choose")
        + "</button></section>"
    )
    parts.append('<div id="connected-repos" class="gh-connected-list">')
    if not grants:
        parts.append('<p class="gh-editor-empty">No repos connected yet.</p>')
    for owner in view.owner_order:
        installation = owner_by_login.get(owner.casefold())
        owner_grants = [
            repo
            for repo in grants
            if repo.full_name.partition("/")[0].casefold() == owner.casefold()
        ]
        owner_options = [
            option
            for option in options
            if option.name.partition("/")[0].casefold() == owner.casefold()
        ]
        parts.append(
            f'<section class="gh-owner-group" data-owner="{esc(owner, quote=True)}"'
            + (
                ' data-person="true"'
                if installation and installation.owner_id == github_user_id
                else ""
            )
            + ("" if owner_grants else " hidden")
            + f'><h3 class="gh-owner-heading">{_owner_avatar(installation)}{esc(owner)}'
            + (
                f" <span>{icon('triangle-alert')}Paused on GitHub</span>"
                if installation and installation.state == "suspended"
                else ""
            )
            + (
                f" <span>{icon('triangle-alert')}GitHub list unavailable</span>"
                if installation and installation.fetch_state == "error"
                else ""
            )
            + f'</h3><div class="gh-owner-rows" data-available="{len(owner_options)}">'
        )
        for repo in owner_grants:
            editable = repo.repo_id in usable
            repo_name = esc(repo.full_name.partition("/")[2]) if editable else "Hidden repo"
            data_name = esc(repo.full_name, quote=True) if editable else ""
            action_name = esc(repo.full_name, quote=True) if editable else "Hidden repo"
            parts.append(
                f'<article class="gh-agent-row" data-repo-id="{repo.repo_id}" '
                f'data-name="{data_name}" '
                f'data-installation="{repo.installation_id or ""}" '
                f'data-working-eligible="{str(editable and repo.status == "active").lower()}" '
                f'data-access="{repo.ceiling_access}">'
                '<div class="gh-row-top"><span class="gh-row-icon">'
                + icon("book-marked" if editable else "lock")
                + '</span><span class="gh-row-content"><strong class="gh-row-name">'
                f'{repo_name}</strong><span class="gh-working-badge"'
                + ("" if repo.is_working_repo else " hidden")
                + f">{icon('pin')}Working repo</span>"
                + '<span class="gh-pending-label" aria-live="polite" hidden></span>'
                + '</span><div class="gh-row-actions gh-js">'
            )
            parts.append(
                '<button class="gh-row-action gh-remove-action" type="button" '
                f'data-action="remove" aria-label="Remove {action_name}">'
                f"{icon('trash-2')}Remove</button>"
                '<button class="gh-row-action gh-undo-action" type="button" '
                'data-action="undo" '
                f'aria-label="Undo removal of {action_name}" '
                f"hidden>{icon('undo-2')}Undo</button>"
            )
            parts.append('</div></div><div class="gh-row-meta">')
            if repo.status != "active":
                parts.append(
                    f'<span class="gh-repo-state">{icon("triangle-alert")}Unavailable</span>'
                )
            parts.append("</div>")
            if repo.repo_id in past_cap_ids:
                parts.append(
                    f'<p class="gh-repo-locked">{icon("triangle-alert")}'
                    "Past the 2,000-repo limit, so you can only remove it here.</p>"
                )
            elif not editable:
                parts.append(
                    f'<p class="gh-repo-locked">{icon("triangle-alert")}'
                    "You can't see this repo on GitHub.</p>"
                )
            if repo.repo_id in needed and needed[repo.repo_id]:
                parts.append(
                    f'<p class="gh-repo-state">{icon("triangle-alert")}'
                    "Needs read and write to finish setup.</p>"
                )
            parts.append(f'<input type="hidden" name="remove" value="{repo.repo_id}" disabled>')
            if editable and repo.repo_id in repairable_ids:
                parts.append(
                    f'<input type="hidden" name="repo" value="{repo.repo_id}" disabled>'
                    f'<input class="gh-scoped-access" type="hidden" '
                    f'name="access_{repo.repo_id}" value="write" disabled>'
                )
            parts.append("</article>")
        parts.append("</div></section>")
    parts.append("</div>")
    parts.append(
        '<button id="open-repo-picker" class="gh-add-button gh-js" type="button">'
        f"{icon('plus')}Add repos</button>"
    )
    parts.append(follow_offers_html(view.follow_offers, agent_name=agent_name))
    if view.following:
        parts.append(following_note_html(agent_name=agent_name))
    owner_choice_rows: list[str] = []
    for item in installations:
        count = sum(
            repo.full_name.partition("/")[0].casefold() == item.owner_login.casefold()
            for repo in grants
        )
        disabled = item.state == "suspended" or item.fetch_state == "error"
        row = (
            '<div class="gh-owner-row"><button class="gh-owner-button" type="button" '
            f'data-owner="{esc(item.owner_login, quote=True)}"'
            + (" disabled" if disabled else "")
            + f'>{_owner_avatar(item)}<span class="gh-owner-login">{esc(item.owner_login)}</span>'
            + (
                f'<small class="gh-owner-added">{icon("circle-check")}{count} '
                f"{'repo' if count == 1 else 'repos'} added</small>"
                if count
                else ""
            )
            + '<small class="gh-owner-selected"></small>'
            + (
                f'<small class="gh-owner-warning">{icon("triangle-alert")}Paused on GitHub</small>'
                if item.state == "suspended"
                else ""
            )
            + (
                f'<small class="gh-owner-warning">{icon("triangle-alert")}'
                "Could not load repos.</small>"
                if item.fetch_state == "error"
                else ""
            )
            + icon("chevron-right")
            + "</button>"
        )
        if item.fetch_state == "error":
            row += (
                f'<a class="gh-link gh-owner-retry" '
                f'data-owner="{esc(item.owner_login, quote=True)}" '
                f'href="{esc(view.check_url, quote=True)}">Try again</a>'
            )
        owner_choice_rows.append(row + "</div>")
    parts.append(
        '<dialog id="repo-picker" class="gh-repo-dialog" aria-labelledby="repo-picker-title">'
        '<div class="gh-dialog-head">'
        '<button id="back-repo-owners" class="gh-link-button" type="button" hidden>'
        f"{icon('chevron-left')}Back</button>"
        '<h2 id="repo-picker-title">Add repos</h2>'
        '<button id="close-repo-picker" class="gh-dialog-close" type="button" '
        f'aria-label="Close add repos">{icon("x")}</button></div>'
        '<div id="repo-owner-choice" class="gh-owner-choice">'
        f"<p>{esc(agent_name)} can read and change the repos you add.</p>"
        + "".join(owner_choice_rows)
        + f'<p class="gh-dialog-link gh-not-listed"><span>{icon("info")}Not listed?</span>'
        '<a class="gh-add-button" href="'
        + esc(view.install_url, quote=True)
        + '">Open GitHub setup</a></p>'
        '<div class="gh-dialog-footer" hidden><span id="owner-picker-count">0 selected</span>'
        '<button id="owner-clear-selection" class="gh-link-button" '
        'type="button">Clear selection</button>'
        '<button id="owner-stage-repos" class="gh-primary" '
        'type="button" disabled>Add 0 repos</button>'
        "</div></div>"
        '<div id="repo-owner-sheet">'
        '<span class="gh-sheet-chip">Can read and change</span>'
        + "".join(
            f'<p class="gh-owner-truncated gh-dialog-copy" '
            f'data-owner="{esc(item.owner_login, quote=True)}" hidden>'
            f"{icon('triangle-alert')}This list is limited to 2,000 repos.</p>"
            for item in installations
            if item.truncated
        )
        + f'<div class="gh-dialog-search">{icon("search")}'
        '<input id="search-repos" class="gh-search" type="search" '
        'aria-label="Search repos" placeholder="Search repos" autocomplete="off"></div>'
        '<div class="gh-dialog-link gh-dialog-bulk"><span id="shown-count"></span>'
        '<button id="select-shown" class="gh-link-button" type="button">'
        f"Select all {len(options)}</button>"
        '<button id="show-selected-repos" class="gh-link-button" type="button" '
        'aria-pressed="false" disabled>Selected (0)</button>'
        '<button id="clear-selection" class="gh-link-button" type="button">Clear selection</button>'
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
            f'data-owner="{esc(option.name.partition("/")[0], quote=True)}" '
            f'data-installation="{option.installation_id}" '
            f'data-max-access="{"write" if option.can_write else "read"}" '
            f'data-required-write="{str(required_write).lower()}">'
            '<label class="gh-picker-label"><span class="gh-row-icon">'
            + icon("book-marked")
            + '</span><input class="gh-picker-toggle" type="checkbox"'
            + (" checked" if option.repo_id == requested_id else "")
            + ">"
            f'<span class="gh-picker-name">{esc(option.name.partition("/")[2])}'
            + ("<small>Finish setup</small>" if option.repair else "")
            + ("<small>Required to finish setup</small>" if required_write else "")
            + "</span></label></div>"
        )
    for repo in grants:
        if repo.repo_id not in usable or repo.repo_id in repairable_ids:
            continue
        parts.append(
            f'<div class="gh-picker-choice gh-picker-connected" '
            f'data-owner="{esc(repo.full_name.partition("/")[0], quote=True)}" '
            f'data-name="{esc(repo.full_name, quote=True)}">'
            f'<span class="gh-row-icon">{icon("book-marked")}</span>'
            f'<span class="gh-picker-name">{esc(repo.full_name.partition("/")[2])}'
            "</span><small>✓ Added</small></div>"
        )
    parts.append(
        '<p id="no-search-results" hidden>No matching repos.</p></div>'
        '<div class="gh-dialog-footer"><span id="picker-count">Not saved yet. Save changes '
        "after adding to the list.</span>"
        '<button id="stage-repos" class="gh-primary" type="button" disabled>Add to list</button>'
        "</div></div></dialog>"
    )
    parts.append(
        '<dialog id="working-picker" class="gh-repo-dialog" '
        'aria-labelledby="working-picker-title">'
        '<div class="gh-dialog-head"><h2 id="working-picker-title">Change working repo</h2>'
        '<button id="close-working" class="gh-dialog-close" type="button" '
        f'aria-label="Close working repo chooser">{icon("x")}</button></div>'
        '<p class="gh-dialog-copy">Opens in the agent\'s project files.</p>'
        '<label class="gh-dialog-copy" for="working-choice">Working repo</label>'
        '<select id="working-choice" class="gh-working-select"></select>'
        '<div class="gh-dialog-footer"><button id="confirm-working" class="gh-primary" '
        'type="button">Choose working repo</button></div></dialog>'
    )
    parts.append(
        '<template id="added-row-template"><article class="gh-agent-row">'
        '<div class="gh-row-top"><span class="gh-row-icon">'
        f'{icon("book-marked")}</span><span class="gh-row-content">'
        '<strong class="gh-row-name"></strong>'
        '<span class="gh-working-badge" hidden>Working repo</span>'
        '<span class="gh-pending-label" aria-live="polite" hidden></span></span>'
        '<div class="gh-row-actions">'
        f'<button class="gh-row-action gh-remove-action" type="button" data-action="remove">'
        f"{icon('trash-2')}Remove</button>"
        '<button class="gh-row-action gh-undo-action" type="button" data-action="undo" hidden>'
        f"{icon('undo-2')}Undo</button>" + '</div></div><div class="gh-row-meta">'
        "</div>"
        '<input type="hidden" name="repo" disabled>'
        "</article></template>"
    )
    # Browsers without scripting still get a complete native form.
    parts.append('<noscript><section class="gh-fallback"><h2>Edit repos</h2>')
    previous_owner = ""
    for repo in grants:
        owner = repo.full_name.partition("/")[0]
        if owner != previous_owner:
            parts.append(f"<h3>{esc(owner)}</h3>")
            previous_owner = owner
        parts.append(
            f'<label><input type="checkbox" name="remove" value="{repo.repo_id}">'
            f"Remove {esc(repo.full_name) if repo.repo_id in usable else 'Hidden repo'}</label>"
        )
        if repo.repo_id in repairable_ids:
            parts.append(
                f'<label><input type="checkbox" name="repo" value="{repo.repo_id}">'
                f"Finish setup for {esc(repo.full_name)}</label>"
                f'<input type="hidden" name="access_{repo.repo_id}" value="write">'
            )
    previous_owner = ""
    for repo in sorted(available, key=lambda item: item.full_name.casefold()):
        owner = repo.full_name.partition("/")[0]
        if owner != previous_owner:
            parts.append(f"<h3>{esc(owner)}</h3>")
            previous_owner = owner
        parts.append(
            f'<label><input type="checkbox" name="repo" value="{repo.id}"'
            + (" checked" if repo.id == requested_id else "")
            + ">"
            f"Add {esc(repo.full_name)}</label>"
        )
    for offer in view.follow_offers:
        parts.append(
            f'<label><input type="checkbox" name="follow_org" value="{offer.installation_id}">'
            f"Also give {esc(agent_name)} new repos added to {esc(offer.owner)}</label>"
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
        '<div class="gh-dock-chips"><span id="dock-add" hidden></span>'
        '<span id="dock-remove" hidden></span>'
        f'<span id="dock-working" hidden>{icon("pin")}<span></span></span></div>'
        '<small id="save-help" hidden></small>'
        f'<p id="owner-limit" class="gh-dock-warning" role="alert" hidden>{icon("triangle-alert")}'
        "<span></span></p></div>"
        '<div class="gh-editor-footer-actions">'
        '<button id="discard-repos" class="gh-editor-discard gh-js" type="button" hidden>'
        "Discard changes</button>"
        '<button id="save-repos" class="gh-primary" type="submit">Save changes</button>'
        f'<a id="cancel-editor" class="gh-editor-cancel" '
        f'href="{esc(view.cancel_url, quote=True)}">Cancel</a>'
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


def _confirmation_page(view: WorkspacePickerView) -> Response:
    """Render the same picker used by the live route and screenshot capture.

    Repos already added show ticked and greyed; only new ticks are sent. Repos
    the agent still needs before it can drop its old GitHub token are ticked,
    and still changeable.
    """
    needed = view.needs_write
    already_added = set(view.already_added_repo_ids)
    agent_name = view.agent_name
    platform = view.platform
    place = _place_label(platform)
    context = (
        f'<div class="gh-context">{platform_mark(platform)}'
        f"<span>{place}: {html.escape(view.workspace_label)}</span></div>"
    )
    parts = [
        f'<p class="gh-status-line">Connect repos to {html.escape(agent_name)}.</p>'
        if agent_name
        else '<p class="gh-status-line">Connect GitHub repos.</p>',
        '<p class="gh-action-line">Choose repos the agent can read and change.</p>',
        f'<form id="github-connect-form" method="post" '
        f'action="{html.escape(view.confirm_url, quote=True)}">',
        f'<input type="hidden" name="state" value="{html.escape(view.state, quote=True)}">',
        '<input type="hidden" name="invitation" '
        f'value="{html.escape(view.invitation_hash, quote=True)}">',
        f'<input type="hidden" name="receipt" value="{view.receipt}">',
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
            f'<p class="gh-inline-error" role="alert">{html.escape(view.selection_error)}</p>'
            if view.selection_error
            else "",
        ]
    )
    for installation in view.installations:
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
                    if repo.id in needed or repo.id == view.requested_repo_id
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
                f' <a href="{html.escape(view.install_url, quote=True)}">Open GitHub setup</a>'
                if view.install_url
                else ""
            )
            + "</p>"
            + "</section>",
            '<aside class="gh-side">',
            _access_choices(agent=agent_name is not None),
            "</aside></div>",
            '<details class="gh-access-help"><summary>How this works</summary>'
            "<p>The agent can read and change connected repos through the Daimon GitHub App. "
            "What it can do does not depend on each person's own GitHub permission."
            + (f" {html.escape(audience_line(agent_name, write=True))}" if agent_name else "")
            + "</p></details>",
            '<div class="gh-finish"><div class="gh-finish-status">'
            '<p id="selection-status" aria-live="polite">0 selected</p>'
            '<button class="gh-link-button" id="change-access" type="button" hidden>'
            "Change</button>" + '</div><div class="gh-finish-actions">',
            '<button class="gh-primary" id="connect-repos" type="submit">'
            f'{icon("github")}<span class="gh-button-label">'
            "Connect selected repos</span></button>"
            f'<a class="gh-link" href="{html.escape(view.cancel_url, quote=True)}">Cancel</a>'
            "</div></div>",
            "</form>",
            _PICKER_SCRIPT,
        ]
    )
    return github_page(
        title="Choose GitHub repos",
        body_html="".join(parts),
        kind="picker",
    )


def _back(target: ChatReturn | None) -> str:
    return _back_to_chat(target.platform, target.workspace_id) if target is not None else ""


def _render(view: ConnectView) -> Response:
    """Render one flow view as its page."""
    match view:
        case ConnectErrorView():
            return _error(view.message, view.status, view.retry_url, action=view.action)
        case ConnectTransientErrorView():
            retry_fields = view.retry_selection.form_fields() if view.retry_selection else None
            return _error(view.message, view.status, view.retry_url, retry_fields=retry_fields)
        case CancelledView():
            return _cancelled_page(_back(view.back_to_chat))
        case ExpiredView():
            return _expired_page(view.requester_label)
        case AlreadyConnectedView():
            return _already_connected_page(
                view.count, _back(view.back_to_chat), agent_name=view.agent_name
            )
        case InstallView():
            return _install_page(view.install_url, view.cancel_url)
        case PendingView():
            return _pending_page(view.check_url, view.cancel_url, view.organization)
        case NoReposView():
            return _no_repos_page(view.share_link, view.install_url)
        case AgentEditorView():
            return _agent_editor_page(view)
        case WorkspacePickerView():
            return _confirmation_page(view)
        case SavedAgentView():
            target = view.back_to_chat
            return _saved_agent_page(
                view.agent_name,
                view.grants,
                set(view.visible_repo_ids),
                f"https://discord.com/channels/{target.workspace_id}"
                if target is not None and target.platform == "discord"
                else None,
                installations=view.installations,
                following=view.following,
            )
        case DoneView():
            return _done_page(
                count=view.count,
                platform=view.back_to_chat.platform,
                external_id=view.back_to_chat.workspace_id,
                requester_label=view.requester_label,
                same_person=view.same_person,
                agent_name=view.agent_name,
            )


def _respond(outcome: ConnectOutcome) -> Response:
    response = (
        RedirectResponse(outcome.url)
        if isinstance(outcome, ConnectRedirect)
        else _render(outcome.view)
    )
    for cookie in outcome.cookies:
        response.set_cookie(
            cookie.name,
            cookie.value,
            max_age=cookie.max_age,
            httponly=True,
            secure=cookie.secure,
            samesite="lax",
        )
    return response


def build_oauth_github_routes(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    client_factory: ClientFactory | None = None,
    deployment_default: DeploymentDefault | None = None,
    anthropic: AsyncAnthropic | None = None,
) -> tuple[RouteHandler, RouteHandler, RouteHandler, RouteHandler]:
    """Build connect, callback, setup and confirmation handlers.

    With the web app on, connect and setup send the browser to the same page there
    (old links, and a GitHub App whose setup URL still names this host). Callback and
    confirm stay for flows started here and for personal account links.
    """
    root = settings.mcp.app_root_url
    web = WebLinks.from_settings(settings.web)
    if root is None:
        raise ValueError("GitHub connection is not configured")
    flow = GitHubConnectFlow(
        settings=settings,
        sessionmaker=sessionmaker,
        fernet=fernet,
        client_factory=client_factory,
        deployment_default=deployment_default,
        anthropic=anthropic,
        teams_owner_ids=teams_admin_owner_ids,
    )

    async def connect(request: Request) -> Response:
        if web is not None:
            return RedirectResponse(
                f"{web.base}/oauth/github/connect/{request.path_params['token']}"
            )
        return _respond(await flow.start(root, request.path_params["token"]))

    async def callback(request: Request) -> Response:
        return _respond(
            await flow.callback(
                root,
                state=request.query_params.get("state", ""),
                code=request.query_params.get("code", ""),
                flow_cookie=request.cookies.get(FLOW_COOKIE, ""),
            )
        )

    async def setup(request: Request) -> Response:
        if web is not None:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"{web.base}/oauth/github/setup{query}")
        return _respond(
            await flow.setup(
                root,
                state=request.query_params.get("state") or "",
                flow_cookie=request.cookies.get(FLOW_COOKIE, ""),
                state_cookie=request.cookies.get(STATE_COOKIE, ""),
            )
        )

    async def confirm(request: Request) -> Response:
        cookie = request.cookies.get(FLOW_COOKIE, "")
        if request.method == "POST":
            raw_body = await read_capped(
                request.stream(), request.headers.get("content-length"), MAX_CONFIRM_BODY_BYTES
            )
            if raw_body is None:
                return _render(selection_too_large())
            selection = selection_from_form(raw_body)
            if isinstance(selection, ConnectErrorView):
                return _render(selection)
            return _respond(await flow.save(root, flow_cookie=cookie, selection=selection))
        state = request.query_params.get("state", "")
        if request.query_params.get("cancel") == "1":
            return _respond(await flow.cancel(root, state=state, flow_cookie=cookie))
        return _respond(await flow.load(root, state=state, flow_cookie=cookie))

    return connect, callback, setup, confirm
