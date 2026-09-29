"""The `routines` command, its card actions and its create dialog.

Mirrors Slack's `/routines` panel and its rules: everyone sees the tenant's
routines; only an admin or the routine's creator may pause, resume, read the
last output or delete one; only an admin may create. Every click re-verifies
the clicker and re-reads the row, so a stale card grants nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
    card_actor,
    dialog,
    dialog_message,
    edit_origin_card,
    guarded,
    replace_card,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.routines_card import (
    FORM_FIELDS,
    confirm_delete_card,
    create_form,
    created_notice,
    form_values,
    output_card,
    panel_card,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.routines import can_manage_routine, panel_rows
from daimon.core.stores import routines as store
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

GONE = "This routine no longer exists."
NOT_OWNER = "Only the routine's creator or an admin can do that."
ADMIN_ONLY = "Only an admin can create routines."
_EVENT = "teams.routines.failed"


class RoutinesPanel:
    """Handlers for the command, the panel buttons and the create dialog."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def command(self, context: CommandContext) -> None:
        user_id, is_admin = context.inbound.user_id, context.is_admin
        await context.send_card(
            await self._panel(context.tenant_id, user_id=user_id, is_admin=is_admin)
        )

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), _EVENT)

    async def on_dialog_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await guarded(self._open(ctx.activity), dialog_message(FAILED), _EVENT)

    async def on_dialog_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await guarded(self._submit(ctx), dialog_message(FAILED), _EVENT)

    async def _panel(
        self, tenant_id: uuid.UUID, *, user_id: str, is_admin: bool, notice: str | None = None
    ) -> AdaptiveCard:
        async with self._runtime.sessionmaker() as session:
            rows = await store.list_routines_for_tenant(session, tenant_id=tenant_id)
        shown, hidden = panel_rows(rows)
        return panel_card(shown, hidden, user_id=user_id, is_admin=is_admin, notice=notice)

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        op = data.get("op")
        notice: str | None = None
        if op != "refresh":
            try:
                routine_id = uuid.UUID(str(data.get("routine")))
            except ValueError:
                return toast(GONE)
            tenant_id = actor.tenant_id
            async with self._runtime.sessionmaker.begin() as session:
                row = await store.get_routine(session, routine_id, tenant_id=tenant_id)
                if row is None:
                    return toast(GONE)
                if not can_manage_routine(row, user_id=actor.user_id, is_admin=actor.is_admin):
                    return toast(NOT_OWNER)
                if op == "output":
                    return replace_card(output_card(row))
                if op == "delete":
                    return replace_card(confirm_delete_card(row))
                if op == "pause":
                    await store.pause_routine(session, routine_id, tenant_id=tenant_id)
                elif op == "resume":
                    now = datetime.now(UTC)
                    await store.resume_routine(session, routine_id, tenant_id=tenant_id, now=now)
                elif op == "confirm_delete":
                    await store.delete_routine(session, routine_id, tenant_id=tenant_id)
                    notice = "🗑️ Routine deleted."
        card = await self._panel(
            actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin, notice=notice
        )
        return replace_card(card)

    async def _form(
        self, actor: Actor, values: dict[str, str], error: str | None = None
    ) -> TaskModuleInvokeResponse:
        agents = await list_agents_by_tenant(self._runtime.anthropic, tenant_id=actor.tenant_id)
        names = (agent.metadata.get(MA_METADATA_KEY_NAME) for agent in agents)
        form = create_form(sorted(name for name in names if name), values, error)
        return dialog("New routine", form)

    async def _open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None or not actor.is_admin:
            return dialog_message(DENIED if actor is None else ADMIN_ONLY)
        return await self._form(actor, {})

    async def _submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, ctx.activity)
        if actor is None or not actor.is_admin:
            return dialog_message(DENIED if actor is None else ADMIN_ONLY)
        values = form_values(ctx.activity.value.data)
        error = await self._create(actor, values)
        if error is not None:
            return await self._form(actor, values, error)
        created = created_notice(values["agent"], values["cron"])
        if ctx.activity.reply_to_id:
            panel = await self._panel(
                actor.tenant_id, user_id=actor.user_id, is_admin=True, notice=created
            )
            await edit_origin_card(ctx, panel)
        return dialog_message(created)

    async def _create(self, actor: Actor, values: dict[str, str]) -> str | None:
        """Validate and write the routine; the error to show, or None once created."""
        missing = [label for field, label in FORM_FIELDS.items() if not values[field]]
        if missing:
            return f"{missing[0]} is required."
        try:
            next_fire_at = validated_next_slot(
                values["cron"], values["timezone"], datetime.now(UTC)
            )
        except InvalidScheduleError as error:
            return f"Could not schedule the routine: {error}."
        agent = await find_agent_by_daimon_tag(
            self._runtime.anthropic, tenant_id=actor.tenant_id, name=values["agent"]
        )
        if agent is None:
            return f"No agent named {values['agent']!r} found."
        async with self._runtime.sessionmaker.begin() as session:
            await store.create_routine(
                session,
                tenant_id=actor.tenant_id,
                created_by_user_id=actor.user_id,
                agent_id=agent.id,
                agent_name=values["agent"],
                cron_expr=values["cron"],
                timezone_=values["timezone"],
                trigger_message=values["message"],
                next_fire_at=next_fire_at,
            )
        log.info("teams.routines.created", tenant_id=str(actor.tenant_id), agent=values["agent"])
        return None
