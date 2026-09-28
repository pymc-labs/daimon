"""The `routines` command, its card actions and its create dialog.

Mirrors Slack's `/routines` panel and its rules: everyone sees the tenant's
routines; only an admin or the routine's creator may pause, resume, read the
last output or delete one; only an admin may create. Every click re-verifies
the clicker and re-reads the row, so a stale card grants nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime

import anthropic
import structlog
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.interactions import Actor, resolve_actor
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.routines_card import (
    FORM_FIELDS,
    confirm_delete_card,
    create_form,
    dialog,
    dialog_message,
    form_values,
    output_card,
    panel_card,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.routines import can_manage_routine, panel_rows
from daimon.core.stores import routines as store
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    InvokeActivity,
    MessageActivityInput,
    TaskFetchInvokeActivity,
    TaskModuleInvokeResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

GONE = "This routine no longer exists."
NOT_OWNER = "Only the routine's creator or an admin can do that."
ADMIN_ONLY = "Only an admin can create routines."
_FAILED = "Sorry, something went wrong. Please try again."
_ERRORS = (DaimonError, anthropic.APIError, SQLAlchemyError)
_PANEL_EDIT_TIMEOUT_S = 5.0


async def _guarded[T](work: Awaitable[T], failed: T, event: str) -> T:
    """Invoke boundary: log and report a failure, answer with `failed`."""
    try:
        return await work
    except _ERRORS as exc:
        log.error(event, exc_info=exc)
        capture_exception_with_scope(exc)
        return failed


def _replace(card: AdaptiveCard) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionCardResponse(value=card)


def _toast(text: str) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionMessageResponse(value=text)


class RoutinesPanel:
    """Handlers for the command, the panel buttons and the create dialog."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def command(self, context: CommandContext) -> None:
        user_id, is_admin = context.inbound.user_id, context.is_admin
        card = await self._panel(context.tenant_id, user_id=user_id, is_admin=is_admin)
        await context.send(MessageActivityInput().add_card(card))

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await _guarded(self._act(ctx.activity), _toast(_FAILED), "teams.routines.failed")

    async def on_dialog_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(
            self._open(ctx.activity), dialog_message(_FAILED), "teams.routines.failed"
        )

    async def on_dialog_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._submit(ctx), dialog_message(_FAILED), "teams.routines.failed")

    async def _actor(self, activity: InvokeActivity) -> Actor | None:
        return await resolve_actor(
            self._runtime,
            conversation=activity.conversation,
            aad_object_id=activity.from_.aad_object_id,
        )

    async def _panel(
        self, tenant_id: uuid.UUID, *, user_id: str, is_admin: bool, notice: str | None = None
    ) -> AdaptiveCard:
        async with self._runtime.sessionmaker() as session:
            rows = await store.list_routines_for_tenant(session, tenant_id=tenant_id)
        shown, hidden = panel_rows(rows)
        return panel_card(shown, hidden, user_id=user_id, is_admin=is_admin, notice=notice)

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await self._actor(activity)
        if actor is None:
            return _toast(DENIED)
        data = activity.value.action.data
        op = data.get("op")
        notice: str | None = None
        if op != "refresh":
            try:
                routine_id = uuid.UUID(str(data.get("routine")))
            except ValueError:
                return _toast(GONE)
            tenant_id = actor.tenant_id
            async with self._runtime.sessionmaker.begin() as session:
                row = await store.get_routine(session, routine_id, tenant_id=tenant_id)
                if row is None:
                    return _toast(GONE)
                if not can_manage_routine(row, user_id=actor.user_id, is_admin=actor.is_admin):
                    return _toast(NOT_OWNER)
                if op == "output":
                    return _replace(output_card(row))
                if op == "delete":
                    return _replace(confirm_delete_card(row))
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
        return _replace(card)

    async def _agent_names(self, tenant_id: uuid.UUID) -> list[str]:
        agents = await list_agents_by_tenant(self._runtime.anthropic, tenant_id=tenant_id)
        names = (agent.metadata.get(MA_METADATA_KEY_NAME) for agent in agents)
        return sorted(name for name in names if name)

    async def _open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await self._actor(activity)
        if actor is None or not actor.is_admin:
            return dialog_message(DENIED if actor is None else ADMIN_ONLY)
        return dialog(create_form(await self._agent_names(actor.tenant_id), {}))

    async def _submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        actor = await self._actor(ctx.activity)
        if actor is None or not actor.is_admin:
            return dialog_message(DENIED if actor is None else ADMIN_ONLY)
        values = form_values(ctx.activity.value.data)
        error = await self._create(actor, values)
        if error is not None:
            names = await self._agent_names(actor.tenant_id)
            return dialog(create_form(names, values, error))
        created = f"✅ Created routine on {values['agent']} ({values['cron']})."
        if ctx.activity.reply_to_id:
            # Best effort: refresh the panel the dialog was opened from.
            card = await self._panel(
                actor.tenant_id, user_id=actor.user_id, is_admin=True, notice=created
            )
            edit = MessageActivityInput(id=ctx.activity.reply_to_id).add_card(card)
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await asyncio.wait_for(ctx.send(edit), _PANEL_EDIT_TIMEOUT_S)
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
