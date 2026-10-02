"""The `setup` command: Agents, Details, Who answers where, and the panel's dialogs.

Mirrors Slack's `/agent-setup` and its rules. The panel is read-only and open to
every member; changes happen in a setup conversation, where the chat tools own
authorization. New agent is open to everyone (a fresh agent is unrouted, so it
puts nothing at risk). Minting a coding-tool token is `authorize_coding_token`'s
call, as on Discord and Slack: panels live in the 1:1 chat, so a channel admin
picks one of their channels in the dialog and the token is bound there (an
unbound token stays with server admins). Only its minter may revoke it; token
values are never logged. Every click re-verifies the
clicker and re-reads state, so a stale card grants nothing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime

import structlog
from daimon.adapters.teams import setup_card as cards
from daimon.adapters.teams.card_actions import (
    FAILED,
    SENDING_PANEL_ERRORS,
    Actor,
    card_actor,
    dialog,
    dialog_message,
    edit_origin_card,
    get_or_create_account,
    guarded,
    replace_card,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import (
    end_setup_conversation,
    open_setup_conversation,
)
from daimon.core.agent_detail_lists import DetailListName
from daimon.core.agent_details import GitHubDeploymentFacts, load_agent_details
from daimon.core.agent_lifecycle import create_blank_agent
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.answering_map import AnsweringMap, load_answering_map, routed_agent_names
from daimon.core.authz import AgentRef, build_agent_ref
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    load_administered_channel_ids,
    load_live_subject,
)
from daimon.core.channel_isolation import load_isolation_viewer
from daimon.core.constants import ALLOWED_MODEL_IDS, DEFAULT_AGENT_MODEL
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import (
    authorize_coding_token,
    coding_tool_config,
    mint_agent_mcp_token,
    token_jti,
)
from daimon.core.models_catalog import ModelChoice, list_model_choices
from daimon.core.panel_audit import PanelOp, PanelOutcome, record_panel_write
from daimon.core.roster import Roster, load_roster, paginate
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    TaskFetchInvokeActivity,
    TaskModuleInvokeResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard

log = structlog.get_logger()

GONE = "That agent is no longer available. It may have been deleted."
NOT_CONFIGURED = "This deployment is not set up for coding-tool access yet. Ask the operator."
NEEDS_ADMIN = (
    "Minting an access token for {name} needs an admin, or an admin of every channel "
    "it is pinned to, binding it to one of them."
)
NOT_MINTER = "Only the person who minted this token can revoke it."
DM_ONLY = "Open setup from our 1:1 chat to start a setup conversation."
STARTED = "Setup conversation started. Reply in this chat."
ALREADY_ENDED = "This setup conversation has already ended."
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _guarded[T](work: Awaitable[T], failed: T) -> T:
    return await guarded(work, failed, "teams.agent_setup.failed", errors=SENDING_PANEL_ERRORS)


def _page(data: Mapping[str, object]) -> int:
    page = data.get("page")
    return page if isinstance(page, int) else 0


def _models() -> tuple[ModelChoice, ...]:
    return list_model_choices(default=DEFAULT_AGENT_MODEL)


class SetupPanel:
    """Handlers for the command, the panel buttons and the two dialogs."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def command(self, context: CommandContext) -> None:
        await context.send_card(
            await self._agents(context.tenant_id, chat=context.inbound.channel_id, page=0)
        )

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await _guarded(self._act(ctx), toast(FAILED))

    async def on_create_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        if await card_actor(self._runtime, ctx.activity) is None:
            return dialog_message(DENIED)
        return dialog("New agent", cards.new_agent_form(_models(), {}))

    async def on_create_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._create(ctx), dialog_message(FAILED))

    async def on_token_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._mint(ctx.activity), dialog_message(FAILED))

    async def on_token_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._token_submit(ctx.activity), dialog_message(FAILED))

    async def _roster(self, tenant_id: uuid.UUID, chat: str | None) -> Roster:
        async with self._runtime.sessionmaker() as session:
            return await load_roster(
                session,
                self._runtime.anthropic,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=chat,
                thread_id=None,
                default=self._runtime.deployment_default,
            )

    async def _agents(
        self, tenant_id: uuid.UUID, *, chat: str, page: int, notice: str | None = None
    ) -> AdaptiveCard:
        roster = await self._roster(tenant_id, chat)
        routed = routed_agent_names(await self._answering_map(tenant_id))
        window = paginate(roster.rows, page=page, page_size=cards.PAGE_SIZE)
        return cards.roster_card(roster, window, routed=routed, notice=notice)

    async def _answering_map(
        self, tenant_id: uuid.UUID, viewer: Actor | None = None
    ) -> AnsweringMap:
        """The install's routing; as `viewer` sees it across isolated channels, if given."""
        async with self._runtime.sessionmaker() as session:
            return await load_answering_map(
                session,
                tenant_id=tenant_id,
                platform="teams",
                default=self._runtime.deployment_default,
                viewer=None
                if viewer is None
                else await load_isolation_viewer(
                    session,
                    self._runtime.anthropic,
                    tenant_id=tenant_id,
                    channel_id=viewer.conversation_id,
                    is_admin=viewer.is_admin,
                ),
            )

    async def _routing(self, actor: Actor, page: int) -> AdaptiveCard:
        roster = await self._roster(actor.tenant_id, actor.conversation_id)
        # A non-admin sees only their side of every isolated channel, as on Discord and Slack.
        answering_map = await self._answering_map(actor.tenant_id, actor)
        # The request names an agent nobody reaches yet, else the one answering here.
        unrouted = (r.name for r in roster.rows if r.answering_tier is None and not r.is_built_in)
        request_agent = next(unrouted, roster.answering.name if roster.answering else None)
        window = paginate(answering_map.channel_overrides, page=page, page_size=cards.PAGE_SIZE)
        return cards.routing_card(
            answering_map, window, is_admin=actor.is_admin, request_agent=request_agent
        )

    async def _details(
        self, actor: Actor, name: str, page: int, expanded: DetailListName | None = None
    ) -> AdaptiveCard | None:
        """The Details card for `name`, or None when it left the roster.

        The roster maps the clicked name to an MA id, so a click never names an id itself.
        """
        roster = await self._roster(actor.tenant_id, actor.conversation_id)
        match = next((row for row in roster.rows if row.name == name), None)
        if match is None:
            return None
        github, mcp = self._runtime.settings.github, self._runtime.settings.mcp
        async with self._runtime.sessionmaker() as session:
            details = await load_agent_details(
                session,
                self._runtime.anthropic,
                tenant_id=actor.tenant_id,
                ma_agent_id=match.ma_agent_id,
                platform="teams",
                channel_id=actor.conversation_id,
                thread_id=None,
                deployment_default=self._runtime.deployment_default,
                github=GitHubDeploymentFacts(
                    has_fallback_pat=github.fallback_pat is not None,
                    app_configured=github.app_id is not None and github.app_private_key is not None,
                ),
                public_mcp_url=str(mcp.public_url) if mcp.public_url is not None else None,
                is_admin=actor.is_admin,
                channel_label=None,
            )
        coding_tools = mcp.public_url is not None and mcp.jwt_secret is not None
        return cards.details_card(
            details,
            here=actor.conversation_id,
            page=page,
            coding_tools=coding_tools,
            expanded=expanded,
        )

    async def _act(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        op, page, chat = data.get("op"), _page(data), actor.conversation_id
        if op == "details":
            name, expanded = str(data.get("agent") or ""), cards.expanded_list(data)
            card = await self._details(actor, name, page, expanded)
            return replace_card(
                card or await self._agents(actor.tenant_id, chat=chat, page=page, notice=GONE)
            )
        if op == "routing":
            return replace_card(await self._routing(actor, page))
        if op == "manage":
            if activity.conversation.conversation_type != "personal":
                return toast(DM_ONLY)
            target = str(data.get("agent") or "") or None
            await open_setup_conversation(
                self._runtime, actor, target_ma_agent_id=target, send=ctx.send
            )
            return toast(STARTED)
        if op == "end":
            ended = await end_setup_conversation(
                self._runtime.sessionmaker,
                tenant_id=actor.tenant_id,
                chat_id=chat,
                thread_id=str(data.get("thread") or ""),
            )
            return replace_card(cards.notice_card(cards.ENDED if ended else ALREADY_ENDED))
        return replace_card(await self._agents(actor.tenant_id, chat=chat, page=page))

    async def _create(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, ctx.activity)
        if actor is None:
            return dialog_message(DENIED)
        data = submitted_fields(ctx.activity.value.data)
        values = {key: str(data.get(key) or "").strip() for key in ("name", "purpose", "model")}
        name = values["name"]
        error = None
        if not _NAME.match(name):
            error = "Name must be 1-64 characters: letters, digits, hyphens, underscores."
        elif values["model"] not in ALLOWED_MODEL_IDS:
            error = "Choose a model from the list."
        else:
            mcp_url = self._runtime.settings.mcp.public_url
            try:
                outcome = await create_blank_agent(
                    self._runtime.anthropic,
                    tenant_id=actor.tenant_id,
                    name=name,
                    system=values["purpose"] or None,
                    model=values["model"],
                    account_id=derive_guild_account_uuid(actor.tenant_id),
                    public_url=str(mcp_url) if mcp_url is not None else None,
                )
                if outcome.anthropic_id is None:
                    error = "Agent creation did not return an identity. Reopen setup and retry."
            except DaimonError as exc:
                error = str(exc)
        if error is not None:
            return dialog("New agent", cards.new_agent_form(_models(), values, error))
        log.info("teams.agent_setup.created", tenant_id=str(actor.tenant_id), agent_name=name)
        details = await self._details(actor, name, 0)
        if ctx.activity.reply_to_id and details is not None:
            await edit_origin_card(ctx, details)  # Like Slack, the panel lands on Details.
        return dialog_message(f"Created {name}. It does not answer anywhere yet.")

    async def _mint(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        """Mint at once, or first ask a channel admin which of their channels it runs in."""
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        name = str(submitted_fields(activity.value.data).get("agent") or "")
        if self._runtime.settings.mcp.jwt_secret is None or (
            self._runtime.settings.mcp.public_url is None
        ):
            return dialog_message(NOT_CONFIGURED)
        async with self._runtime.sessionmaker() as session:
            administered = await load_administered_channel_ids(
                session,
                tenant_id=actor.tenant_id,
                platform="teams",
                caller=ChannelAdminCaller(platform_user_id=actor.user_id),
            )
        if not administered:
            return await self._issue(actor, name, channel_id=None)
        form = cards.token_channel_form(
            agent_name=name, channel_ids=sorted(administered), allow_unbound=actor.is_admin
        )
        return dialog("Use from your coding tools", form)

    async def _token_submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleInvokeResponse:
        data = submitted_fields(activity.value.data)
        if data.get("op") != "mint":
            return await self._revoke(activity)
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        channel = str(data.get("channel") or cards.UNBOUND)
        return await self._issue(
            actor,
            str(data.get("agent") or ""),
            channel_id=None if channel == cards.UNBOUND else channel,
        )

    async def _issue(
        self, actor: Actor, name: str, *, channel_id: str | None
    ) -> TaskModuleInvokeResponse:
        """Mint `name`'s token as `authorize_coding_token` decides, as Discord and Slack do.

        `channel_id` is the channel picked in the dialog; the grants and the
        pin are re-read here, so a stale or forged pick grants nothing.
        """
        secret = self._runtime.settings.mcp.jwt_secret
        public_url = self._runtime.settings.mcp.public_url
        if secret is None or public_url is None:
            return dialog_message(NOT_CONFIGURED)
        roster = await self._roster(actor.tenant_id, None)
        target = next((row for row in roster.rows if row.name == name), None)
        try:
            async with self._runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=actor.tenant_id)
                subject = await load_live_subject(
                    session,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    caller=ChannelAdminCaller(
                        platform_user_id=actor.user_id, is_server_admin=actor.is_admin
                    ),
                )
        except AccessPolicyUnreadable:
            return dialog_message(POLICY_UNREADABLE_REFUSAL)
        agent = AgentRef.of(name)
        if target is not None and channel_id is not None and policy.agent_channel_pins:
            ma_agent = await self._runtime.anthropic.beta.agents.retrieve(target.ma_agent_id)
            agent = build_agent_ref(ma_agent.name, ma_agent.metadata, target.name)
        decision, bound_channel_id = authorize_coding_token(
            policy, subject=subject, agent=agent, channel_id=channel_id
        )
        if not decision:
            log.info("teams.coding_tools.refused", agent_name=name, reason=decision.reason)
            await self._audit(
                actor, "coding_token_mint", outcome="denied", reason=f"authz:{decision.reason}"
            )
            return dialog_message(NEEDS_ADMIN.format(name=name))
        if target is None:
            return dialog_message(GONE)
        account_id = await get_or_create_account(self._runtime, actor)
        async with self._runtime.sessionmaker.begin() as session:
            token = await mint_agent_mcp_token(
                session,
                account_id=account_id,
                tenant_id=actor.tenant_id,
                agent_id=derive_agent_uuid(
                    tenant_id=actor.tenant_id, ma_agent_id=target.ma_agent_id
                ),
                label=name,
                secret=secret.get_secret_value().encode(),
                now=datetime.now(UTC),
                platform="teams" if bound_channel_id is not None else None,
                channel_id=bound_channel_id,
            )
        jti = token_jti(token)
        log.info(  # never the token
            "teams.coding_tools.minted",
            agent_name=name,
            jti=str(jti),
            bound_channel_id=bound_channel_id,
        )
        await self._audit(
            actor, "coding_token_mint", outcome="allowed", reason="completed", jti=jti
        )
        cli, mcp_json = coding_tool_config(agent_name=name, public_url=str(public_url), jwt=token)
        card = cards.token_card(
            agent_name=name, cli=cli, mcp_json=mcp_json, jti=str(jti), channel_id=bound_channel_id
        )
        return dialog("Use from your coding tools", card)

    async def _audit(
        self,
        actor: Actor,
        op: PanelOp,
        *,
        outcome: PanelOutcome,
        reason: str,
        jti: uuid.UUID | None = None,
    ) -> None:
        await record_panel_write(
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            platform="teams",
            platform_user_id=actor.user_id,
            op=op,
            outcome=outcome,
            reason=reason,
            token_kind="agent",
            token_jti=jti,
        )

    async def _revoke(self, activity: TaskSubmitInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        try:
            jti = uuid.UUID(str(submitted_fields(activity.value.data).get("jti")))
        except ValueError:
            return dialog_message(NOT_MINTER)
        account_id = await get_or_create_account(self._runtime, actor)
        async with self._runtime.sessionmaker.begin() as session:
            row = await get_mcp_token(session, jti=jti)
            mine = row is not None and (row.tenant_id, row.account_id) == (
                actor.tenant_id,
                account_id,
            )
            revoked = (
                await revoke_mcp_token(session, jti=jti, now=datetime.now(UTC)) if mine else None
            )
        if not mine:
            log.info("teams.coding_tools.revoke_refused", jti=str(jti))
            await self._audit(
                actor, "coding_token_revoke", outcome="denied", reason="not_minter", jti=jti
            )
            return dialog_message(NOT_MINTER)
        if revoked is None:
            await self._audit(
                actor, "coding_token_revoke", outcome="error", reason="already_revoked", jti=jti
            )
            return dialog_message("That token was already revoked.")
        log.info("teams.coding_tools.revoked", jti=str(jti))
        await self._audit(
            actor, "coding_token_revoke", outcome="allowed", reason="completed", jti=jti
        )
        return dialog_message("Token revoked.")
