"""What each step of the GitHub repo connect flow shows, as data any page can render.

The flow in `github_connect_flow` returns these models; the MCP server renders
them as HTML and the web app as JSON. Views carry facts and URLs, never markup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode

from daimon.core.stores.github_access import MAX_AGENT_INSTALLATIONS, AgentRepo
from daimon.core.stores.github_connect import GitHubPermission, RepoConfirmation
from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

FLOW_COOKIE = "daimon_gh_connect"
# GitHub drops `state` from the setup redirect after some installation changes, so the
# browser keeps it too. It still has to match the flow cookie in `get_flow`.
STATE_COOKIE = "daimon_gh_connect_state"
FLOW_COOKIE_MAX_AGE = 900
# After a save the flow cookie proves the browser that saved, so its receipt page
# keeps working for a week.
SAVED_COOKIE_MAX_AGE = 7 * 24 * 60 * 60
# A save posts one `repo=<id>` per addition plus one shared `access=write`,
# so 256 KiB covers several thousand repos while bounding what one request can
# make the server read and parse. The cap is checked before any other work.
MAX_CONFIRM_BODY_BYTES = 256 * 1024
# Handed to `parse_qs` so a body of nothing but `&` cannot allocate a huge field list.
MAX_CONFIRM_FIELDS = 10_000
# The fields a save may carry. Anything else a client sent is dropped rather than
# echoed back into a retry.
SELECTION_FIELDS = frozenset(
    {
        "state",
        "invitation",
        "receipt",
        "snapshot",
        "snapshot_sig",
        "visibility",
        "visibility_sig",
        "repo",
        "remove",
        "access",
        "working",
    }
)
_SCOPED_ACCESS = re.compile(r"access_(\d+)")
_SCALARS = (
    "state",
    "invitation",
    "receipt",
    "snapshot",
    "snapshot_sig",
    "visibility",
    "visibility_sig",
)
_UNVERIFIED = "Selection could not be verified."
_Values = Annotated[tuple[str, ...], Field(max_length=MAX_CONFIRM_FIELDS)]


@pydantic_dataclass(frozen=True)
class ConnectRepo:
    id: int
    owner_id: int
    installation_id: int
    full_name: str
    # The signed-in person's GitHub permission on the repo; None when GitHub
    # reports none the flow can use.
    permission: GitHubPermission | None
    private: bool = False
    archived: bool = False

    @property
    def usable(self) -> bool:
        return self.permission is not None

    @property
    def max_access(self) -> Literal["read", "write"]:
        """The App grants write on any usable installed repository."""
        return "write" if self.usable else "read"


@pydantic_dataclass(frozen=True)
class ConnectInstallation:
    id: int
    owner_id: int
    owner_login: str
    repository_selection: str
    repos: tuple[ConnectRepo, ...]
    owner_type: str = ""
    avatar_url: str | None = None
    state: Literal["active", "suspended"] = "active"
    total: int = 0
    truncated: bool = False
    fetch_state: Literal["ok", "error"] = "ok"


@pydantic_dataclass(frozen=True)
class PendingInstallation:
    """Whether the signed-in person asked a GitHub owner to install the App."""

    found: bool
    account_login: str | None = None


class ConnectSelection(BaseModel):
    """One save, as the editor or picker posts it.

    Values stay strings: the flow validates them in a fixed order, so a bad value
    and a stale page refuse the same way whichever adapter parsed the body.
    `repo_access` maps a repo id to its `access_<id>` values; `unknown_fields`
    names anything else the client sent, which an agent save refuses.
    """

    model_config = ConfigDict(frozen=True)
    state: str = ""
    invitation: str = ""
    receipt: str = ""
    snapshot: str = ""
    snapshot_sig: str = ""
    visibility: str = ""
    visibility_sig: str = ""
    repo: _Values = ()
    remove: _Values = ()
    access: _Values = ()
    working: _Values = ()
    repo_access: Annotated[dict[str, _Values], Field(max_length=MAX_CONFIRM_FIELDS)] = Field(
        default_factory=dict[str, tuple[str, ...]]
    )
    unknown_fields: _Values = ()

    def form_fields(self) -> dict[str, list[str]]:
        """The known fields as form name to values, for a retry that re-posts them."""
        fields = {name: [value] for name in _SCALARS if (value := getattr(self, name))}
        lists = (("repo", self.repo), ("remove", self.remove), ("access", self.access))
        fields.update({name: list(values) for name, values in lists if values})
        if self.working:
            fields["working"] = list(self.working)
        fields.update({f"access_{key}": list(values) for key, values in self.repo_access.items()})
        return fields


def selection_from_fields(fields: Mapping[str, Sequence[str]]) -> ConnectSelection:
    """Read a parsed form; the first value of each single-valued field counts."""
    scalars = {name: fields[name][0] for name in _SCALARS if fields.get(name)}
    return ConnectSelection(
        **scalars,
        repo=tuple(fields.get("repo", ())),
        remove=tuple(fields.get("remove", ())),
        access=tuple(fields.get("access", ())),
        working=tuple(fields.get("working", ())),
        repo_access={
            match.group(1): tuple(values)
            for name, values in fields.items()
            if (match := _SCOPED_ACCESS.fullmatch(name))
        },
        unknown_fields=tuple(
            name
            for name in fields
            if name not in SELECTION_FIELDS and not _SCOPED_ACCESS.fullmatch(name)
        ),
    )


class ChatReturn(BaseModel):
    """Where the person came from, so a page can point them back."""

    model_config = ConfigDict(frozen=True)
    platform: str
    workspace_id: str


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ConnectErrorView(_View):
    """A refusal. `retry_url` is a link to try again; `action` replaces the advice."""

    kind: Literal["error"] = "error"
    message: str = "This link has expired."
    status: int = 400
    retry_url: str | None = None
    action: str | None = None


class ConnectTransientErrorView(_View):
    """An upstream failure the person can retry.

    With `retry_selection` the retry re-posts that selection to `retry_url`
    (a link would be a GET, which renders the page afresh and drops the ticks).
    """

    kind: Literal["transient_error"] = "transient_error"
    message: str
    status: int
    retry_url: str | None
    retry_selection: ConnectSelection | None = None


class CancelledView(_View):
    kind: Literal["cancelled"] = "cancelled"
    back_to_chat: ChatReturn | None = None


class ExpiredView(_View):
    kind: Literal["expired"] = "expired"
    requester_label: str


class AlreadyConnectedView(_View):
    kind: Literal["already_connected"] = "already_connected"
    count: int | None
    back_to_chat: ChatReturn | None = None
    agent_name: str | None = None


class InstallView(_View):
    """No installation is visible: the GitHub App must be installed first."""

    kind: Literal["install"] = "install"
    install_url: str
    cancel_url: str


class PendingView(_View):
    """The person asked a GitHub owner to approve the App; nothing to pick yet."""

    kind: Literal["pending"] = "pending"
    check_url: str
    cancel_url: str
    organization: str | None = None


class NoReposView(_View):
    """Installations exist but none of their repos are usable by this person."""

    kind: Literal["no_repos"] = "no_repos"
    # The setup link to hand to someone with access; empty when it can't be shown.
    share_link: str
    install_url: str


class AgentEditorView(_View):
    """One agent's repos with what can be added, repaired, removed or made working.

    `visible_repos` are the usable repos the signed-in person sees on GitHub;
    a grant outside them renders as a hidden repo that can only be removed.
    The form carries `state`, `invitation_hash`, `receipt`, `snapshot`,
    `snapshot_sig`, `visibility` and `visibility_sig` back on save.
    """

    kind: Literal["agent_editor"] = "agent_editor"
    agent_name: str
    workspace_label: str
    platform: str
    confirm_url: str
    check_url: str
    cancel_url: str
    install_url: str
    state: str
    invitation_hash: str
    receipt: str
    snapshot: str
    snapshot_sig: str
    visibility: str
    visibility_sig: str
    installations: list[ConnectInstallation]
    grants: list[AgentRepo]
    visible_repos: list[ConnectRepo]
    available: list[ConnectRepo]
    repairable_repo_ids: list[int]
    # Repo id to whether it needs write to finish setup.
    needs_write: dict[int, bool]
    requested_repo_id: int | None
    working_repo_id: int | None
    past_cap_repo_ids: list[int]
    owner_order: list[str]
    owner_limit: int
    github_user_id: int | None
    pending_installation: PendingInstallation | None
    selection_error: str | None


class WorkspacePickerView(_View):
    """The repo picker for a workspace (or a first agent) connection.

    Repos in `already_added_repo_ids` show ticked and fixed; only new ticks are
    sent. Repos in `needs_write` are ticked, and still changeable.
    """

    kind: Literal["workspace_picker"] = "workspace_picker"
    agent_name: str | None
    workspace_label: str
    platform: str
    confirm_url: str
    cancel_url: str
    install_url: str
    state: str
    invitation_hash: str
    receipt: str
    installations: list[ConnectInstallation]
    already_added_repo_ids: list[int]
    needs_write: dict[int, bool]
    requested_repo_id: int | None
    selection_error: str | None


class SavedAgentView(_View):
    """An agent's repos after a save; repos outside `visible_repo_ids` stay hidden."""

    kind: Literal["saved_agent"] = "saved_agent"
    agent_name: str
    grants: list[AgentRepo]
    visible_repo_ids: list[int]
    back_to_chat: ChatReturn | None
    installations: list[ConnectInstallation]


class DoneView(_View):
    kind: Literal["done"] = "done"
    count: int
    back_to_chat: ChatReturn
    requester_label: str
    # The GitHub account that saved is the requester's own linked account.
    same_person: bool
    agent_name: str | None


ConnectView = Annotated[
    ConnectErrorView
    | ConnectTransientErrorView
    | CancelledView
    | ExpiredView
    | AlreadyConnectedView
    | InstallView
    | PendingView
    | NoReposView
    | AgentEditorView
    | WorkspacePickerView
    | SavedAgentView
    | DoneView,
    Field(discriminator="kind"),
]


class ConnectCookie(BaseModel):
    """A cookie to set: HttpOnly, SameSite=Lax, Secure on an https root."""

    model_config = ConfigDict(frozen=True)
    name: str
    value: str
    max_age: int
    secure: bool


class ConnectRedirect(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["redirect"] = "redirect"
    url: str
    cookies: tuple[ConnectCookie, ...] = ()


class ConnectPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["page"] = "page"
    view: ConnectView
    cookies: tuple[ConnectCookie, ...] = ()


ConnectOutcome = ConnectRedirect | ConnectPage


def unverified(retry_url: str | None = None, status: int = 400) -> ConnectErrorView:
    return ConnectErrorView(message=_UNVERIFIED, status=status, retry_url=retry_url)


def selection_too_large() -> ConnectErrorView:
    return ConnectErrorView(
        message="That selection is too large to save.",
        status=413,
        action="Go back, choose fewer repos, and save again.",
    )


def selection_from_form(body: bytes) -> ConnectSelection | ConnectErrorView:
    """Parse a form body already capped at `MAX_CONFIRM_BODY_BYTES`."""
    try:
        fields = parse_qs(
            body.decode("utf-8"), keep_blank_values=True, max_num_fields=MAX_CONFIRM_FIELDS
        )
    except UnicodeDecodeError:
        return unverified()
    except ValueError:  # parse_qs: more than MAX_CONFIRM_FIELDS fields
        return selection_too_large()
    return selection_from_fields(fields)


def receipt_signature(state: str, invitation_hash: str, secret: str) -> str:
    message = f"{state}:{invitation_hash}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def snapshot_signature(state: str, invitation_hash: str, snapshot: str, secret: str) -> str:
    message = json.dumps([state, invitation_hash, snapshot], separators=(",", ":")).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def visibility_digest(installations: Sequence[ConnectInstallation]) -> str:
    """Bind the form to the GitHub repo lists it presented."""
    inventory = [
        (
            item.id,
            item.state,
            item.fetch_state,
            sorted(repo.id for repo in item.repos if repo.usable),
        )
        for item in installations
    ]
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def repo_confirmation(
    repo: ConnectRepo,
    *,
    requested: Literal["read", "write"],
    needs_write: bool,
    installation_writes: Mapping[int, bool],
) -> RepoConfirmation:
    """Confirm write access from the App installation for a usable repo."""
    if not repo.usable or not installation_writes.get(repo.installation_id, False):
        raise ValueError("The GitHub App needs write access to connect this repo.")
    return RepoConfirmation(
        repo_id=repo.id,
        owner_id=repo.owner_id,
        installation_id=repo.installation_id,
        full_name=repo.full_name,
        max_access="write",
        authorizer_permission=repo.permission,
    )


def confirm_url(root: str, state: str | None = None, *, cancel: bool = False) -> str:
    """The confirm step under `root`; with `state`, a GET that renders it afresh."""
    if state is None:
        return f"{root}/oauth/github/confirm"
    query = {"state": state, "cancel": "1"} if cancel else {"state": state}
    return f"{root}/oauth/github/confirm?{urlencode(query)}"


def install_url(app_slug: str, state: str) -> str:
    return f"https://github.com/apps/{app_slug}/installations/new?{urlencode({'state': state})}"


def _usable(installations: Sequence[ConnectInstallation]) -> dict[int, ConnectRepo]:
    return {repo.id: repo for item in installations for repo in item.repos if repo.usable}


def agent_editor_view(
    *,
    root: str,
    state: str,
    invitation_hash: str,
    secret: str,
    agent_name: str,
    workspace: str,
    platform: str,
    grants: list[AgentRepo],
    installations: list[ConnectInstallation],
    snapshot: str,
    cancel_url: str,
    install_url: str,
    needed: Mapping[int, bool] | None = None,
    requested_repo: str | None = None,
    selection_error: str | None = None,
    pending_installation: PendingInstallation | None = None,
    github_user_id: int | None = None,
) -> AgentEditorView:
    """One agent's grants, what the person may add, and the signed form fields."""
    snapshot_digest = hashlib.sha256(snapshot.encode()).hexdigest()
    visibility = visibility_digest(installations)
    needed = needed or {}
    usable = _usable(installations)
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
    owner_by_login = {item.owner_login.casefold(): item for item in installations}
    past_cap_ids = [
        repo.repo_id
        for repo in grants
        if (
            (item := owner_by_login.get(repo.full_name.partition("/")[0].casefold()))
            and item.state == "active"
            and item.fetch_state == "ok"
            and item.truncated
            and repo.repo_id not in usable
        )
    ]
    owner_logins = {repo.full_name.partition("/")[0] for repo in grants}
    owner_logins.update(item.owner_login for item in installations)
    ordered_owners = sorted(
        owner_logins,
        key=lambda login: (
            not any(
                repo.is_working_repo
                and repo.full_name.partition("/")[0].casefold() == login.casefold()
                for repo in grants
            ),
            owner_by_login.get(login.casefold()) is None
            or owner_by_login[login.casefold()].owner_id != github_user_id,
            login.casefold(),
        ),
    )
    working = next((repo for repo in grants if repo.is_working_repo), None)
    return AgentEditorView(
        agent_name=agent_name,
        workspace_label=workspace,
        platform=platform,
        confirm_url=confirm_url(root),
        check_url=confirm_url(root, state),
        cancel_url=cancel_url,
        install_url=install_url,
        state=state,
        invitation_hash=invitation_hash,
        receipt=receipt_signature(state, invitation_hash, secret),
        snapshot=snapshot_digest,
        snapshot_sig=snapshot_signature(state, invitation_hash, snapshot_digest, secret),
        visibility=visibility,
        visibility_sig=snapshot_signature(state, invitation_hash, visibility, secret),
        installations=installations,
        grants=grants,
        visible_repos=list(usable.values()),
        available=available,
        repairable_repo_ids=[
            repo.repo_id for repo in grants if repo.repo_id in usable and repo.repo_id in needed
        ],
        needs_write=dict(needed),
        requested_repo_id=requested_id,
        working_repo_id=working.repo_id if working else None,
        past_cap_repo_ids=past_cap_ids,
        owner_order=ordered_owners,
        owner_limit=MAX_AGENT_INSTALLATIONS,
        github_user_id=github_user_id,
        pending_installation=pending_installation,
        selection_error=selection_error,
    )


def workspace_picker_view(
    *,
    root: str,
    state: str,
    invitation_hash: str,
    secret: str,
    cancel_url: str,
    installations: list[ConnectInstallation],
    platform: str,
    workspace: str,
    agent_name: str | None,
    selection_error: str | None = None,
    already_added: frozenset[int] = frozenset(),
    needed: Mapping[int, bool] | None = None,
    requested_repo: str | None = None,
    install_url: str = "",
) -> WorkspacePickerView:
    requested_id = next(
        (
            repo.id
            for repo in _usable(installations).values()
            if requested_repo is not None and repo.full_name.casefold() == requested_repo.casefold()
        ),
        None,
    )
    return WorkspacePickerView(
        agent_name=agent_name,
        workspace_label=workspace,
        platform=platform,
        confirm_url=confirm_url(root),
        cancel_url=cancel_url,
        install_url=install_url,
        state=state,
        invitation_hash=invitation_hash,
        receipt=receipt_signature(state, invitation_hash, secret),
        installations=installations,
        already_added_repo_ids=sorted(already_added),
        needs_write=dict(needed or {}),
        requested_repo_id=requested_id,
        selection_error=selection_error,
    )
