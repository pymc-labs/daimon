"""Post requester-only private forms for agent keys, MCP tokens and GitHub access.

These tools create single-use, expiring request rows and post a target-naming
card through the caller's platform. Secret values never enter tool arguments.
Submission checks requester identity; these enrollment paths deliberately do
not inherit the admin gate for direct agent-spec mutations.

Replacing a key that already exists is the one exception: it destroys shared
state, so `daimon.core.operation_policy` decides it here, *before* the mint,
and a refusal posts no card at all — nobody is asked for a secret they were
never going to be allowed to save.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final
from urllib.parse import urlparse

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import (
    _post_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._credential_button import (
    edit_card_replaced as edit_discord_card_replaced,
)
from daimon.adapters.mcp.tools.setup_target import require_turn_origin, resolve_setup_agent
from daimon.adapters.mcp.tools.slack._credential_button import (
    _post_slack_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._credential_button import (
    edit_card_replaced as edit_slack_card_replaced,
)
from daimon.core.continuity.continuation import MAX_REQUESTED_WORK, sanitize_requested_work
from daimon.core.credential_requests import (
    DEFAULT_TTL,
    ENV_FILE_TARGET,
    CredentialRequestKind,
    build_skill_repo_target,
    mint_request_token,
)
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_host
from daimon.core.operation_policy import (
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)
from daimon.core.stores.agent_files import get_agent_file
from daimon.core.stores.credential_requests import (
    create_credential_request,
    list_live_credential_requests,
    supersede_credential_request,
    update_credential_request_message,
)
from daimon.core.stores.domain import CredentialRequestRow, TurnOriginRow
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from daimon.core.stores.turn_origins import get_active_origin
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

# Mirrors packages/adapters/discord/daimon/adapters/discord/agent_setup/credentials.py's
# _POSIX_KEY_RE. Duplicated rather than imported: the Discord adapter and the
# MCP adapter cannot import each other (import-linter's independence
# contract), and this rule is small enough not to warrant a core lift.
_POSIX_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# `normalize_owner_repo` does not truncate to two path segments (it only
# strips a known prefix/suffix), so a URL like
# "https://github.com/owner/repo/extra" normalizes to "owner/repo/extra"
# rather than raising. This pattern is what actually enforces "exactly two
# segments" before the row is minted, guaranteeing the modal's later
# normalization sees the same shape this tool validated.
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_PENDING_TASK_DESCRIPTION = (
    "The work that should run once the value is saved, in the person's words "
    "— a script, a file, a question. Omit it when they only asked to save "
    "or replace the key; the request itself is never a task."
)

#: Words that mark work beyond the save itself. A `pending_task` that names the
#: key, server or repo but carries none of these is the request restated, not a
#: task waiting on it.
_WORK_WORDS: Final[tuple[str, ...]] = (
    "run",
    "finish",
    "append",
    "write",
    "fetch",
    "pull",
    "analy",
    "plot",
    "report",
    "build",
    "test",
    "deploy",
    "continue",
    "update",
    "generate",
    "compute",
    "query",
    "summar",
)

#: What the model should say once the card is up. The card already carries the
#: expiry as a live timestamp, the requester restriction and who can use the
#: value; a model paraphrasing any of those writes a sentence that stops being
#: true as it ages, and repeats what the reader can already see.
_REPLY_POINTS_AT_THE_FORM = (
    "Reply with at most one short sentence pointing to the form below, or "
    "nothing more if a form was all they asked for. Do not mention when it "
    "expires, who can open it, or who can use the key — the card says that."
)


class RequestCredentialResult(BaseModel):
    """Result of minting and posting a credential-request button."""

    model_config = ConfigDict(frozen=True)

    kind: CredentialRequestKind
    target: str
    message_id: str
    instruction: str
    """What to say about the posted card."""


def _named_single_key(text: str) -> str | None:
    """Return the one key name `text` spells out, or None when it names none.

    A file form is for several keys; a sentence carrying an UPPER_SNAKE word
    (`TOGGL_API_TOKEN`) names exactly one, and handing that person a whole
    `.env` upload is the mismatch this catches. A service name on its own
    ("the Higgsfield key") carries no underscore and names nothing here — the
    file form is still the right answer when the key argument is omitted.
    """
    for word in re.split(r"[^A-Za-z0-9_]+", text):
        if "_" in word and word.isupper() and _POSIX_KEY_RE.fullmatch(word):
            return word
    return None


def _require_requestable_platform(auth: AuthIdentity) -> str:
    """Refuse before the mint when the click could never be dispatched.

    The row is minted before the button is posted, so a caller whose platform
    context cannot carry a button (no bound identity, or a Slack caller with
    no workspace) must be refused here — after the mint the failure surfaces
    as "created but posting failed", leaving a dead row behind.
    """
    if auth.platform_user_id is None:
        raise ToolError("credential requests require a platform-bound identity")
    if auth.platform == "slack" and auth.external_id is None:
        raise ToolError("credential requests require a workspace context")
    return auth.platform_user_id


def _bounded_pending_task(pending_task: str | None, *, echoes: tuple[str, ...]) -> str | None:
    """Return the waiting task to persist, or None when it says nothing.

    `sanitize_requested_work` nulls out an empty, too-short, or name-echoing
    string; the slice is the caller's half of that contract (the sanitizer
    deliberately does not truncate). The echoes are the agent name and the
    target, the two strings a model restates instead of describing work.

    On top of that sits a heuristic backstop for a save-only request: text that
    names one of those and carries no word signalling work beyond the save
    ("give daimon a HIGGSFIELD_API_KEY so people can try it here") is the ask
    itself, and persisting it would buy a billed continuation for a request
    that ended at the card. The guidance in `defaults/` is the rule; this only
    catches the case where the model passed the ask back anyway. Text that does
    name work is kept even when it repeats the key name ("once
    TOGGL_API_TOKEN is saved, run /root/work/toggl_report.py").
    """
    work = sanitize_requested_work(pending_task, echoes=echoes)
    if work is None:
        return None
    normalized = work.lower()
    names_a_target = any(echo.strip().lower() in normalized for echo in echoes if echo.strip())
    describes_work = any(word in normalized for word in _WORK_WORDS)
    if names_a_target and not describes_work:
        return None
    return work[:MAX_REQUESTED_WORK]


async def _resolve_agent_uuid(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    expected_ma_agent_id: str | None,
    origin: TurnOriginRow,
) -> tuple[uuid.UUID, BetaManagedAgentsAgent]:
    """Return the derived agent UUID and the MA agent it was derived from.

    The MA agent is threaded back out rather than discarded because the minted
    row now records which agent the control targets (`target_ma_agent_id` /
    `target_name`), and re-resolving it at the mint site would be a second
    round trip that could disagree with this one.
    """
    if expected_ma_agent_id is None:
        if agent_name == origin.configuration_target_name:
            expected_ma_agent_id = origin.configuration_target_ma_agent_id
        elif agent_name == origin.responder_name:
            expected_ma_agent_id = origin.responder_ma_agent_id
    ma_agent = await resolve_setup_agent(
        runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
    )
    agent_uuid = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(ma_agent.id))
    return agent_uuid, ma_agent


async def _require_key_replacement_allowed(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    ma_agent: BetaManagedAgentsAgent,
    key: str,
) -> None:
    """Raise before the mint when this caller may not replace an existing key.

    `key_replace` is an attachment operation: an admin is allowed on any
    target (that is the first-run onboarding step), a non-admin is refused on
    a defaults-managed agent and on one that currently answers somewhere in
    the tenant. The reachability read is paid for only when the policy says
    the answer actually depends on it.
    """
    is_daimon_managed = ma_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    reachable = False
    if needs_reachability_read(
        "key_replace", is_admin=auth.is_admin, is_daimon_managed=is_daimon_managed
    ):
        async with runtime.session_factory() as session:
            reachable = await is_agent_reachable_in_tenant(
                session,
                tenant_id=auth.tenant_id,
                agent_name=ma_agent.name,
                default=runtime.deployment_default,
            )
    outcome = decide_operation(
        "key_replace",
        is_admin=auth.is_admin,
        target=TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable),
    )
    if outcome in ("managed_agent", "needs_admin"):
        raise ToolError(
            f"'{ma_agent.name}' is shared with everyone here, so replacing the key "
            f"'{key}' it already has needs a server or workspace admin, and the caller "
            f"is not one. Nothing changed: the existing '{key}' is still in use and no "
            "card was posted. Tell them an admin can ask Daimon to replace the "
            f"'{key}' key on '{ma_agent.name}'. Do not ask anyone for the value here "
            "and do not retry."
        )


async def _supersede_live_siblings(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester_platform_user_id: str,
    origin_thread_id: str,
    now: datetime,
) -> list[CredentialRequestRow]:
    """Retire every form this person already has open here, and return them.

    A corrected request used to leave its predecessor live, so one thread held
    two buttons for one intent and the stale one asked for the wrong thing.
    Scope is deliberately narrow — same requester, same thread, same agent —
    so a second person's form and another agent's form are untouched.

    Runs in the mint's own transaction, so the retirement and the new row
    commit together: there is never a moment with no live form at all. A row
    someone is submitting right this second wins its own UPDATE and is left
    out of the returned list, so no card is edited out from under them.
    """
    live = await list_live_credential_requests(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_platform_user_id=requester_platform_user_id,
        origin_thread_id=origin_thread_id,
        now=now,
    )
    retired: list[CredentialRequestRow] = []
    for row in live:
        if await supersede_credential_request(session, token=row.token, now=now) is not None:
            retired.append(row)
    return retired


async def _mint_and_post(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    kind: CredentialRequestKind,
    target: str,
    mcp_server_url: str | None,
    agent_id: uuid.UUID,
    ma_agent: BetaManagedAgentsAgent,
    requester_platform_user_id: str,
    agent_name: str,
    purpose: str,
    channel_id: str,
    origin: TurnOriginRow,
    requested_work: str | None,
    replaces_updated_at: datetime | None = None,
    branch: str | None = None,
) -> RequestCredentialResult:
    # A tool-supplied channel cannot redirect a private-input request.
    channel_id = origin.parent_channel_id if auth.platform == "slack" else origin.thread_id
    token = mint_request_token()
    now = datetime.now(UTC)
    expires_at = now + DEFAULT_TTL
    # The card says who will pick the work back up; an origin with no
    # responder name is the headless case, where the built-in agent's name is
    # the only honest thing to print.
    responder_name = origin.responder_name or "Daimon"
    async with runtime.session_factory.begin() as session:
        active_origin = await get_active_origin(
            session,
            origin_id=origin.id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=origin.platform,
            now=now,
            for_update=True,
        )
        if active_origin is None:
            raise ToolError(
                "This turn origin expired before the request; retry in that conversation."
            )
        retired = await _supersede_live_siblings(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            requester_platform_user_id=requester_platform_user_id,
            origin_thread_id=origin.thread_id,
            now=now,
        )
        await create_credential_request(
            session,
            token=token,
            kind=kind,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            account_id=auth.account_id,
            target=target,
            mcp_server_url=mcp_server_url,
            requester_platform_user_id=requester_platform_user_id,
            channel_id=channel_id,
            expires_at=expires_at,
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=str(ma_agent.id),
            target_name=ma_agent.name,
            requested_work=requested_work,
            responder_name=responder_name,
            replaces_updated_at=replaces_updated_at,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            origin_thread_id=origin.thread_id,
        )
    try:
        if auth.platform == "slack":
            message_id = await _post_slack_credential_button_impl(
                runtime,
                auth,
                channel_id=channel_id,
                thread_ts=origin.thread_id,
                kind=kind,
                target=target,
                token=token,
                agent_name=agent_name,
                purpose=purpose,
                expires_at=expires_at,
                responder_name=responder_name,
                mcp_server_url=mcp_server_url,
                branch=branch,
            )
        else:
            message_id = await _post_credential_button_impl(
                runtime,
                auth,
                channel_id=channel_id,
                kind=kind,
                target=target,
                token=token,
                agent_name=agent_name,
                purpose=purpose,
                expires_at=expires_at,
                responder_name=responder_name,
                mcp_server_url=mcp_server_url,
                branch=branch,
            )
    except ToolError as exc:
        # The row already exists (single-use + TTL bound it regardless), but
        # with no live button it is silently unusable — say so rather than
        # letting the caller believe the request succeeded.
        raise ToolError(
            f"credential request was created but posting the button failed: {exc}"
        ) from exc
    async with runtime.session_factory.begin() as session:
        await update_credential_request_message(session, token=token, posted_message_id=message_id)
    # Only now that the replacement is visible: an edit that lands first would
    # point the reader at a "newer form below" that does not exist yet.
    for old in retired:
        if auth.platform == "slack":
            await edit_slack_card_replaced(runtime, auth, row=old)
        else:
            await edit_discord_card_replaced(runtime, row=old)
    return RequestCredentialResult(
        kind=kind,
        target=target,
        message_id=message_id,
        instruction=_REPLY_POINTS_AT_THE_FORM,
    )


async def _request_agent_key_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    key: str | None,
    purpose: str,
    channel_id: str,
    pending_task: str | None = None,
    origin_context_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if key is not None and not _POSIX_KEY_RE.match(key):
        raise ToolError(
            "key must match [A-Za-z_][A-Za-z0-9_]* "
            "(letters, digits, underscores; must not start with a digit)"
        )
    if key is None:
        named = _named_single_key(purpose)
        if named is not None:
            raise ToolError(
                f"You named one key ({named}); pass it as `key` instead of requesting a file."
            )
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_id, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    # No key name means a whole-file import, which names no single key and
    # replaces nothing by compare-and-set: the file form merges.
    kind: CredentialRequestKind = "env_file" if key is None else "env"
    target = ENV_FILE_TARGET if key is None else key
    replaces_updated_at: datetime | None = None
    if key is not None:
        async with runtime.session_factory() as session:
            existing = await get_agent_file(
                session, tenant_id=auth.tenant_id, agent_id=agent_id, key=key
            )
        if existing is not None:
            await _require_key_replacement_allowed(runtime, auth, ma_agent=ma_agent, key=key)
            # The card promises "the value as it stands right now"; the
            # submit path compares against this timestamp and refuses a
            # write that would clobber someone else's later change.
            replaces_updated_at = existing.updated_at
    return await _mint_and_post(
        runtime,
        auth,
        kind=kind,
        target=target,
        mcp_server_url=None,
        agent_id=agent_id,
        ma_agent=ma_agent,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
        origin=origin,
        requested_work=_bounded_pending_task(pending_task, echoes=(agent_name, target)),
        replaces_updated_at=replaces_updated_at,
    )


def _reject_reserved_server(runtime: McpRuntime, *, server_name: str, url: str) -> None:
    """The deployment's own daimon-mcp entry can never be the target of a credential.

    Same gate as `attach_mcp_server`: a grant or token stored at the public URL
    would take the slot the per-agent JWT needs, and the vault bootstrap
    would 409 on every session create with nothing to heal it.
    """
    public_url = runtime.settings.mcp.public_url
    rejection = get_reserved_mcp_rejection(
        server_name=server_name,
        url=url,
        public_url=str(public_url) if public_url is not None else None,
    )
    if rejection is not None:
        raise ToolError(rejection)


async def _request_mcp_token_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    channel_id: str,
    pending_task: str | None = None,
    origin_context_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if urlparse(url).scheme not in ("http", "https"):
        raise ToolError("mcp server url must be http or https")
    try:
        assert_public_host(url, what="mcp server url")
    except McpUrlError as err:
        raise ToolError(str(err)) from err
    _reject_reserved_server(runtime, server_name=server_name, url=url)
    # Normalise the trailing slash once, here, before the URL is persisted.
    # The vault stores it as the credential's `auth.mcp_server_url` and
    # mcp_vault's idempotent replace matches on that string exactly, so
    # `…/mcp/` and `…/mcp` are two credentials for one server — a re-paste
    # would stack rather than replace. Observed live: request row held the
    # slashed form while the vault held the bare one.
    url = url.rstrip("/")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_id, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    return await _mint_and_post(
        runtime,
        auth,
        kind="mcp",
        target=server_name,
        mcp_server_url=url,
        agent_id=agent_id,
        ma_agent=ma_agent,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=f"connecting the MCP server '{server_name}'",
        channel_id=channel_id,
        origin=origin,
        requested_work=_bounded_pending_task(pending_task, echoes=(agent_name, server_name)),
    )


async def _request_mcp_oauth_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    channel_id: str,
    pending_task: str | None = None,
    origin_context_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    # OAuth only ever happens over TLS: the code and the tokens travel in the
    # browser and the callback, and no authorization server accepts a plain
    # http redirect target.
    if urlparse(url).scheme != "https":
        raise ToolError("an OAuth MCP server url must be https")
    try:
        assert_public_host(url, what="mcp server url")
    except McpUrlError as err:
        raise ToolError(str(err)) from err
    _reject_reserved_server(runtime, server_name=server_name, url=url)
    url = url.rstrip("/")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_id, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    return await _mint_and_post(
        runtime,
        auth,
        kind="mcp_oauth",
        target=server_name,
        mcp_server_url=url,
        agent_id=agent_id,
        ma_agent=ma_agent,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=f"connecting the MCP server '{server_name}' with your account",
        channel_id=channel_id,
        origin=origin,
        requested_work=_bounded_pending_task(pending_task, echoes=(agent_name, server_name)),
    )


async def _request_skill_repo_token_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    repo_url: str,
    branch: str,
    path: str,
    purpose: str,
    channel_id: str,
    pending_task: str | None = None,
    origin_context_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if urlparse(repo_url).scheme not in ("http", "https"):
        raise ToolError("repo url must be http or https, e.g. https://github.com/owner/repo")
    if not _OWNER_REPO_RE.fullmatch(normalize_owner_repo(repo_url)):
        raise ToolError(
            "repo url must name exactly one owner/repo, e.g. https://github.com/owner/repo"
        )
    # "@" and "#" are the packing delimiters; a branch or path carrying one
    # would round-trip as a different repo, so refuse rather than mangle.
    if "@" in branch or "#" in branch:
        raise ToolError("branch must not contain '@' or '#'")
    if "#" in path:
        raise ToolError("path must not contain '#'")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_id, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    return await _mint_and_post(
        runtime,
        auth,
        kind="skill_repo",
        target=build_skill_repo_target(repo_url, branch, path),
        mcp_server_url=None,
        agent_id=agent_id,
        ma_agent=ma_agent,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
        origin=origin,
        requested_work=_bounded_pending_task(pending_task, echoes=(agent_name, repo_url)),
        branch=branch,
    )


async def _request_repo_binding_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    repo_url: str,
    purpose: str,
    channel_id: str,
    branch: str = "main",
    pending_task: str | None = None,
    origin_context_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if urlparse(repo_url).scheme not in ("http", "https"):
        raise ToolError("repo url must be http or https, e.g. https://github.com/owner/repo")
    if not _OWNER_REPO_RE.fullmatch(normalize_owner_repo(repo_url)):
        raise ToolError(
            "repo url must name exactly one owner/repo, e.g. https://github.com/owner/repo"
        )
    # Same delimiter rule `request_skill_repo_token` applies: the branch rides
    # in the packed `target`, so a branch carrying one would round-trip as a
    # different repo.
    if "@" in branch or "#" in branch:
        raise ToolError("branch must not contain '@' or '#'")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_id, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    return await _mint_and_post(
        runtime,
        auth,
        kind="repo",
        target=build_skill_repo_target(repo_url, branch, ""),
        mcp_server_url=None,
        agent_id=agent_id,
        ma_agent=ma_agent,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
        origin=origin,
        requested_work=_bounded_pending_task(pending_task, echoes=(agent_name, repo_url)),
        branch=branch,
    )


def register_credential_request_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_agent_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        purpose: str,
        channel_id: Annotated[
            str,
            Field(description="Compatibility field; origin controls the posting destination."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        key: Annotated[
            str | None,
            Field(
                description=(
                    "The exact key name (e.g. TOGGL_API_TOKEN). Pass it whenever the "
                    "person named or implied ONE key. Omit ONLY when they want to "
                    "upload a .env file holding several keys."
                )
            ),
        ] = None,
        pending_task: Annotated[str | None, Field(description=_PENDING_TASK_DESCRIPTION)] = None,
    ) -> RequestCredentialResult:
        """Give an agent an API key or token for any service: Toggl, OpenAI,
        Higgsfield, or a platform that just launched. Unknown services work too.

        For an inventory question, use ``list_agent_keys`` with the requested agent's
        name. This tool posts an input form; it does not inspect existing keys.
        Target the agent the user named, which may differ from the answering agent.

        Never accept secret values in chat; ask for rotation if pasted. One named key
        → pass `key`. To load, upload or import a whole `.env` file of several keys at
        once, omit it. For MCP credentials use ``request_mcp_token``; GitHub access
        uses ``request_repo_binding``.

        Posts a card naming the agent and the key. Only the requester can open its
        private form; it expires in 30 minutes. Values never appear in chat. Anyone
        who talks to the agent can use added keys. Members can add new keys, including
        to built-in Daimon; replacing one a shared agent already has needs an admin.
        Pass the waiting task as `pending_task` so it resumes after the value is
        saved; call this before any clarifying question about that task, even when
        the task is underspecified."""
        return await _request_agent_key_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            key=key,
            purpose=purpose,
            channel_id=channel_id,
            pending_task=pending_task,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_mcp_token(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: Annotated[
            str, Field(description="Connection name; reusing a name replaces that server entry.")
        ],
        url: Annotated[
            str,
            Field(
                description="MCP endpoint URL accepting bearer authentication, e.g. https://mcp.example.com/mcp."
            ),
        ],
        channel_id: Annotated[
            str,
            Field(description="Compatibility field; origin controls the posting destination."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        pending_task: Annotated[str | None, Field(description=_PENDING_TASK_DESCRIPTION)] = None,
    ) -> RequestCredentialResult:
        """Connect an agent such as research-bot to Linear or GitHub through an MCP
        endpoint with a bearer token, not browser OAuth. For a server that only
        signs people in through a browser (Notion, Slack, Atlassian) use
        ``request_mcp_oauth`` instead; such servers reject a pasted key.
        Match supported authentication; an API key is not automatically an MCP token.

        Use ``attach_mcp_server`` for public servers without tokens. Never accept
        credentials in chat. Members can use this form on shared agents and built-in
        Daimon; the admin and fork gates for direct spec edits do not apply.

        Posts a requester-only card naming the agent and the server, opening a private
        form, expiring in 30 minutes. Submission attaches the server to the agent, not
        this session's toolset. Check tool availability before promising use here.
        Values never appear in chat; everyone talking to the agent can use the
        connection. Pass the waiting task as `pending_task` so it resumes after the
        value is saved."""
        return await _request_mcp_token_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            channel_id=channel_id,
            pending_task=pending_task,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_mcp_oauth(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: Annotated[
            str, Field(description="Connection name; reusing a name replaces that server entry.")
        ],
        url: Annotated[
            str,
            Field(
                description="MCP endpoint URL that signs people in through OAuth, e.g. https://mcp.notion.com/mcp."
            ),
        ],
        channel_id: Annotated[
            str,
            Field(description="Compatibility field; origin controls the posting destination."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        pending_task: Annotated[str | None, Field(description=_PENDING_TASK_DESCRIPTION)] = None,
    ) -> RequestCredentialResult:
        """Connect an agent to an MCP server that signs people in through the browser,
        such as Notion, Slack or Atlassian. Each person connects their own account:
        the grant is theirs alone, and other members connect separately when asked.

        Use ``request_mcp_token`` for servers that take a pasted bearer token and
        ``attach_mcp_server`` for public servers. Members can use this on shared
        agents and built-in Daimon; the admin and fork gates for direct spec edits
        do not apply.

        Posts a requester-only card; its button opens a private sign-in link that
        expires in ten minutes. Finishing sign-in stores the grant for that person
        and attaches the server to the agent, not this session's toolset. Check
        tool availability before promising use here. Pass the waiting task as
        `pending_task` so it resumes after the connection is made."""
        return await _request_mcp_oauth_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            channel_id=channel_id,
            pending_task=pending_task,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_skill_repo_token(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        repo_url: Annotated[
            str,
            Field(description="GitHub repo or repository URL, e.g. https://github.com/owner/repo."),
        ],
        purpose: str,
        channel_id: Annotated[
            str,
            Field(description="Compatibility field; origin controls the posting destination."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        branch: Annotated[
            str, Field(description="Branch the skills are read from, e.g. main.")
        ] = "main",
        path: str = "",
        pending_task: Annotated[str | None, Field(description=_PENDING_TASK_DESCRIPTION)] = None,
    ) -> RequestCredentialResult:
        """The skills repo is private: collect a GitHub token to import its skills.

        After ``sync_skills`` cannot read a private skill repository, use this form.
        The skill repo is separate from the working repo; for that one use
        ``request_repo_binding``.

        Posts a requester-only card naming the agent and the repo, expiring in 30
        minutes. Its private form retries import and attachment; tokens never appear
        in chat. Anyone talking to the agent can use the imported skills. Pass the
        same repo URL, branch and path, and the waiting task as `pending_task` so it
        resumes after the value is saved."""
        return await _request_skill_repo_token_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            branch=branch,
            path=path,
            purpose=purpose,
            channel_id=channel_id,
            pending_task=pending_task,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        repo_url: Annotated[
            str,
            Field(description="GitHub repo or repository URL, e.g. https://github.com/owner/repo."),
        ],
        purpose: str,
        channel_id: Annotated[
            str,
            Field(description="Compatibility field; origin controls the posting destination."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        branch: Annotated[
            str, Field(description="Branch the agent checks out, e.g. main.")
        ] = "main",
        pending_task: Annotated[str | None, Field(description=_PENDING_TASK_DESCRIPTION)] = None,
    ) -> RequestCredentialResult:
        """Let an agent read a GitHub working repo or repository, public or private.

        For a private skill repo use ``request_skill_repo_token``. If the user has
        no working token, ``post_github_app_install_link`` offers a GitHub App install;
        installing alone does not bind the repo or verify this tenant's access.

        Posts a requester-only card naming the agent and the repo, expiring in 30
        minutes. Only the requester can open its private form, which collects a GitHub
        token only when needed; values never appear in chat. Saving binds the working
        repository on `branch` for future sessions. Existing working tokens remain in
        use. Pass the waiting task as `pending_task` so it resumes after the value is
        saved."""
        return await _request_repo_binding_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            purpose=purpose,
            channel_id=channel_id,
            branch=branch,
            pending_task=pending_task,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )
