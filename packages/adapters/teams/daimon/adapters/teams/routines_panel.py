"""The `routines` command, its card actions and its create dialog.

Mirrors Slack's `/routines` panel and its rules: a member sees only the
routines they created, an admin sees all; only an admin or the creator may
pause, resume, read the last output or delete one; a member may create one
for the agent the chat or channel it is opened in answers with, an admin for
any agent, as in chat.
Every click re-verifies the clicker and re-reads the row, so a stale card
grants nothing.
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
    dialog_notice,
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
from daimon.core.authz import build_agent_ref
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.routines import (
    can_manage_routine,
    channel_agent,
    may_schedule_agent,
    panel_rows,
    panel_save_refusal,
)
from daimon.core.stores import routines as store
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
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
NOT_HERE = "{agent} doesn't answer here, so only an admin can schedule it."
NO_AGENT = "No agent answers here, so there's none you can schedule.\n\nAsk an admin."
NO_AGENTS_YET = "There are no agents to schedule yet.\n\nTo add one, {verb} {setup}."
NEW_ROUTINE = "New routine"
_EVENT = "teams.routines.failed"


def _place(actor: Actor) -> str:
    """The channel (or chat) the panel was opened in, as a turn there resolves it.

    A channel thread's conversation carries its root (`;messageid=`); the
    channel is what its agent is configured on.
    """
    return actor.conversation_id.split(";", 1)[0]


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
        return await guarded(self._open(ctx.activity), dialog_notice(NEW_ROUTINE, FAILED), _EVENT)

    async def on_dialog_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await guarded(self._submit(ctx), dialog_message(FAILED), _EVENT)

    async def _panel(
        self, tenant_id: uuid.UUID, *, user_id: str, is_admin: bool, notice: str | None = None
    ) -> AdaptiveCard:
        async with self._runtime.sessionmaker() as session:
            rows = await store.list_routines_for_tenant(session, tenant_id=tenant_id)
        if not is_admin:
            # A routine's label and trigger are its creator's work.
            rows = [row for row in rows if row.created_by_user_id == user_id]
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
                if op in ("pause", "resume"):

                    async def allowed(row: RoutineRow) -> bool:
                        return can_manage_routine(
                            row, user_id=actor.user_id, is_admin=actor.is_admin
                        )

                    result = await apply_routine_action(
                        session,
                        routine_id,
                        tenant_id=tenant_id,
                        op=op,
                        allowed=allowed,
                        load=store.get_routine,
                        pause=store.pause_routine,
                        resume=store.resume_routine,
                        now=lambda: datetime.now(UTC),
                    )
                    if result != "changed":
                        return toast(GONE if result == "gone" else NOT_OWNER)
                else:
                    row = await store.get_routine(session, routine_id, tenant_id=tenant_id)
                    if row is None:
                        return toast(GONE)
                    if not can_manage_routine(row, user_id=actor.user_id, is_admin=actor.is_admin):
                        return toast(NOT_OWNER)
                    if op == "output":
                        return replace_card(output_card(row))
                    if op == "delete":
                        return replace_card(confirm_delete_card(row))
                    if op == "confirm_delete":
                        await store.delete_routine(session, routine_id, tenant_id=tenant_id)
                        notice = "🗑️ Routine deleted."
        card = await self._panel(
            actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin, notice=notice
        )
        return replace_card(card)

    async def _schedulable(self, actor: Actor) -> list[str]:
        """The agents `actor` may schedule here, by the rule chat uses."""
        agents = await list_agents_by_tenant(self._runtime.anthropic, tenant_id=actor.tenant_id)
        names = sorted(
            name for agent in agents if (name := agent.metadata.get(MA_METADATA_KEY_NAME))
        )
        if actor.is_admin:
            return names
        here = await self._answers_here(actor)
        return [
            name for name in names if may_schedule_agent(name, is_admin=False, answers_here=here)
        ]

    async def _answers_here(self, actor: Actor) -> str | None:
        async with self._runtime.sessionmaker() as session:
            return await channel_agent(
                session,
                tenant_id=actor.tenant_id,
                channel_id=_place(actor),
                default=self._runtime.deployment_default,
            )

    async def _form(
        self, actor: Actor, values: dict[str, str], error: str | None = None
    ) -> TaskModuleInvokeResponse:
        form = create_form(await self._schedulable(actor), values, error)
        return dialog(NEW_ROUTINE, form)

    async def _open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_notice(NEW_ROUTINE, DENIED)
        if not await self._schedulable(actor):
            if not actor.is_admin:
                return dialog_notice(NEW_ROUTINE, NO_AGENT)
            nouns = RefusalNouns.for_platform("teams")
            return dialog_notice(
                NEW_ROUTINE, NO_AGENTS_YET.format(verb=nouns.verb, setup=nouns.setup)
            )
        return await self._form(actor, {})

    async def _submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, ctx.activity)
        if actor is None:
            return dialog_message(DENIED)
        values = form_values(ctx.activity.value.data)
        if (
            values["agent"]
            and not actor.is_admin
            and not may_schedule_agent(
                values["agent"], is_admin=False, answers_here=await self._answers_here(actor)
            )
        ):
            return dialog_message(NOT_HERE.format(agent=values["agent"]))
        error = await self._create(actor, values)
        if error is not None:
            return await self._form(actor, values, error)
        created = created_notice(values["agent"], values["cron"], values["timezone"])
        if ctx.activity.reply_to_id:
            panel = await self._panel(
                actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin, notice=created
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
        except InvalidScheduleError:
            return "That schedule or time zone isn't valid.\n\nCheck both and try again."
        agent = await find_agent_by_daimon_tag(
            self._runtime.anthropic, tenant_id=actor.tenant_id, name=values["agent"]
        )
        if agent is None:
            return f"No agent named {values['agent']!r} found."
        async with self._runtime.sessionmaker() as session:
            refusal = await panel_save_refusal(
                session,
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                is_admin=actor.is_admin,
                agent=build_agent_ref(agent.name, agent.metadata, values["agent"]),
                channel_id=_place(actor),
            )
        if refusal is not None:
            return admission_refusal_text(
                refusal, RefusalNouns.for_platform("teams"), agent_name=values["agent"]
            )
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
