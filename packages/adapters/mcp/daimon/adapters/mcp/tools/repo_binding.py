"""Bind an agent to a public GitHub repo from inside an ordinary chat turn.

`request_repo_binding` posts a requester-only private form because a private
repo needs a token nobody may type in chat. A public repo needs no secret at
all, so that whole card round trip buys nothing here: the only facts to
establish are that the repo really is public and that this caller may change
this agent's working repo. Both are checked before anything is written, and
the binding lands in the same turn that asked for it.

The sibling write for an agent's own repo is `self_edit`'s `set_repo_binding`,
which is `agent-chat`-tagged and so is not reachable from a person's
conversation. This tool is the chat-callable one; it mints no credential,
because an `anon:` binding is exactly what a verified-public repo warrants.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access

# The URL-shape rule and the agent resolution are the ones `request_repo_binding`
# already applies; imported rather than re-spelled so the two chat entry points
# can never disagree about what counts as one repo or which agent a name means.
from daimon.adapters.mcp.tools.credential_requests import (
    _OWNER_REPO_RE,  # pyright: ignore[reportPrivateUsage]
    _resolve_agent_uuid,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.tools.task_continuity import (
    _REPLY_VERBATIM,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.continuity.tool_messages import render_tool_unsaved_work_question
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import is_public_repo
from daimon.core.operation_policy import TargetFacts, decide_operation, needs_reachability_read
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import RepoAccessProof
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from daimon.core.stores.thread_sessions import get_live_thread_session, set_pending_unsaved_work
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

#: The `ma_secret_ref` a verified-public binding carries. The operator's
#: fallback token only ever clones `anon:` bindings, which is why nothing but a
#: repo proven public at bind time may be written with this ref.
_ANON_SECRET_REF = "anon:"


class RepoBindResult(BaseModel):
    """Result of binding an agent to a public repo. Carries no secret."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    repo_url: str
    """The canonical `owner/repo` that was stored."""
    branch: str
    confirmation: str
    """Final person-facing copy. Relay it verbatim."""
    instruction: str
    """What to do with `confirmation`."""


def _refusal(agent_name: str, owner_repo: str) -> str:
    """`ToolError` text: this caller may not change a shared agent's working repo."""
    return "\n".join(
        [
            f"Giving '{agent_name}' a working repo needs a workspace or server admin, "
            "and the caller is not one.",
            f"Tell them an admin can ask Daimon: give {agent_name} access to {owner_repo}.",
            "Nothing was changed. Do not retry.",
        ]
    )


async def _bind_public_repo_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    repo_url: str,
    branch: str,
    origin_context_id: str,
    expected_ma_agent_id: str,
    unsaved_work: Literal["copy", "leave"] | None,
    http_client: httpx.AsyncClient | None = None,
) -> RepoBindResult:
    """Point one agent's working repo at a verified-public GitHub repo.

    Order, each step short-circuiting before the next:

    1. The turn origin must be live and bound to this caller — the same trust
       root `request_repo_binding` and `hand_off_task` use.
    2. Resolve the named agent to a concrete MA identity, so a recreated
       namesake cannot inherit a bind aimed at its predecessor.
    3. Decide the permission BEFORE any GitHub call. A refused caller must not
       be able to use this tool as an oracle for whether a repo exists, and a
       refusal that costs a network round trip is a refusal that can be made
       to cost the deployment something.
    4. URL and branch shape, so what is probed is exactly what is stored.
    5. The repo must be verifiably public. `anon:` bindings are what the
       operator's fallback token clones, so a private repo bound as `anon:`
       would be a cross-tenant read waiting to happen.
    6. Ask once about uncommitted work, if this caller is already working in a
       different repo. This is a refusal the model answers and retries with,
       the same shape `hand_off_task` uses.
    7. One transaction for the answer and the binding.

    `http_client` is optional: when None this creates and closes its own client
    around the visibility probe only. Callers pass one only from tests that
    need to inject a mock transport.
    """
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    agent_uuid, ma_agent = await _resolve_agent_uuid(
        runtime, auth, agent_name, expected_ma_agent_id, origin
    )
    await require_pin_write_access(runtime, auth, ma_agent=ma_agent, origin=origin)

    is_daimon_managed = ma_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    reachable = False
    if needs_reachability_read(
        "repo_bind", is_admin=auth.is_admin, is_daimon_managed=is_daimon_managed
    ):
        async with runtime.session_factory() as session:
            reachable = await is_agent_reachable_in_tenant(
                session,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
                default=runtime.deployment_default,
            )
    outcome = decide_operation(
        "repo_bind",
        is_admin=auth.is_admin,
        target=TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=reachable),
    )
    if outcome != "allow":
        raise ToolError(_refusal(agent_name, normalize_owner_repo(repo_url)))

    if urlparse(repo_url).scheme not in ("http", "https"):
        raise ToolError("repo url must be http or https, e.g. https://github.com/owner/repo")
    owner_repo = normalize_owner_repo(repo_url)
    if not _OWNER_REPO_RE.fullmatch(owner_repo):
        raise ToolError(
            "repo url must name exactly one owner/repo, e.g. https://github.com/owner/repo"
        )
    # "@" and "#" are the packing delimiters a branch round-trips through
    # elsewhere; a branch carrying one would come back as a different repo.
    if "@" in branch or "#" in branch:
        raise ToolError("branch must not contain '@' or '#'")

    if http_client is not None:
        public = await is_public_repo(http_client, owner_repo=owner_repo)
    else:
        async with httpx.AsyncClient() as owned_client:
            public = await is_public_repo(owned_client, owner_repo=owner_repo)
    if not public:
        raise ToolError(
            f"{owner_repo} is not a public GitHub repo, or it does not exist, so it cannot "
            "be connected without access. Tell the caller that, and use request_repo_binding "
            "to collect access to a private repo privately. Nothing was changed."
        )

    async with runtime.session_factory() as session:
        live_session = await get_live_thread_session(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
        )
    # We cannot know whether the checkout is dirty without spending a turn in
    # it, so the rule is the same one `hand_off_task` uses: ask only when the
    # change could lose something — the caller is already working in some
    # other repo — and let the answer ride the retry. Re-binding the repo
    # already mounted replaces no checkout and is never interrupted.
    mounted = (
        live_session.effective_config.repo_url
        if live_session is not None and live_session.effective_config is not None
        else None
    )
    if mounted is not None and normalize_owner_repo(mounted) != owner_repo and unsaved_work is None:
        raise ToolError(render_tool_unsaved_work_question(mounted))

    now = datetime.now(UTC)
    async with runtime.session_factory.begin() as session:
        if unsaved_work is not None and live_session is not None:
            # The answer outlives this turn: the checkout it governs is built
            # at this caller's next message. Written in the same transaction
            # as the binding so a bound repo can never be paired with a lost
            # answer.
            await set_pending_unsaved_work(session, id=live_session.id, choice=unsaved_work)
        row = await set_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_uuid,
            repo_url=repo_url,
            default_branch=branch,
            ma_secret_ref=_ANON_SECRET_REF,
            proof=RepoAccessProof(kind="public", at=now, account_id=auth.account_id),
        )

    return RepoBindResult(
        agent_name=agent_name,
        repo_url=row.repo_url,
        branch=row.default_branch,
        # `unsaved_work=None` even when the caller answered: any copying
        # happens while the next checkout is built, and is reported with the
        # count it actually copied. Claiming it here would be claiming a
        # result that has not happened yet.
        confirmation=render_change_confirmation(
            ConfigurationChange(
                target_name=agent_name,
                kind="repo",
                availability="next_message",
                repo=owner_repo,
                branch=branch,
                unsaved_work=None,
            )
        ),
        instruction=_REPLY_VERBATIM,
    )


def register_repo_binding_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def bind_public_repo(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        repo_url: Annotated[
            str,
            Field(description="Public repository URL, e.g. https://github.com/owner/repo."),
        ],
        origin_context_id: str,
        expected_ma_agent_id: str,
        branch: Annotated[
            str, Field(description="Branch to check out; 'main' unless the person named one.")
        ] = "main",
        unsaved_work: Annotated[
            Literal["copy", "leave"] | None,
            Field(description="Only after the person answers the uncommitted-work question."),
        ] = None,
    ) -> RepoBindResult:
        """Have an agent work in a public GitHub repo: "work in
        github.com/owner/project", "point it at our open-source repository".

        Only for a repository that needs no token to clone; this checks, and
        refuses otherwise. When a token is needed, use ``request_repo_binding``,
        which collects one privately.

        The agent checks it out and works in it from your next message here, and
        anyone who talks to it works in it too. Files already open in this
        conversation are carried across or left behind according to the answer
        you get. Binding an agent that answers a channel or the whole workspace
        needs an admin."""
        return await _bind_public_repo_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            branch=branch,
            origin_context_id=origin_context_id,
            expected_ma_agent_id=expected_ma_agent_id,
            unsaved_work=unsaved_work,
        )
