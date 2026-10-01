"""post_wizard: the agent-facing tool that posts a multi-step form.

Deliberately NOT admin-gated — a form is an ordinary conversational
capability, not a chat-mutation admin action, mirroring
``credential_requests.py``'s rationale for the same omission.

No other tool in this server carries an end-your-turn contract. Everywhere
else, the tool's return value is simply data and the agent decides what to
do next. Here, the return value IS an instruction: posting a wizard means
the agent's turn is over, because the user's answers arrive later as a
brand-new message (a keyed block resolved by the turn pipeline), not as
anything this tool call can return synchronously. In the system this
behaviour is ported from, the tool result's instruction alone was found
insufficient — models kept narrating past it or calling another tool — which
is why the seeded agent prompt (``defaults/agents/daimon.yaml``) carries a
matching, reinforcing line rather than relying on this docstring alone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord import (
    _post_wizard_impl as _post_wizard_discord_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.stores.wizard_session import create_wizard_session
from daimon.core.wizard.expiry import derive_expires_at
from daimon.core.wizard.spec import Step, WizardSpec
from daimon.core.wizard.state import WizardStatus, new_short_id
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_STOP_SIGNAL: Final[str] = (
    "WIZARD_POSTED: the form is now live in the channel. End your turn "
    "immediately — do not narrate, do not continue, and do not call another "
    "tool. The user's answers will arrive later as a brand-new message "
    "carrying a keyed block; that is when you resume, with the full history "
    "of having asked."
)


async def _post_wizard_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    prompt: str,
    steps: list[Step],
    channel_id: str,
) -> str:
    if auth.platform == "slack":
        raise ToolError("post_wizard is not supported on Slack yet")
    if auth.platform_user_id is None:
        raise ToolError("post_wizard requires a platform-bound identity")

    # `image_url` is server-populated: a caller-supplied value must never
    # reach the renderer, so it is stripped before validation even runs.
    stripped_steps = [step.model_copy(update={"image_url": None}) for step in steps]
    try:
        spec = WizardSpec(prompt=prompt, steps=stripped_steps)
    except ValueError as err:
        raise ToolError(
            f"Invalid wizard form: {err} Split it into a shorter follow-up form."
        ) from err

    short_id = new_short_id()

    # Post before insert: a failed post must leave no stored row nobody can
    # tap. `_post_wizard_discord_impl`'s ToolErrors already name the
    # channel/permission/handle problem, so they propagate unchanged.
    posted = await _post_wizard_discord_impl(
        runtime, auth, channel_id=channel_id, spec=spec, short_id=short_id
    )

    rebuilt_steps = [
        step.model_copy(update={"image_url": posted.image_urls[step.image_handle]})
        if step.image_handle is not None and step.image_handle in posted.image_urls
        else step
        for step in spec.steps
    ]
    rebuilt_spec = spec.model_copy(update={"steps": rebuilt_steps})

    now = datetime.now(UTC)
    expires_at = derive_expires_at(now=now, attachment_urls=list(posted.image_urls.values()))

    # If this insert fails after a successful post, let it propagate: the
    # agent sees an error, and the orphaned message is inert because no row
    # backs its custom_ids.
    async with runtime.session_factory.begin() as session:
        await create_wizard_session(
            session,
            short_id=short_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            requester_platform_user_id=auth.platform_user_id,
            channel_id=channel_id,
            message_id=posted.message_id,
            spec=rebuilt_spec.model_dump(mode="json"),
            answers={},
            current_step=0,
            status=WizardStatus.OPEN.value,
            expires_at=expires_at,
            now=now,
        )

    return _STOP_SIGNAL


def register_wizard_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def post_wizard(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        prompt: str,
        steps: list[Step],
        channel_id: str,
    ) -> str:
        """Post a multi-step form in the channel instead of asking in prose.

        Each entry in ``steps`` carries ``question`` (the text shown to the
        user), ``kind`` (``choice``, ``multi`` or ``text``), and ``options``
        for the two choice kinds. A step may carry one finished image by
        passing the file-store handle returned by the image-generation tool —
        never a URL, never a diagram source. Server channels and threads are
        supported; direct messages are not. A form that exceeds a platform
        ceiling (too many steps, too many options, too long a question)
        raises an error naming the offending step — split the form into a
        shorter follow-up and retry.

        AFTER CALLING THIS TOOL YOU MUST END YOUR TURN IMMEDIATELY. Do not
        narrate the form, do not continue the conversation, and do not call
        another tool. The user's answers arrive later as a brand-new message.
        """
        return await _post_wizard_impl(
            runtime,
            await _auth(ctx),
            prompt=prompt,
            steps=steps,
            channel_id=channel_id,
        )
