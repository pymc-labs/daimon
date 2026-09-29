"""The `setup` command: Agents, Details, Who answers where, and the panel's dialogs.

Mirrors Slack's `/agent-setup` and its rules. The panel is read-only and open to
every member; changes happen in a setup conversation, where the chat tools own
authorization. New agent is open to everyone (a fresh agent is unrouted, so it
puts nothing at risk). Minting a coding-tool token is admin-only and only its
minter may revoke it; token values are never logged. Every click re-verifies the
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
from daimon.core.answering_map import AnsweringMap, load_answering_map, routed_agent_names
from daimon.core.constants import ALLOWED_MODEL_IDS, DEFAULT_AGENT_MODEL
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import coding_tool_config, mint_agent_mcp_token, token_jti
from daimon.core.models_catalog import ModelChoice, list_model_choices
from daimon.core.roster import Roster, load_roster, paginate
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
NEEDS_ADMIN = "Minting an access token for {name} needs an admin."
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
        return await _guarded(self._revoke(ctx.activity), dialog_message(FAILED))

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

    async def _answering_map(self, tenant_id: uuid.UUID) -> AnsweringMap:
        async with self._runtime.sessionmaker() as session:
            return await load_answering_map(
                session,
                tenant_id=tenant_id,
                platform="teams",
                default=self._runtime.deployment_default,
            )

    async def _routing(self, actor: Actor, page: int) -> AdaptiveCard:
        roster = await self._roster(actor.tenant_id, actor.conversation_id)
        answering_map = await self._answering_map(actor.tenant_id)
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
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        name = str(submitted_fields(activity.value.data).get("agent") or "")
        secret = self._runtime.settings.mcp.jwt_secret
        public_url = self._runtime.settings.mcp.public_url
        if secret is None or public_url is None:
            return dialog_message(NOT_CONFIGURED)
        if not actor.is_admin:
            log.info("teams.coding_tools.refused_non_admin", agent_name=name)
            return dialog_message(NEEDS_ADMIN.format(name=name))
        roster = await self._roster(actor.tenant_id, None)
        target = next((row for row in roster.rows if row.name == name), None)
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
            )
        jti = token_jti(token)
        log.info("teams.coding_tools.minted", agent_name=name, jti=str(jti))  # never the token
        cli, mcp_json = coding_tool_config(agent_name=name, public_url=str(public_url), jwt=token)
        card = cards.token_card(agent_name=name, cli=cli, mcp_json=mcp_json, jti=str(jti))
        return dialog("Use from your coding tools", card)

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
            if row is None or row.tenant_id != actor.tenant_id or row.account_id != account_id:
                log.info("teams.coding_tools.revoke_refused", jti=str(jti))
                return dialog_message(NOT_MINTER)
            revoked = await revoke_mcp_token(session, jti=jti, now=datetime.now(UTC))
        if revoked is None:
            return dialog_message("That token was already revoked.")
        log.info("teams.coding_tools.revoked", jti=str(jti))
        return dialog_message("Token revoked.")
