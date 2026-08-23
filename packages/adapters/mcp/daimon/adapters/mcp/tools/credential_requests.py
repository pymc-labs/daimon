"""Credential-request tools: request_env_credential, request_mcp_credential,
request_repo_binding.

These three tools replace the "go open /agent-setup" redirect for a task that
needs an env secret, an auth-required MCP server, or a repo bound to an
agent: the agent mints a single-use, TTL-bounded ``credential_requests`` row
and posts a target-naming button in the thread (``tools/discord/`` or
``tools/slack/_credential_button.py``, by the caller's platform). Clicking
the button opens a native modal in a different process (the platform bot,
worker VM) — the secret itself never travels through either process's tool
arguments, so
none of the three tools below has a token/secret/value parameter, and none
should ever be added.

Deliberately NOT admin-gated — the chat-mutation admin check other tools call
at the top of their impl is intentionally absent here. Authorization for the
actual credential write happens at click time instead, when the button's
click gate compares the clicking user against
``requester_platform_user_id`` on the minted row. This widens today's
admin-only credential writes (``/agent-setup``'s mutation buttons are
``is_admin``-only) in exchange for one-click UX — a deliberate, accepted
trade documented here so a future reviewer does not "fix" the omission by
adding that admin gate back.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import (
    _post_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._credential_button import (
    _post_slack_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.credential_requests import (
    DEFAULT_TTL,
    CredentialRequestKind,
    build_skill_repo_target,
    mint_request_token,
)
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.credential_requests import create_credential_request
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

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


class RequestCredentialResult(BaseModel):
    """Result of minting and posting a credential-request button."""

    model_config = ConfigDict(frozen=True)

    kind: CredentialRequestKind
    target: str
    expires_at: datetime
    message_id: str


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


async def _resolve_agent_uuid(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
) -> uuid.UUID:
    ma_agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if ma_agent is None:
        raise ToolError(f"agent '{agent_name}' not found in this tenant")
    return derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(ma_agent.id))


async def _mint_and_post(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    kind: CredentialRequestKind,
    target: str,
    mcp_server_url: str | None,
    agent_id: uuid.UUID,
    requester_platform_user_id: str,
    agent_name: str,
    purpose: str,
    channel_id: str,
) -> RequestCredentialResult:
    token = mint_request_token()
    expires_at = datetime.now(UTC) + DEFAULT_TTL
    async with runtime.session_factory.begin() as session:
        row = await create_credential_request(
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
        )
    post = (
        _post_slack_credential_button_impl
        if auth.platform == "slack"
        else _post_credential_button_impl
    )
    try:
        message_id = await post(
            runtime,
            auth,
            channel_id=channel_id,
            kind=kind,
            target=target,
            token=token,
            agent_name=agent_name,
            purpose=purpose,
        )
    except ToolError as exc:
        # The row already exists (single-use + TTL bound it regardless), but
        # with no live button it is silently unusable — say so rather than
        # letting the caller believe the request succeeded.
        raise ToolError(
            f"credential request was created but posting the button failed: {exc}"
        ) from exc
    return RequestCredentialResult(
        kind=kind, target=target, expires_at=row.expires_at, message_id=message_id
    )


async def _request_env_credential_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    key: str,
    purpose: str,
    channel_id: str,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if not _POSIX_KEY_RE.match(key):
        raise ToolError(
            "env key must match [A-Za-z_][A-Za-z0-9_]* "
            "(letters, digits, underscores; must not start with a digit)"
        )
    agent_id = await _resolve_agent_uuid(runtime, auth, agent_name)
    return await _mint_and_post(
        runtime,
        auth,
        kind="env",
        target=key,
        mcp_server_url=None,
        agent_id=agent_id,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
    )


async def _request_mcp_credential_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    server_name: str,
    url: str,
    channel_id: str,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if urlparse(url).scheme not in ("http", "https"):
        raise ToolError("mcp server url must be http or https")
    # Normalise the trailing slash once, here, before the URL is persisted.
    # The vault stores it as the credential's `auth.mcp_server_url` and
    # mcp_vault's idempotent replace matches on that string exactly, so
    # `…/mcp/` and `…/mcp` are two credentials for one server — a re-paste
    # would stack rather than replace. Observed live: request row held the
    # slashed form while the vault held the bare one.
    url = url.rstrip("/")
    agent_id = await _resolve_agent_uuid(runtime, auth, agent_name)
    return await _mint_and_post(
        runtime,
        auth,
        kind="mcp",
        target=server_name,
        mcp_server_url=url,
        agent_id=agent_id,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=f"connecting the MCP server '{server_name}'",
        channel_id=channel_id,
    )


# No `default_branch` parameter here: the credential_requests row has no
# column to hold one, and none is being added, so an argument accepted here
# would be silently discarded — the exact class of surface-that-lies this
# tool exists not to become. The modal collects the branch instead,
# defaulting to `main`.
async def _request_skill_repo_credential_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    repo_url: str,
    branch: str,
    path: str,
    purpose: str,
    channel_id: str,
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
    agent_id = await _resolve_agent_uuid(runtime, auth, agent_name)
    return await _mint_and_post(
        runtime,
        auth,
        kind="skill_repo",
        target=build_skill_repo_target(repo_url, branch, path),
        mcp_server_url=None,
        agent_id=agent_id,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
    )


async def _request_repo_binding_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    repo_url: str,
    purpose: str,
    channel_id: str,
) -> RequestCredentialResult:
    requester = _require_requestable_platform(auth)
    if urlparse(repo_url).scheme not in ("http", "https"):
        raise ToolError("repo url must be http or https, e.g. https://github.com/owner/repo")
    if not _OWNER_REPO_RE.fullmatch(normalize_owner_repo(repo_url)):
        raise ToolError(
            "repo url must name exactly one owner/repo, e.g. https://github.com/owner/repo"
        )
    agent_id = await _resolve_agent_uuid(runtime, auth, agent_name)
    return await _mint_and_post(
        runtime,
        auth,
        kind="repo",
        target=repo_url,
        mcp_server_url=None,
        agent_id=agent_id,
        requester_platform_user_id=requester,
        agent_name=agent_name,
        purpose=purpose,
        channel_id=channel_id,
    )


def register_credential_request_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def request_env_credential(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        key: str,
        purpose: str,
        channel_id: str,
    ) -> RequestCredentialResult:
        """Request an environment secret from the user via a private modal.

        Call this INSTEAD of ever accepting a secret value in chat. Posts a
        button in the thread naming the exact env key; clicking it opens a
        private form where the user enters the value — it
        never appears in this channel or in your context. Once added, the
        credential becomes usable by everyone who talks to ``agent_name``.
        """
        return await _request_env_credential_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            key=key,
            purpose=purpose,
            channel_id=channel_id,
        )

    @mcp.tool
    async def request_mcp_credential(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        server_name: str,
        url: str,
        channel_id: str,
    ) -> RequestCredentialResult:
        """Request an auth-required MCP server's credential via a private modal.

        Call this INSTEAD of ever accepting a token value in chat, and instead
        of ``attach_mcp_server`` when the server requires authentication.
        Posts a button in the thread naming the exact server; clicking it
        opens a private form where the user enters the token —
        it never appears in this channel or in your context.
        Once added, the credential becomes usable by everyone who talks to
        ``agent_name``.
        """
        return await _request_mcp_credential_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            channel_id=channel_id,
        )

    # The docstring below deliberately does NOT name the skill-import tool.
    # That tool is admin-gated; this one is not (matching its sibling
    # `request_*` tools), so naming it would disclose a gated tool's existence
    # to every principal that can discover this one —
    # `test_chat_session_tool_reachability` asserts against exactly that by
    # searching the rendered tool text for the gated name. Kept as a comment
    # rather than a docstring paragraph because the docstring IS prompt
    # context: it is rendered into every model's tool list, where an internal
    # rationale is noise.
    @mcp.tool
    async def request_skill_repo_credential(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        repo_url: str,
        purpose: str,
        channel_id: str,
        branch: str = "main",
        path: str = "",
    ) -> RequestCredentialResult:
        """Ask for a GitHub token so a skill import can read a PRIVATE repo.

        Call this when importing skills from a repo reports that no
        credential was available — NOT ``request_repo_binding``. Importing
        skills from a repo and binding a repo for the agent to check out are
        different things: this one writes no ``agent_repo_binding`` row, so
        it will not make the agent start cloning the skill repo. The stored
        token is shared between the two, so a user who has already bound a
        repo with a token that can also read this one will not be asked
        twice.

        Pass the same repo url, branch, and path the failed import used —
        they are carried through the click, and the import re-runs
        automatically on submit, so there is nothing to call afterwards.
        """
        return await _request_skill_repo_credential_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            branch=branch,
            path=path,
            purpose=purpose,
            channel_id=channel_id,
        )

    @mcp.tool
    async def request_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        repo_url: str,
        purpose: str,
        channel_id: str,
    ) -> RequestCredentialResult:
        """Request a repo binding for an agent via a private modal.

        Call this INSTEAD of ever telling the user to open ``/agent-setup``
        to bind a repository. Posts a button in the thread naming the exact
        repo and agent; clicking it opens a private form where the
        user confirms the branch and, only if the repo is private and not
        otherwise readable, pastes a GitHub token that never appears in this
        channel or in your context.
        """
        return await _request_repo_binding_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            purpose=purpose,
            channel_id=channel_id,
        )
