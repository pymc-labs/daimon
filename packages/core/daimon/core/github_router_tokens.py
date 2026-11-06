"""Choose the GitHub App token for one call through the GitHub MCP router.

A routed session holds one installation token per installation and permission
chunk. Each MCP request names repos or owners in its tool arguments; this
module resolves those names with a fixed per-tool rule table, then picks the
one token that serves the whole request, or says why none can:

- a repo or owner outside the agent's grants rides along on the token the
  granted targets choose, or on the owner-free token when the call names no
  granted target; GitHub enforces each token's exact repo scope, so a public
  repo still reads and a private one gets GitHub's own 404, as on the direct
  path (agents read public repos such as other projects' sources this way);
- a request that needs two tokens gets `NeedsOneScope`, never partial results;
- a granted target with no usable token (mid-rotation, or a token broader than
  the current grant) gets `NotUsable`, a retry-later answer;
- a call the table cannot resolve gets `UnsupportedCall`.

A usable token is a delivered, unsuperseded row of the router session that
stays valid for EXPIRY_MARGIN, whose repos are all in the agent's current
grant view, under the same installation, with permissions no broader than
each repo's granted profile. Owner-free calls and non-call methods use the
token covering the working repo, else the lowest (installation_id, chunk).

Everything except `load_usable_rows` and `load_grant_view` is pure. Token
values never appear in reprs or logs.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.github_app_auth import PERMISSION_PROFILES, PermissionProfile
from daimon.core.stores.github_access import (
    AdmissibleGrantRows,
    admissible_grant_rows,
    list_live_grant_repositories,
)
from daimon.core.stores.github_issued_tokens import (
    decrypt_issued_token,
    list_router_session_tokens,
)
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

#: A token must stay valid this long past the request to be chosen, so a call
#: never starts on a token that expires mid-flight.
EXPIRY_MARGIN: Final = timedelta(seconds=60)

NO_ACCESS_MESSAGE: Final = "This agent has no GitHub access to {target}."
ONE_SCOPE_MESSAGE: Final = (
    "Ask about one repo at a time: this request spans repos that need separate GitHub access."
)
REFRESHING_MESSAGE: Final = (
    "GitHub access for this conversation is being refreshed. Try again in a minute."
)
UNSUPPORTED_MESSAGE: Final = (
    "Daimon cannot tell which GitHub repos the tool {tool} would reach with these "
    "arguments, so it did not run it."
)


# --- Call resolution ---------------------------------------------------------


@dataclass(frozen=True)
class NamedTargets:
    """Repos (lowercase `owner/name`) and owners (lowercase logins) a call names.

    A named owner means every repo the agent holds under that owner, so one
    token must cover all of them.
    """

    repos: frozenset[str] = frozenset()
    owners: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OwnerFree:
    """A call that names no repo or owner, such as `get_me` or an unqualified search."""


@dataclass(frozen=True)
class Unsupported:
    """A call whose reach the rule table cannot decide; it is refused, not guessed."""

    tool: str
    reason: str


type RepoScope = NamedTargets | OwnerFree | Unsupported


@dataclass(frozen=True)
class RepoArgs:
    """An owner argument and a repo argument that together name one repo.

    `required`: the call is unsupported unless the owner is given.
    `repo_optional`: the owner alone is valid and names the whole owner.
    """

    owner: str = "owner"
    repo: str = "repo"
    required: bool = True
    repo_optional: bool = False


@dataclass(frozen=True)
class ToolRule:
    """How one tool's arguments name repos and owners.

    `repo_args`: owner+repo pairs; a source and a destination pair both apply.
    `owner_args`: arguments that name an owner on their own (`org`, `organization`).
    `query_arg`: a search query whose `repo:`, `org:`, `user:` and `owner:`
    qualifiers name targets; a query with none is owner-free.
    `head_arg`: a pull request head that may name a fork owner as `owner:branch`.
    `refuse_args`: arguments that put the call out of reach (`enterprise`).
    A rule with none of these is owner-free.
    """

    repo_args: tuple[RepoArgs, ...] = ()
    owner_args: tuple[str, ...] = ()
    query_arg: str | None = None
    head_arg: str | None = None
    refuse_args: tuple[str, ...] = ()


OWNER_REPO: Final = ToolRule(repo_args=(RepoArgs(),))
OWNER_OPTIONAL_REPO: Final = ToolRule(repo_args=(RepoArgs(repo_optional=True),))
OPTIONAL_OWNER_REPO: Final = ToolRule(repo_args=(RepoArgs(required=False, repo_optional=True),))
SEARCH: Final = ToolRule(query_arg="query")
SEARCH_WITH_REPO: Final = ToolRule(
    repo_args=(RepoArgs(required=False, repo_optional=True),), query_arg="query"
)
LEVELED: Final = ToolRule(
    repo_args=(RepoArgs(required=False),), owner_args=("org",), refuse_args=("enterprise",)
)
OWNER_FREE: Final = ToolRule()

#: Every tool of GitHub's MCP server, with its argument rule. Captured
#: 2026-11-06 from the "Tools" and "Additional Tools in Remote GitHub MCP
#: Server" sections of https://github.com/github/github-mcp-server#tools
#: (README at commit 99d6d6503a7b2a133e2f8a595d6d0ba24b4086d3, 2026-11-04).
#: A tool missing from this table is unsupported when it carries repo-like
#: arguments. Arguments that only filter results (`list_pull_requests` head
#: and base, `repository_ruleset_read` repository_name) do not widen reach and
#: are not read. Person arguments (`get_teams` user, `list_gists` username,
#: `list_starred_repositories` username) name a user, not a repo owner.
TOOL_RULES: Final[Mapping[str, ToolRule]] = {
    # Actions
    "actions_get": OWNER_REPO,
    "actions_list": OWNER_REPO,
    "actions_run_trigger": OWNER_REPO,
    "get_job_logs": OWNER_REPO,
    # Code quality and code security
    "get_code_quality_finding": OWNER_REPO,
    "get_code_scanning_alert": OWNER_REPO,
    "list_code_scanning_alerts": OWNER_REPO,
    # Context
    "get_me": OWNER_FREE,
    "get_team_members": ToolRule(owner_args=("org",)),
    "get_teams": OWNER_FREE,
    "ui_get": OWNER_OPTIONAL_REPO,
    # Copilot
    "assign_copilot_to_issue": OWNER_REPO,
    "request_copilot_review": OWNER_REPO,
    "assign_copilot_to_issue_with_intent": OWNER_REPO,
    "create_pull_request_with_copilot": OWNER_REPO,
    "get_copilot_space": ToolRule(owner_args=("owner",)),
    "list_copilot_spaces": OWNER_FREE,
    "github_support_docs_search": OWNER_FREE,
    # Dependabot
    "get_dependabot_alert": OWNER_REPO,
    "list_dependabot_alerts": OWNER_REPO,
    # Discussions
    "discussion_comment_write": OWNER_REPO,
    "get_discussion": OWNER_REPO,
    "get_discussion_comments": OWNER_REPO,
    "list_discussion_categories": OWNER_OPTIONAL_REPO,
    "list_discussions": OWNER_OPTIONAL_REPO,
    # Gists
    "create_gist": OWNER_FREE,
    "get_gist": OWNER_FREE,
    "list_gists": OWNER_FREE,
    "update_gist": OWNER_FREE,
    # Git
    "get_repository_tree": OWNER_REPO,
    # Governance: repository, organization or enterprise level
    "create_repository_ruleset": LEVELED,
    "custom_properties_read": LEVELED,
    "custom_properties_write": LEVELED,
    "repository_ruleset_read": LEVELED,
    # Issues
    "add_issue_comment": OWNER_REPO,
    "get_label": OWNER_REPO,
    "issue_read": OWNER_REPO,
    "issue_write": ToolRule(
        repo_args=(RepoArgs(), RepoArgs("parent_owner", "parent_repo", required=False))
    ),
    "list_issue_fields": OWNER_OPTIONAL_REPO,
    "list_issue_types": OWNER_OPTIONAL_REPO,
    "list_issues": OWNER_REPO,
    "search_issues": SEARCH_WITH_REPO,
    "sub_issue_write": OWNER_REPO,
    "update_issue_comment": OWNER_REPO,
    # Labels
    "label_write": OWNER_REPO,
    "list_label": OWNER_REPO,
    # Notifications
    "dismiss_notification": OWNER_FREE,
    "get_notification_details": OWNER_FREE,
    "list_notifications": OPTIONAL_OWNER_REPO,
    "manage_notification_subscription": OWNER_FREE,
    "manage_repository_notification_subscription": OWNER_REPO,
    "mark_all_notifications_read": OPTIONAL_OWNER_REPO,
    # Organizations
    "search_orgs": SEARCH,
    # Projects
    "projects_get": ToolRule(owner_args=("owner",)),
    "projects_list": ToolRule(owner_args=("owner",)),
    "projects_write": ToolRule(
        owner_args=("owner",),
        repo_args=(RepoArgs("item_owner", "item_repo", required=False),),
    ),
    # Pull requests
    "add_comment_to_pending_review": OWNER_REPO,
    "add_reply_to_pull_request_comment": OWNER_REPO,
    "create_pull_request": ToolRule(repo_args=(RepoArgs(),), head_arg="head"),
    "list_pull_requests": OWNER_REPO,
    "merge_pull_request": OWNER_REPO,
    "pull_request_read": OWNER_REPO,
    "pull_request_review_write": OWNER_REPO,
    "search_pull_requests": SEARCH_WITH_REPO,
    "update_pull_request": OWNER_REPO,
    "update_pull_request_branch": OWNER_REPO,
    # Repositories
    "create_branch": OWNER_REPO,
    "create_or_update_file": OWNER_REPO,
    "create_repository": ToolRule(owner_args=("organization",)),
    "delete_file": OWNER_REPO,
    "delete_repository": OWNER_REPO,
    "fork_repository": ToolRule(repo_args=(RepoArgs(),), owner_args=("organization",)),
    "get_commit": OWNER_REPO,
    "get_file_contents": OWNER_REPO,
    "get_latest_release": OWNER_REPO,
    "get_release_by_tag": OWNER_REPO,
    "get_tag": OWNER_REPO,
    "list_branches": OWNER_REPO,
    "list_commits": OWNER_REPO,
    "list_releases": OWNER_REPO,
    "list_repository_collaborators": OWNER_REPO,
    "list_tags": OWNER_REPO,
    "push_files": OWNER_REPO,
    "search_code": SEARCH,
    "search_commits": SEARCH,
    "search_repositories": SEARCH,
    # Secret protection and security advisories
    "get_secret_scanning_alert": OWNER_REPO,
    "list_secret_scanning_alerts": OWNER_REPO,
    "get_global_security_advisory": OWNER_FREE,
    "list_global_security_advisories": OWNER_FREE,
    "list_org_repository_security_advisories": ToolRule(owner_args=("org",)),
    "list_repository_security_advisories": OWNER_REPO,
    # Stargazers and users
    "list_starred_repositories": OWNER_FREE,
    "star_repository": OWNER_REPO,
    "unstar_repository": OWNER_REPO,
    "search_users": SEARCH,
}

_REPO_LIKE_ARGS: Final = frozenset(
    {"owner", "repo", "org", "organization", "query", "repository", "full_name", "user"}
)
_REPO_LIKE_SUFFIXES: Final = ("_owner", "_repo", "_org", "_repository")
_LOGIN: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})")
_REPO_NAME: Final = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_QUALIFIER: Final = re.compile(
    r"(?<![\w-])(-?)(repo|org|user|owner)\s*:\s*(?:\"([^\"]*)\"|(\S+))", re.IGNORECASE
)


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text_arg(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _Refused(f"argument {name} is not a string")
    return value.strip()


def _login(value: str) -> str:
    if not _LOGIN.fullmatch(value):
        raise _Refused("an owner is not a GitHub login")
    return value.lower()


def _repo_name(owner: str, name: str) -> str:
    if not _REPO_NAME.fullmatch(name) or name in (".", ".."):
        raise _Refused("a repo is not a GitHub repository name")
    return f"{_login(owner)}/{name.lower()}"


def _full_repo_name(value: str) -> str:
    owner, slash, name = value.partition("/")
    if not slash:
        raise _Refused("a repo qualifier is not owner/name")
    return _repo_name(owner, name)


def _query_targets(query: str) -> tuple[set[str], set[str]]:
    """Repos and owners a search query's positive qualifiers name.

    A negated qualifier only removes results, so it does not widen reach.
    """
    repos: set[str] = set()
    owners: set[str] = set()
    for negated, kind, quoted, bare in _QUALIFIER.findall(query):
        if negated:
            continue
        value = quoted or bare
        if kind.lower() == "repo":
            repos.add(_full_repo_name(value))
        else:
            owners.add(_login(value))
    return repos, owners


def _has_repo_like_args(arguments: Mapping[str, object]) -> bool:
    return any(name in _REPO_LIKE_ARGS or name.endswith(_REPO_LIKE_SUFFIXES) for name in arguments)


def _apply_rule(rule: ToolRule, arguments: Mapping[str, object]) -> RepoScope:
    repos: set[str] = set()
    owners: set[str] = set()
    for name in rule.refuse_args:
        if _text_arg(arguments, name) is not None:
            raise _Refused(f"argument {name} is outside agent repo access")
    for pair in rule.repo_args:
        owner = _text_arg(arguments, pair.owner)
        repo = _text_arg(arguments, pair.repo)
        if owner is None:
            if repo is not None or pair.required:
                raise _Refused(f"argument {pair.owner} is missing")
            continue
        if repo is not None:
            repos.add(_repo_name(owner, repo))
        elif pair.repo_optional:
            owners.add(_login(owner))
        else:
            raise _Refused(f"argument {pair.repo} is missing")
    for name in rule.owner_args:
        owner = _text_arg(arguments, name)
        if owner is not None:
            owners.add(_login(owner))
    if rule.query_arg is not None:
        query = _text_arg(arguments, rule.query_arg)
        if query is not None:
            query_repos, query_owners = _query_targets(query)
            repos |= query_repos
            owners |= query_owners
    if rule.head_arg is not None:
        head = _text_arg(arguments, rule.head_arg)
        if head is not None and ":" in head:
            head_owner = _login(head.split(":", 1)[0])
            if not any(repo.startswith(f"{head_owner}/") for repo in repos):
                owners.add(head_owner)
    if not repos and not owners:
        return OwnerFree()
    return NamedTargets(frozenset(repos), frozenset(owners))


def resolve_call(tool_name: str, arguments: Mapping[str, object] | None) -> RepoScope:
    """Resolve one `tools/call` to the repos and owners it reaches."""
    args: Mapping[str, object] = arguments or {}
    rule = TOOL_RULES.get(tool_name)
    if rule is None:
        if _has_repo_like_args(args):
            return Unsupported(tool_name, "tool is not in the verified tool table")
        return OwnerFree()
    try:
        return _apply_rule(rule, args)
    except _Refused as refused:
        return Unsupported(tool_name, refused.reason)


# --- Grant view and token rows -----------------------------------------------


@dataclass(frozen=True)
class GrantedRepo:
    repo_id: int
    full_name: str
    installation_id: int
    profile: PermissionProfile

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0].lower()


@dataclass(frozen=True)
class GrantView:
    """The agent's current admissible grants, as the router checks them per request."""

    repos: tuple[GrantedRepo, ...]
    working_repo_id: int | None = None


@dataclass(frozen=True)
class RouterToken:
    """One issued-token row of a router session, with its decrypted value."""

    token_id: uuid.UUID
    installation_id: int
    chunk: int | None
    repo_ids: frozenset[int]
    permissions: Mapping[str, str]
    status: str
    superseded_at: datetime | None
    expires_at: datetime
    token: str = field(repr=False)


def grant_view_from_rows(rows: AdmissibleGrantRows) -> GrantView:
    """Build a grant view from `admissible_grant_rows` output.

    A repo listed twice keeps its widest profile, as token grouping does.
    """
    by_id: dict[int, GrantedRepo] = {}
    working: int | None = None
    for grant, repo, profile in rows:
        current = by_id.get(repo.repo_id)
        if current is None or profile == "write":
            by_id[repo.repo_id] = GrantedRepo(
                repo.repo_id, repo.repo_full_name, repo.installation_id, profile
            )
        if grant.is_working_repo and working is None:
            working = repo.repo_id
    return GrantView(tuple(sorted(by_id.values(), key=lambda r: r.repo_id)), working)


# --- Token choice -------------------------------------------------------------


@dataclass(frozen=True)
class TokenChoice:
    token: RouterToken


@dataclass(frozen=True)
class NoAccess:
    """Kept for callers' refusal tables; token choice no longer returns it.

    An ungranted target is not refused: it rides on the chosen token, and
    GitHub answers for it within that token's exact scope.
    """

    target: str

    @property
    def message(self) -> str:
        return NO_ACCESS_MESSAGE.format(target=self.target)


@dataclass(frozen=True)
class NeedsOneScope:
    @property
    def message(self) -> str:
        return ONE_SCOPE_MESSAGE


@dataclass(frozen=True)
class NotUsable:
    @property
    def message(self) -> str:
        return REFRESHING_MESSAGE


@dataclass(frozen=True)
class UnsupportedCall:
    tool: str
    reason: str

    @property
    def message(self) -> str:
        return UNSUPPORTED_MESSAGE.format(tool=self.tool)


type TokenRefusal = NoAccess | NeedsOneScope | NotUsable | UnsupportedCall
type TokenDecision = TokenChoice | TokenRefusal

_PERMISSION_RANK: Final[Mapping[str, int]] = {"none": 0, "read": 1, "write": 2, "admin": 3}
#: When calls in one batch fail differently, the batch reports the first kind here.
_REFUSAL_ORDER: Final = (UnsupportedCall, NoAccess, NotUsable, NeedsOneScope)


def _within_profile(permissions: Mapping[str, str], profile: PermissionProfile) -> bool:
    allowed = PERMISSION_PROFILES[profile]
    return all(
        _PERMISSION_RANK.get(level, len(_PERMISSION_RANK))
        <= _PERMISSION_RANK.get(allowed.get(name, "none"), 0)
        for name, level in permissions.items()
    )


def usable_tokens(
    rows: Iterable[RouterToken], grant_view: GrantView, *, now: datetime
) -> list[RouterToken]:
    """Rows the router may use now, best first.

    Best is the lowest (installation_id, chunk); within one chunk, as during a
    rotation's overlap, the row that expires last.
    """
    granted = {repo.repo_id: repo for repo in grant_view.repos}
    cutoff = now + EXPIRY_MARGIN
    usable = [
        row
        for row in rows
        if row.status == "delivered"
        and row.superseded_at is None
        and row.expires_at > cutoff
        and row.repo_ids
        and all(
            repo_id in granted
            and granted[repo_id].installation_id == row.installation_id
            and _within_profile(row.permissions, granted[repo_id].profile)
            for repo_id in row.repo_ids
        )
    ]
    return sorted(
        usable,
        key=lambda row: (
            row.installation_id,
            row.chunk if row.chunk is not None else 0,
            -row.expires_at.timestamp(),
            str(row.token_id),
        ),
    )


def _owner_free_candidates(
    usable: Sequence[RouterToken], working_repo: int | None
) -> frozenset[uuid.UUID] | TokenRefusal:
    if not usable:
        return NotUsable()
    if working_repo is not None:
        working = frozenset(row.token_id for row in usable if working_repo in row.repo_ids)
        if working:
            return working
    lowest = (usable[0].installation_id, usable[0].chunk)
    return frozenset(row.token_id for row in usable if (row.installation_id, row.chunk) == lowest)


def _named_candidates(
    scope: NamedTargets,
    usable: Sequence[RouterToken],
    grant_view: GrantView,
    working_repo: int | None,
) -> frozenset[uuid.UUID] | TokenRefusal:
    by_name = {repo.full_name.lower(): repo.repo_id for repo in grant_view.repos}
    # Targets outside the grants add nothing here: they ride on whichever
    # token the granted targets need.
    required = {by_name[name] for name in scope.repos if name in by_name}
    for owner in scope.owners:
        # An owner reaches every repo the agent holds under it, so one token
        # must cover them all; matching the installation is not enough when
        # permission or 500-repo chunks split it.
        required |= {repo.repo_id for repo in grant_view.repos if repo.owner == owner}
    if not required:
        # Only ungranted targets: use the owner-free token, as the direct
        # path's working token would be used.
        return _owner_free_candidates(usable, working_repo)
    candidates = frozenset(row.token_id for row in usable if required <= row.repo_ids)
    if candidates:
        return candidates
    covered = {repo_id for row in usable for repo_id in row.repo_ids}
    return NeedsOneScope() if required <= covered else NotUsable()


def _candidates(
    scope: RepoScope,
    usable: Sequence[RouterToken],
    grant_view: GrantView,
    working_repo: int | None,
) -> frozenset[uuid.UUID] | TokenRefusal:
    match scope:
        case Unsupported(tool=tool, reason=reason):
            return UnsupportedCall(tool, reason)
        case OwnerFree():
            return _owner_free_candidates(usable, working_repo)
        case NamedTargets():
            return _named_candidates(scope, usable, grant_view, working_repo)


def choose_batch_token(
    scopes: Sequence[RepoScope],
    usable_rows: Iterable[RouterToken],
    grant_view: GrantView,
    working_repo: int | None,
    *,
    now: datetime | None = None,
) -> TokenDecision:
    """The one token that serves every call of a request, or why none does."""
    if not scopes:
        raise ValueError("a request has at least one call")
    usable = usable_tokens(usable_rows, grant_view, now=now or datetime.now(UTC))
    shared: frozenset[uuid.UUID] | None = None
    refusals: list[TokenRefusal] = []
    for scope in scopes:
        candidates = _candidates(scope, usable, grant_view, working_repo)
        if isinstance(candidates, frozenset):
            shared = candidates if shared is None else shared & candidates
        else:
            refusals.append(candidates)
    if refusals:
        return min(refusals, key=lambda refusal: _REFUSAL_ORDER.index(type(refusal)))
    best = next((row for row in usable if shared is not None and row.token_id in shared), None)
    return TokenChoice(best) if best is not None else NeedsOneScope()


def choose_token(
    scope: RepoScope,
    usable_rows: Iterable[RouterToken],
    grant_view: GrantView,
    working_repo: int | None,
    *,
    now: datetime | None = None,
) -> TokenDecision:
    """The token for one call or non-call method (pass `OwnerFree()` for the latter)."""
    return choose_batch_token([scope], usable_rows, grant_view, working_repo, now=now)


# --- Database reads -----------------------------------------------------------


async def load_usable_rows(
    session: AsyncSession,
    router_session_id: uuid.UUID,
    *,
    fernet: MultiFernet,
    now: datetime | None = None,
) -> list[RouterToken]:
    """The router session's delivered, unsuperseded rows valid past EXPIRY_MARGIN, decrypted.

    Read per request with no cache, so a rotation's replacement rows are found
    as soon as they are delivered. A row that cannot be decrypted is skipped.
    """
    valid_after = (now or datetime.now(UTC)) + EXPIRY_MARGIN
    rows = await list_router_session_tokens(
        session, router_session_id=router_session_id, valid_after=valid_after
    )
    tokens: list[RouterToken] = []
    for row in rows:
        try:
            value = decrypt_issued_token(row, fernet=fernet)
        except InvalidToken:
            _log.warning("GitHub router token row %s cannot be decrypted", row.token_id)
            continue
        if value is None:
            continue
        tokens.append(
            RouterToken(
                token_id=row.token_id,
                installation_id=row.installation_id,
                chunk=row.chunk,
                repo_ids=frozenset(row.repo_ids),
                permissions=dict(row.permissions),
                status=row.status,
                superseded_at=row.superseded_at,
                expires_at=row.expires_at,
                token=value,
            )
        )
    return tokens


async def load_grant_view(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> GrantView:
    """The agent's current admissible grants, read without taking the grant lock."""
    live = await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id)
    rows: AdmissibleGrantRows = tuple(
        (
            grant,
            repo,
            "write" if grant.ceiling_access == "write" and repo.max_access == "write" else "read",
        )
        for grant, repo in live
    )
    accepted, _ = admissible_grant_rows(rows)
    return grant_view_from_rows(accepted)
