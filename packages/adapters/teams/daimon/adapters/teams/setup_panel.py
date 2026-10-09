"""The `setup` command: Agents, Details, Who answers where, and the panel's dialogs.

Mirrors Slack's `/agent-setup` and its rules. The panel is open to every
member; agent changes happen in a setup conversation, where the chat tools own
authorization, and a channel's environment, isolation and admins in the
Channel settings dialog (`channel_settings`). New agent is open to everyone (a
fresh agent is unrouted, so it puts nothing at risk); Add skill on Details is
Discord's and Slack's form, with their rule (`add_skill`). Minting a coding-tool
token is `authorize_coding_token`'s call, as on Discord and Slack: panels live
in the 1:1 chat, so a channel admin picks one of their channels in the dialog
and the token is bound there (an unbound token stays with server admins). Only
its minter may revoke it; token values are never logged. Every click
re-verifies the clicker and re-reads state, so a stale card grants nothing.
"""

from __future__ import annotations

import functools
import re
import uuid
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime

import anthropic
import structlog
from daimon.adapters.teams import setup_card as cards
from daimon.adapters.teams.add_skill import (
    SkillAddRefused,
    add_previewed_skill,
    skill_change_refusal,
)
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
from daimon.adapters.teams.channel_admin_groups import channel_admin_caller
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import (
    end_setup_conversation,
    open_setup_conversation,
)
from daimon.core.agent_detail_lists import DetailListName
from daimon.core.agent_details import GitHubDeploymentFacts, load_agent_details
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_lifecycle import create_blank_agent
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.agent_reach import record_created_for_channel
from daimon.core.answering_map import AnsweringMap, load_answering_map, routed_agent_names
from daimon.core.authz import AgentRef, build_agent_ref
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    load_administered_channel_ids,
    load_live_subject,
)
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
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_audit import PanelOp, PanelOutcome, record_panel_write
from daimon.core.panel_operator_tokens import (
    list_panel_operator_tokens,
    mint_panel_operator_token,
    revoke_panel_operator_token,
)
from daimon.core.permissions import any_agent_rules
from daimon.core.roster import Roster, RosterAgent, load_roster, paginate
from daimon.core.rule_views import load_rule_viewer
from daimon.core.skills.ingest import SkillIngestError, bundle_from_markdown
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
    "its rule runs it in, binding it to one of them."
)
NOT_MINTER = "Only the person who minted this token can revoke it."
OPERATOR_NEEDS_ADMIN = "Only an admin can mint or revoke operator tokens."
OPERATOR_NOT_CONFIGURED = "This deployment has no MCP signing key, so it mints no operator tokens."
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
            await self._agents(
                context.tenant_id,
                chat=context.inbound.channel_id,
                page=0,
                is_admin=context.is_admin,
            )
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

    async def on_operator_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._operator_open(ctx.activity), dialog_message(FAILED))

    async def on_operator_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._operator_submit(ctx.activity), dialog_message(FAILED))

    async def on_skill_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._skill_open(ctx.activity), dialog_message(FAILED))

    async def on_skill_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._skill_submit(ctx), dialog_message(FAILED))

    async def _skill_target(self, actor: Actor, name: str) -> RosterAgent | None:
        """The agent `name` as the panel lists it to `actor`: a click never names an id."""
        roster = await self._roster(actor.tenant_id, actor.conversation_id, is_admin=actor.is_admin)
        return next((row for row in roster.rows if row.name == name), None)

    async def _skill_open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        agent = await self._skill_target(
            actor, str(submitted_fields(activity.value.data).get("agent") or "")
        )
        if agent is None:
            return dialog_message(GONE)
        account_id = await get_or_create_account(self._runtime, actor)
        if refusal := await skill_change_refusal(
            self._runtime, actor, agent, account_id=account_id
        ):
            return dialog_message(refusal)
        return dialog(f"Add a skill to {agent.name}", cards.add_skill_form(agent.name))

    async def _skill_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        """Preview the paste, or add it when it is the text just previewed."""
        actor = await card_actor(self._runtime, ctx.activity)
        if actor is None:
            return dialog_message(DENIED)
        data = submitted_fields(ctx.activity.value.data)
        agent = await self._skill_target(actor, str(data.get("agent") or ""))
        if agent is None:
            return dialog_message(GONE)
        account_id = await get_or_create_account(self._runtime, actor)
        # Every submit, as on Discord: a preview is a step of the add, not open to everyone.
        if refusal := await skill_change_refusal(
            self._runtime, actor, agent, account_id=account_id
        ):
            return dialog_message(refusal)
        title, text = f"Add a skill to {agent.name}", str(data.get("skill") or "")
        try:
            bundle = bundle_from_markdown(text.strip())
        except SkillIngestError as exc:
            return dialog(title, cards.add_skill_form(agent.name, text=text, error=str(exc)))
        if data.get("hash") != bundle.preview.content_hash:
            form = cards.add_skill_form(agent.name, text=text, preview=bundle.preview)
            return dialog(title, form)
        try:
            result = await add_previewed_skill(
                self._runtime, actor, agent, bundle, account_id=account_id
            )
        except SkillAddRefused as exc:
            return dialog_message(exc.refusal)
        except SkillIngestError as exc:
            return dialog_message(f"{exc} Nothing was added.")
        except (DaimonError, anthropic.APIError):
            log.exception("teams.agent_setup.skill_add_failed", agent_name=agent.name)
            return dialog_message(
                f"Adding {bundle.preview.name} to {agent.name} failed. Try again."
            )
        details = await self._details(actor, agent.name, 0)
        if ctx.activity.reply_to_id and details is not None:
            await edit_origin_card(ctx, details)  # As on Slack, Details shows the new skill.
        done = "already had" if result.action == "unchanged" else "now has"
        return dialog_message(f"{agent.name} {done} the skill {bundle.preview.name}.")

    async def _operator_tokens(
        self, actor: Actor, notice: str | None = None
    ) -> TaskModuleInvokeResponse:
        async with self._runtime.sessionmaker() as session:
            rows = await list_panel_operator_tokens(
                session, tenant_id=actor.tenant_id, now=datetime.now(UTC)
            )
        return dialog("Operator tokens", cards.operator_tokens_card(rows, notice=notice))

    async def _operator_open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None or not actor.is_admin:
            return dialog_message(OPERATOR_NEEDS_ADMIN)
        if self._runtime.settings.mcp.jwt_secret is None:
            return dialog_message(OPERATOR_NOT_CONFIGURED)
        return await self._operator_tokens(actor)

    async def _operator_submit(
        self, activity: TaskSubmitInvokeActivity
    ) -> TaskModuleInvokeResponse:
        """Mint or revoke for a live admin; every outcome is audited."""
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        data = submitted_fields(activity.value.data)
        revoking = data.get("op") == "revoke"
        op: PanelOp = "operator_token_revoke" if revoking else "operator_token_mint"
        audit = functools.partial(
            record_panel_write,
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            platform="teams",
            platform_user_id=actor.user_id,
            op=op,
            token_kind="operator",
        )
        if not actor.is_admin:
            await audit(outcome="denied", reason="needs_admin")
            return dialog_message(OPERATOR_NEEDS_ADMIN)
        secret = self._runtime.settings.mcp.jwt_secret
        if secret is None:
            return dialog_message(OPERATOR_NOT_CONFIGURED)
        now = datetime.now(UTC)
        if revoking:
            try:
                jti = uuid.UUID(str(data.get("jti")))
            except ValueError:
                return await self._operator_tokens(actor, "Pick a token to revoke.")
            async with self._runtime.sessionmaker.begin() as session:
                revoked = await revoke_panel_operator_token(
                    session, tenant_id=actor.tenant_id, jti=jti, now=now
                )
            await audit(
                outcome="allowed" if revoked else "error",
                reason="completed" if revoked else "already_revoked",
                token_jti=jti,
            )
            return await self._operator_tokens(
                actor, "Token revoked." if revoked else "That token was already revoked."
            )
        scopes = [part for part in str(data.get("scopes") or "").split(",") if part]
        try:
            async with self._runtime.sessionmaker.begin() as session:
                minted = await mint_panel_operator_token(
                    session,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    platform_user_id=actor.user_id,
                    scopes=scopes,
                    label=str(data.get("label") or ""),
                    secret=secret.get_secret_value().encode(),
                    now=now,
                )
        except OperatorTokenError as exc:
            await audit(outcome="denied", reason="scopes")
            return await self._operator_tokens(actor, f"{exc}. Nothing was minted.")
        await audit(outcome="allowed", reason="completed", token_jti=minted.jti)
        log.info("teams.operator_token.minted", jti=str(minted.jti))  # never the token
        card = cards.operator_token_card(
            token=minted.token,
            scopes=sorted(minted.scopes),
            expires=minted.expires_at.date().isoformat(),
        )
        return dialog("Operator token", card)

    async def _roster(
        self, tenant_id: uuid.UUID, chat: str | None, *, is_admin: bool | None
    ) -> Roster:
        """The agents as a viewer at `chat` sees them; `is_admin=None` lists every agent.

        A non-admin sees only their side of every isolated channel, as on Discord
        and Slack. None is for lookups that `authorize` gates afterwards.
        """
        async with self._runtime.sessionmaker() as session:
            viewer = (
                None
                if is_admin is None
                else await load_rule_viewer(
                    session,
                    self._runtime.anthropic,
                    tenant_id=tenant_id,
                    channel_id=chat,
                    is_admin=is_admin,
                )
            )
            return await load_roster(
                session,
                self._runtime.anthropic,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=chat,
                thread_id=None,
                default=self._runtime.deployment_default,
                viewer=viewer,
            )

    async def _agents(
        self,
        tenant_id: uuid.UUID,
        *,
        chat: str,
        page: int,
        is_admin: bool,
        notice: str | None = None,
    ) -> AdaptiveCard:
        roster = await self._roster(tenant_id, chat, is_admin=is_admin)
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
                else await load_rule_viewer(
                    session,
                    self._runtime.anthropic,
                    tenant_id=tenant_id,
                    channel_id=viewer.conversation_id,
                    is_admin=viewer.is_admin,
                ),
            )

    async def _routing(self, actor: Actor, page: int) -> AdaptiveCard:
        roster = await self._roster(actor.tenant_id, actor.conversation_id, is_admin=actor.is_admin)
        # A non-admin sees only their side of every isolated channel, as on Discord and Slack.
        answering_map = await self._answering_map(actor.tenant_id, actor)
        # The request names an agent nobody reaches yet, else the one answering here.
        unrouted = (r.name for r in roster.rows if r.answering_tier is None and not r.is_built_in)
        request_agent = next(unrouted, roster.answering.name if roster.answering else None)
        window = paginate(answering_map.channel_overrides, page=page, page_size=cards.PAGE_SIZE)
        administered: frozenset[str] = frozenset()
        if not actor.is_admin:
            async with self._runtime.sessionmaker() as session:
                administered = await load_administered_channel_ids(
                    session,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    caller=ChannelAdminCaller(platform_user_id=actor.user_id),
                )
        return cards.routing_card(
            answering_map,
            window,
            is_admin=actor.is_admin,
            request_agent=request_agent,
            changes_channels=actor.is_admin or bool(administered),
        )

    async def _details(
        self, actor: Actor, name: str, page: int, expanded: DetailListName | None = None
    ) -> AdaptiveCard | None:
        """The Details card for `name`, or None when it left the roster.

        The roster maps the clicked name to an MA id, so a click never names an id itself.
        """
        roster = await self._roster(actor.tenant_id, actor.conversation_id, is_admin=actor.is_admin)
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
                card
                or await self._agents(
                    actor.tenant_id, chat=chat, page=page, is_admin=actor.is_admin, notice=GONE
                )
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
        return replace_card(
            await self._agents(actor.tenant_id, chat=chat, page=page, is_admin=actor.is_admin)
        )

    async def _create(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, ctx.activity)
        if actor is None:
            return dialog_message(DENIED)
        data = submitted_fields(ctx.activity.value.data)
        values = {key: str(data.get(key) or "").strip() for key in ("name", "purpose", "model")}
        name = values["name"]
        error, created_id = None, None
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
                created_id = outcome.anthropic_id
                if created_id is None:
                    error = "Agent creation did not return an identity. Reopen setup and retry."
            except DaimonError as exc:
                error = str(exc)
        if error is not None or created_id is None:
            return dialog("New agent", cards.new_agent_form(_models(), values, error))
        log.info("teams.agent_setup.created", tenant_id=str(actor.tenant_id), agent_name=name)
        queue_agent_face(
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            agent_name=name,
            metadata=None,  # create_blank_agent is never managed
            default_agent_name=self._runtime.deployment_default.agent_name,
        )
        caller = await channel_admin_caller(
            self._runtime, tenant_id=actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin
        )
        async with self._runtime.sessionmaker.begin() as session:
            await record_created_for_channel(  # One a channel admin makes here is the channel's.
                session,
                tenant_id=actor.tenant_id,
                platform="teams",
                ma_agent_id=created_id,
                channel_id=actor.conversation_id.split(";", 1)[0],
                caller=caller,
            )
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
        roster = await self._roster(actor.tenant_id, None, is_admin=None)
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
        if target is not None and channel_id is not None and any_agent_rules(policy):
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
