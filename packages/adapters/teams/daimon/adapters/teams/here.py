"""The `here` command: who answers where it was typed, what it can read and holds.

Mirrors Discord's and Slack's `/here` with the shared card
(`daimon.core.here_card`). Typed in a channel post it describes that post's
thread, answered there for the sender only (or in the 1:1 chat); typed in the
chat it describes the chat, or the setup conversation it is switched into. The
bot heard the message, so it and the sender can both view the place.

Only the place itself counts as visible to the caller: listing which other
channels they can see takes Graph lookups per team, so an agent rule's other
channels stay unnamed rather than risk naming one they cannot see.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.adapters.teams.billing_panel import plain_name
from daimon.adapters.teams.card_actions import button, card_actor, heading, submitted_fields, toast
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.access_policy import Approvals
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.approval_admin import approval_admin
from daimon.core.approval_gate import set_approval_gate
from daimon.core.errors import DaimonError
from daimon.core.here_card import HereCard, load_here_card, render_here_card
from daimon.core.stores.identity import find_platform_principal
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import ActionSet, AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

TITLE = "Here"
_FAILED = "Something went wrong reading this place's settings. Try again later."


def here_card(
    card: HereCard, *, channel_id: str | None = None, thread_id: str | None = None
) -> AdaptiveCard:
    """The shared compact card, as literal text: names cannot format or mention."""
    shown = render_here_card(card)
    lines = [
        # The shared copy names Slack and Discord's command; Teams' is `setup`.
        *([shown.subline.replace("/agent-setup", "setup")] if shown.subline else []),
        *([f"Reading: {shown.reading}"] if shown.reading else []),
        *([f"Publishing: {shown.publishing}"] if shown.publishing else []),
        *shown.extras,
    ]
    body: list[CardElement] = [heading(plain_name(shown.title))]
    body += [TextBlock(text=plain_name(line), spacing="Small", wrap=True) for line in lines]
    if card.approval_gate_admin and channel_id:
        scopes = [("server", "Server", ""), ("channel", "Channel", channel_id)]
        if thread_id and thread_id != channel_id:
            scopes.append(("thread", "Thread", thread_id))
        for scope, label, target in scopes:
            body.append(TextBlock(text=f"{label} write approvals", wrap=True))
            body.append(
                ActionSet(
                    actions=[
                        button("here_approvals", title, value, scope=scope, target=target)
                        for title, value in (
                            ("Ask", "on"),
                            ("Allow writes", "off"),
                            ("Inherit", "inherit"),
                        )
                    ]
                )
            )
    return AdaptiveCard(body=body, fallback_text=TITLE)


async def _load(context: CommandContext) -> HereCard:
    runtime, asked = context.runtime, context.asked_in or context.inbound
    # A plain 1:1 chat is its own thread; only a post or a setup conversation is one.
    thread_id = asked.thread_id if asked.thread_id != asked.channel_id else None
    public_url = runtime.settings.mcp.public_url
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=context.tenant_id, platform="teams", external_id=asked.user_id
        )
        return await load_here_card(
            session,
            runtime.anthropic,
            tenant_id=context.tenant_id,
            platform="teams",
            channel_id=asked.channel_id,
            thread_id=thread_id,
            is_dm=asked.kind == "dm",
            default=runtime.deployment_default,
            github=GitHubDeploymentFacts(
                app_configured=runtime.settings.github_app.app_id is not None
                and runtime.settings.github_app.private_key is not None,
            ),
            gate_overrides_active=(
                runtime.settings.tool_safety.enabled
                and runtime.settings.tool_safety.allow_gate_overrides
            ),
            public_mcp_url=str(public_url) if public_url is not None else None,
            # Only the caller sees the answer, so an admin may see admin views.
            is_admin=context.is_admin,
            caller_account_id=principal.account_id if principal else None,
            visible_channel_ids={asked.channel_id},
            bot_can_view=True,
            caller_can_view=True,
        )


async def show_here(context: CommandContext) -> None:
    try:
        asked = context.asked_in or context.inbound
        card = here_card(
            await _load(context),
            channel_id=asked.channel_id if asked.kind != "dm" else None,
            thread_id=asked.thread_id if asked.kind == "channel" else None,
        )
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.here.failed", exc_info=exc)
        text = str(exc) if isinstance(exc, DaimonError) else _FAILED
        card = AdaptiveCard(body=[heading(TITLE), TextBlock(text=text, wrap=True)])
    await context.send_card(card)


class HereApprovals:
    def __init__(self, runtime: TeamsRuntime) -> None:
        self.runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self.runtime, ctx.activity, remember_name=False)
        if actor is None:
            return toast("Cannot verify this administrator.")
        data = submitted_fields(ctx.activity.value.action.data)
        raw = data.get("op")
        scope = data.get("scope")
        target = str(data.get("target") or "")
        if scope not in {"server", "channel", "thread"}:
            return toast("Invalid approval scope.")
        value: Approvals
        if raw == "on":
            value = "on"
        elif raw == "off":
            value = "off"
        elif raw == "inherit":
            value = "inherit"
        else:
            return toast("Invalid approval setting.")
        if scope != "server" and not target:
            return toast("Missing approval scope.")
        subject = await approval_admin(
            self.runtime.sessionmaker,
            self.runtime.settings,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
        )
        try:
            await set_approval_gate(
                self.runtime.sessionmaker,
                tenant_id=actor.tenant_id,
                platform="teams",
                target=None if scope == "server" else target,
                value=value,
                subject=subject,
            )
        except DaimonError as exc:
            return toast(str(exc))
        if not (
            self.runtime.settings.tool_safety.enabled
            and self.runtime.settings.tool_safety.allow_gate_overrides
        ):
            return toast("Saved but ignored: deployment overrides are disabled.")
        return toast(f"Third-party write approvals: {value}.")
