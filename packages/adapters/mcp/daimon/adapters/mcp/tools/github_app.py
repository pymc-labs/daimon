"""GitHub App install-link tool: post_github_app_install_link.

Makes an existing GitHub App installation reachable from chat. A link button
carries a URL and no custom id, so nothing dispatches it in the bot process
and no request state is created anywhere — Discord opens the URL directly.
This tool introduces no credential-request row, no minted token, no expiry,
and no dynamic item; see ``tools/discord/_app_install_button.py`` for the
posting path this delegates to.

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
from daimon.core.github_app_auth import build_app_install_url
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class PostAppInstallLinkResult(BaseModel):
    """Result of posting the GitHub App install-link button.

    A link button returns no signal when clicked — Discord opens the URL
    itself, and nothing dispatches back to daimon. This result reports only
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
    if auth.platform == "slack":
        raise ToolError("post_github_app_install_link is not supported on Slack yet")
    if auth.platform_user_id is None:
        raise ToolError("posting the install link requires a platform-bound identity")
    slug = runtime.settings.github.app_slug
    if slug is None:
        raise ToolError(
            "this deployment has no GitHub App install link configured — "
            "an operator must set DAIMON_GITHUB__APP_SLUG"
        )
    message_id = await _post_app_install_button_impl(
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
    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def post_github_app_install_link(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        purpose: str,
    ) -> PostAppInstallLinkResult:
        """Post a button inviting the user to install daimon's GitHub App.

        Call this when the user wants daimon to reach a private repository
        and has no token to paste, or when a sync failed because the repo is
        not readable. Posts a button in the thread; clicking it opens the
        install page on GitHub, where the user chooses which repositories to
        grant access to.

        This tool CANNOT tell whether the install happened — a link button
        gives no signal back. After the user says they installed it, verify
        by attempting the sync again: the installation is resolved live at
        that point.
        """
        return await _post_app_install_link_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            purpose=purpose,
        )
