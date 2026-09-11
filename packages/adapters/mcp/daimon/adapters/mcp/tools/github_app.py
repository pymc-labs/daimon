"""GitHub App install-link tool: post_github_app_install_link.

Posts the configured App install URL through the caller's platform. No request
state, token or expiry is created. Discord opens the link directly; Slack
acknowledges its click payload without treating it as installation proof.

Ungated deliberately: posting a link has no blast radius inside daimon, and
GitHub itself enforces who may install an App on a given account or
organisation, so no admin tag and no admin gate are applied here.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import (
    _post_app_install_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._app_install_button import (
    _post_slack_app_install_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.github_app_auth import build_app_install_url
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class PostAppInstallLinkResult(BaseModel):
    """Result of posting the GitHub App install-link button.

    Neither platform supplies proof of installation from the link click.
    This result reports only
    that the invitation was posted: it carries no field named or meaning
    installed, success, completed, connected, or verified.
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str
    message_id: str
    install_url: str


async def _post_app_install_link_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    purpose: str,
) -> PostAppInstallLinkResult:
    if auth.platform_user_id is None:
        raise ToolError("posting the install link requires a platform-bound identity")
    slug = runtime.settings.github.app_slug
    if slug is None:
        raise ToolError(
            "this deployment has no GitHub App install link configured — "
            "tell the user an operator must configure it; a working GitHub token "
            "can still be supplied privately through request_repo_binding"
        )
    post_button = (
        _post_slack_app_install_button_impl
        if auth.platform == "slack"
        else _post_app_install_button_impl
    )
    message_id = await post_button(
        runtime,
        auth,
        channel_id=channel_id,
        slug=slug,
        purpose=purpose,
    )
    return PostAppInstallLinkResult(
        channel_id=channel_id,
        message_id=message_id,
        install_url=build_app_install_url(slug),
    )


def register_github_app_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack"})  # pyright: ignore[reportArgumentType]
    async def post_github_app_install_link(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        purpose: str,
    ) -> PostAppInstallLinkResult:
        """Install the GitHub App: post a link inviting the user to grant repository access.

        For a private repo with a working token, use ``request_repo_binding`` instead.
        A token remains the fallback and existing bound tokens keep being used.

        Posts a link button in this channel opening GitHub's install page. It cannot
        tell whether installation happened. Installing alone neither verifies this
        tenant's access nor binds a working repo; use ``request_repo_binding`` and
        verify access through the requested operation afterwards."""
        return await _post_app_install_link_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            purpose=purpose,
        )
