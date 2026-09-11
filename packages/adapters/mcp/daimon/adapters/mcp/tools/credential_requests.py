"""Post requester-only private forms for agent keys, MCP tokens and GitHub access.

These tools create single-use, expiring request rows and post a target-naming
card through the caller's platform. Secret values never enter tool arguments.
Submission checks requester identity; these enrollment paths deliberately do
not inherit the admin gate for direct agent-spec mutations.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated
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
from pydantic import BaseModel, ConfigDict, Field

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


async def _request_agent_key_impl(
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
            "key must match [A-Za-z_][A-Za-z0-9_]* "
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


async def _request_mcp_token_impl(
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
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def request_agent_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        key: Annotated[
            str,
            Field(
                description=(
                    "Stored environment variable name, UPPER_SNAKE, e.g. TOGGL_TOKEN "
                    "or OPENAI_API_KEY; never the secret value."
                )
            ),
        ],
        purpose: str,
        channel_id: Annotated[
            str,
            Field(
                description="Channel where the requester-only private-input card will be posted."
            ),
        ],
    ) -> RequestCredentialResult:
        """Give an agent an API key or token for any service: Toggl, OpenAI,
        Higgsfield, or a platform that just launched. Unknown services work too.

        Never accept secret values in chat; ask for rotation if pasted. For MCP
        credentials use ``request_mcp_token``; GitHub access uses ``request_repo_binding``.
        To load .env keys, request each key separately; whole-file import is unavailable.

        Posts a card in this channel. Only the requester can open its private form;
        it expires in 30 minutes. Values never appear in chat. Anyone who talks to
        the agent can use added keys, including on Daimon itself without a fork."""
        return await _request_agent_key_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            key=key,
            purpose=purpose,
            channel_id=channel_id,
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
            Field(
                description="Channel where the requester-only private-input card will be posted."
            ),
        ],
    ) -> RequestCredentialResult:
        """Connect an agent such as research-bot to Linear, Notion or GitHub through an
        MCP endpoint with a bearer token. Match the endpoint's supported authentication;
        an API key is not automatically an MCP token, and this does not complete OAuth.

        Use ``attach_mcp_server`` for public servers without tokens. Never accept
        credentials in chat. Members can use this form on shared agents and built-in
        Daimon; the admin and fork gates for direct spec edits do not apply.

        Posts a requester-only card in this channel, expiring in 30 minutes. The
        private form collects the token and attaches the server. Values never appear
        in chat; everyone talking to the agent can use the connection."""
        return await _request_mcp_token_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            server_name=server_name,
            url=url,
            channel_id=channel_id,
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
            Field(
                description="Channel where the requester-only private-input card will be posted."
            ),
        ],
        branch: str = "main",
        path: str = "",
    ) -> RequestCredentialResult:
        """The skills repo is private: collect a GitHub token to import its skills.

        After ``sync_skills`` cannot read a private skill repository, use this form.
        For a working repo use ``request_repo_binding``. Currently this enrollment
        also changes the working-repo binding and therefore what the agent clones.

        Posts a requester-only card, expiring in 30 minutes. Its private form retries
        import and attachment; tokens never appear in chat. Anyone talking to the
        agent can use the imported skills. Pass the same repo URL, branch and path."""
        return await _request_skill_repo_token_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            branch=branch,
            path=path,
            purpose=purpose,
            channel_id=channel_id,
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
            Field(
                description="Channel where the requester-only private-input card will be posted."
            ),
        ],
    ) -> RequestCredentialResult:
        """Let an agent read a GitHub working repo or repository, public or private.

        For a private skill repo use ``request_skill_repo_token``. If the user has
        no working token, ``post_github_app_install_link`` offers a GitHub App install;
        installing alone does not bind the repo or verify this tenant's access.

        Posts a requester-only card in this channel, expiring in 30 minutes. The
        private form confirms the branch and collects a GitHub token only when needed;
        values never appear in chat. Saving binds the working repository for future
        sessions. Existing working tokens remain in use."""
        return await _request_repo_binding_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            repo_url=repo_url,
            purpose=purpose,
            channel_id=channel_id,
        )
