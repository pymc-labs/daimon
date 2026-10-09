"""The channel settings dialog, opened from Who answers where: environment,
permissions, channel admins and channel skills of one channel the caller picks.

The panel lives in the 1:1 chat, so the caller names the channel. The rights
mirror Discord and Slack: a channel admin sets only their own channels'
environment, through `authorize_environment_pick`; permissions and channel
admin grants and channel skills stay with server admins, permissions go
through core `set_channel_rule` and skills through `add_skill_to_channel`.
Every submit re-verifies the clicker and re-reads their grants, so a stale or
forged card grants nothing, and every write, allowed or not, is audited with
`record_panel_write`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Final, cast, get_args

import anthropic
import structlog
from daimon.adapters.teams import channel_settings_card as cards
from daimon.adapters.teams.card_actions import (
    FAILED,
    SENDING_PANEL_ERRORS,
    Actor,
    card_actor,
    dialog,
    dialog_message,
    get_or_create_account,
    guarded,
    submitted_fields,
)
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.answering_map import load_answering_map
from daimon.core.authz import Subject, build_subject
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    InvalidChannelAdminIds,
    load_live_subject,
    normalize_channel_admin_ids,
)
from daimon.core.channel_environments import (
    NOT_OFFERED_NOTE,
    authorize_environment_pick,
    build_clear_environment_note,
    build_limited_network_confirm,
    build_limited_network_refusal,
    build_missing_environment_note,
    build_set_environment_note,
    list_environment_names,
    load_panel_hidden_environment_names,
    may_pick_environment_in,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.channel_rules import (
    ChannelRuleRefused,
    as_readers,
    as_writers,
    channel_rule_status,
    set_channel_rule,
)
from daimon.core.channel_skills import REFUSALS, add_skill_to_channel
from daimon.core.errors import DaimonError, SkillsListTruncatedError
from daimon.core.panel_audit import PanelOp, PanelOutcome, record_panel_write
from daimon.core.permissions import channel_rule, own_reader_channels
from daimon.core.routine_delivery import teams_channel_of
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    get_channel_admins,
    list_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.teams_installations import list_teams_installations
from microsoft_teams.api import (
    TaskFetchInvokeActivity,
    TaskModuleInvokeResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext

log = structlog.get_logger(__name__)

ChannelNames = Callable[[str], Awaitable[Mapping[str, str | None]]]
"""One team's standard channels by id, with their names; empty when unreadable."""

CHANNELS_NEED_ADMIN: Final = (
    "Changing a channel's settings needs a server admin or an admin of that channel."
)
SERVER_ADMIN_ONLY: Final = (
    "Only a server admin can change a channel's permissions, admins or skills. Nothing changed."
)
SKILLS_UNREADABLE: Final = "This organisation's skills could not all be read. Nothing was added."
UNKNOWN_CHANNEL: Final = "That isn't a Teams channel id. Nothing changed."
_AUDIT_OPS: Final[Mapping[cards.ChannelOp, PanelOp]] = {
    "environment": "environment",
    "rule": "channel_rule",
    "admins": "channel_admins",
    "skills": "channel_skills",
}
_MAX_ENVIRONMENT_OPTIONS: Final = 99
_MAX_OPTION_VALUE: Final = 250


async def _guarded[T](work: Awaitable[T], failed: T) -> T:
    return await guarded(work, failed, "teams.channel_settings.failed", errors=SENDING_PANEL_ERRORS)


def _channel(raw: object) -> str | None:
    """The channel a picked or typed id names (a thread names its channel); None if neither."""
    channel = teams_channel_of(str(raw or "").strip())
    if channel is None:
        return None
    try:
        normalized, _, _ = normalize_channel_admin_ids(
            "teams", channel_id=channel, role_ids=(), user_ids=()
        )
    except InvalidChannelAdminIds:
        return None
    return normalized


class ChannelSettingsDialog:
    """Handlers for the dialog's open and its submits."""

    def __init__(self, runtime: TeamsRuntime, *, channel_names: ChannelNames) -> None:
        self._runtime = runtime
        self._channel_names = channel_names

    async def on_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._open(ctx.activity), dialog_message(FAILED))

    async def on_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._submit(ctx.activity), dialog_message(FAILED))

    async def _subject(self, actor: Actor) -> Subject:
        async with self._runtime.sessionmaker() as session:
            return await load_live_subject(
                session,
                tenant_id=actor.tenant_id,
                platform="teams",
                caller=ChannelAdminCaller(
                    platform_user_id=actor.user_id, is_server_admin=actor.is_admin
                ),
            )

    async def _listed(self, tenant_id: uuid.UUID) -> dict[str, str | None]:
        async with self._runtime.sessionmaker() as session:
            installs = await list_teams_installations(session, tenant_id=tenant_id)
        listed: dict[str, str | None] = {}
        for install in installs:
            listed |= await self._channel_names(install.team_id)
        return listed

    async def _picker(
        self, actor: Actor, subject: Subject, notice: str | None = None
    ) -> TaskModuleInvokeResponse:
        listed = await self._listed(actor.tenant_id)
        if actor.is_admin:
            async with self._runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=actor.tenant_id)
                grants = await list_channel_admins(
                    session, tenant_id=actor.tenant_id, platform="teams"
                )
            # Channels already set up stay reachable when their team can't be listed.
            extra = [*own_reader_channels(policy), *(grant.channel_id for grant in grants)]
            channels = cards.visible_channels(listed, extra)
        else:
            mine = sorted(subject.administered_channel_ids)
            channels = cards.visible_channels({c: listed.get(c) for c in mine}, ())
        form = cards.channel_picker_form(channels, free_entry=actor.is_admin, notice=notice)
        return dialog("Channel settings", form)

    async def _open(self, activity: TaskFetchInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        subject = await self._subject(actor)
        if not actor.is_admin and not subject.administered_channel_ids:
            return dialog_message(CHANNELS_NEED_ADMIN)
        return await self._picker(actor, subject)

    async def _settings(
        self, actor: Actor, subject: Subject, channel_id: str, notice: str | None = None
    ) -> TaskModuleInvokeResponse:
        """The channel's form as this caller may change it, read fresh."""
        tenant_id = actor.tenant_id
        async with self._runtime.sessionmaker() as session:
            may_pick = await may_pick_environment_in(
                session, tenant_id=tenant_id, subject=subject, channel_id=channel_id
            )
            answering_map = await load_answering_map(
                session,
                tenant_id=tenant_id,
                platform="teams",
                default=self._runtime.deployment_default,
            )
            policy = await load_access_policy(session, tenant_id=tenant_id)
            grant = await get_channel_admins(
                session, tenant_id=tenant_id, platform="teams", channel_id=channel_id
            )
            skills = await list_channel_skills(
                session, tenant_id=tenant_id, platform="teams", channel_id=channel_id
            )
        picker = None
        if may_pick:
            try:
                async with self._runtime.sessionmaker() as session:
                    hidden = await load_panel_hidden_environment_names(
                        session,
                        self._runtime.anthropic,
                        tenant_id=tenant_id,
                        channel_id=channel_id,
                        is_admin=actor.is_admin,
                        default=self._runtime.deployment_default,
                    )
                names = await list_environment_names(
                    self._runtime.anthropic, tenant_id=tenant_id, hidden=hidden
                )
            except anthropic.APIError:
                log.warning("teams.channel_settings.environments_unlisted", exc_info=True)
                names = []
            picker = plan_environment_picker(
                answering_map,
                channel_id=channel_id,
                names=names,
                limit=_MAX_ENVIRONMENT_OPTIONS,
                max_value_length=_MAX_OPTION_VALUE,
            )
        name = (await self._listed(tenant_id)).get(channel_id)
        settings = cards.ChannelSettings(
            channel_id=channel_id,
            label=cards.channel_label(channel_id, name),
            picker=picker,
            rule=channel_rule_status(policy, channel_id) if actor.is_admin else None,
            admin_user_ids=(grant.user_ids if grant else ()) if actor.is_admin else None,
            skills=tuple(skills) if actor.is_admin else None,
        )
        return dialog("Channel settings", cards.channel_settings_form(settings, notice=notice))

    async def _audit(
        self, actor: Actor, op: PanelOp, *, outcome: PanelOutcome, reason: str
    ) -> None:
        await record_panel_write(
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            platform="teams",
            platform_user_id=actor.user_id,
            op=op,
            outcome=outcome,
            reason=reason,
        )

    async def _submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return dialog_message(DENIED)
        data = submitted_fields(activity.value.data)
        subject = await self._subject(actor)
        op = data.get("op")
        if op not in get_args(cards.ChannelOp):
            return await self._picker(actor, subject)
        op = cast("cards.ChannelOp", op)
        typed = data.get("channel_id") if actor.is_admin else None
        channel_id = _channel(typed or data.get("channel"))
        if channel_id is None:
            return await self._picker(actor, subject, UNKNOWN_CHANNEL)
        audit_op = _AUDIT_OPS.get(op)
        if not actor.is_admin and channel_id not in subject.administered_channel_ids:
            if audit_op is not None:
                await self._audit(actor, audit_op, outcome="denied", reason="needs_admin")
            return dialog_message(CHANNELS_NEED_ADMIN)
        if audit_op is None:
            return await self._settings(actor, subject, channel_id)
        if audit_op != "environment" and not actor.is_admin:
            await self._audit(actor, audit_op, outcome="denied", reason="needs_admin")
            return dialog_message(SERVER_ADMIN_ONLY)
        if audit_op == "environment":
            notice = await self._save_environment(actor, subject, channel_id, data)
        elif audit_op == "channel_rule":
            notice = await self._change_rule(actor, channel_id, data)
        elif audit_op == "channel_skills":
            notice = await self._save_skills(actor, channel_id, data)
        else:
            notice = await self._save_admins(actor, channel_id, data)
        return await self._settings(actor, subject, channel_id, notice)

    async def _save_environment(
        self,
        actor: Actor,
        subject: Subject,
        channel_id: str,
        data: Mapping[str, object],
    ) -> str:
        """As Slack's and Discord's picks: `authorize_environment_pick`, then the write."""
        try:
            name = parse_environment_option(str(data.get("environment") or ""))
        except ValueError:
            return NOT_OFFERED_NOTE
        async with self._runtime.sessionmaker() as session:
            pick = await authorize_environment_pick(
                session,
                self._runtime.anthropic,
                tenant_id=actor.tenant_id,
                subject=subject,
                channel_id=channel_id,
                environment_name=name,
                default=self._runtime.deployment_default,
            )
        if not pick.decision:
            await self._audit(
                actor, "environment", outcome="denied", reason=f"authz:{pick.decision.reason}"
            )
            if pick.decision.reason == "not_a_reader":
                return build_limited_network_refusal(environment_name=name)
            return CHANNELS_NEED_ADMIN
        if name is not None and pick.missing:
            return build_missing_environment_note(name)
        if pick.needs_confirm:
            await self._audit(actor, "environment", outcome="denied", reason="needs_confirm")
            return build_limited_network_confirm(environment_name=name, panel=True)
        name = pick.environment_name or name
        account_id = await get_or_create_account(self._runtime, actor)
        async with self._runtime.sessionmaker.begin() as session:
            previous = await save_scope_environment(
                session,
                tenant_id=actor.tenant_id,
                channel_id=channel_id,
                environment_name=name,
                actor_account_id=account_id,
            )
        await self._audit(actor, "environment", outcome="allowed", reason="completed")
        log.info(
            "teams.channel_settings.environment_saved",
            channel_id=channel_id,
            environment_name=name,
            previous_environment_name=previous,
        )
        if name is None:
            return build_clear_environment_note(channel=channel_id, cleared=previous is not None)
        return build_set_environment_note(environment_name=name, channel=channel_id)

    async def _change_rule(
        self,
        actor: Actor,
        channel_id: str,
        data: Mapping[str, object],
    ) -> str:
        """A server admin's permissions change, as Slack's and Discord's controls make it."""
        readers, writers = as_readers(data.get("readers")), as_writers(data.get("writers"))
        extra = data.get("extra") or "none"
        if readers is None or writers is None or extra not in cards.RULE_EXTRAS:
            return "Pick who can read it and who can post. Nothing changed."
        copy = extra == "copy"
        if copy:
            readers = "own"
        async with self._runtime.sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        # A side left as it was follows the other to and from own (`resolve_rule`).
        current = channel_rule(policy, channel_id)
        public_url = self._runtime.settings.mcp.public_url
        label = (await self._listed(actor.tenant_id)).get(channel_id) if copy else None
        try:
            change = await set_channel_rule(
                self._runtime.anthropic,
                self._runtime.sessionmaker,
                tenant_id=actor.tenant_id,
                platform="teams",
                channel_id=channel_id,
                readers=None if readers == current.readers else readers,
                writers=None if writers == current.writers else writers,
                # Only a server admin reaches this, re-checked by the caller.
                subject=build_subject(is_admin=True, platform_user_id=actor.user_id),
                default=self._runtime.deployment_default,
                actor_account_id=await get_or_create_account(self._runtime, actor),
                copy=copy,
                channel_label=label,
                public_url=str(public_url) if public_url is not None else None,
                release_agents=extra == "release",
            )
        except ChannelRuleRefused as exc:
            await self._audit(actor, "channel_rule", outcome="denied", reason=f"rule:{exc.reason}")
            return f"{exc} Nothing changed."
        except DaimonError as exc:  # a copy that can't be made
            await self._audit(actor, "channel_rule", outcome="error", reason="failed")
            return f"{exc} Nothing changed."
        await self._audit(actor, "channel_rule", outcome="allowed", reason="completed")
        return " ".join(change.notes)

    async def _save_admins(
        self,
        actor: Actor,
        channel_id: str,
        data: Mapping[str, object],
    ) -> str:
        """A server admin's grant: Entra object ids, as the chat tools take them."""
        try:
            _, _, users = normalize_channel_admin_ids(
                "teams",
                channel_id=channel_id,
                role_ids=(),
                user_ids=cards.parse_admin_ids(str(data.get("admins") or "")),
            )
        except InvalidChannelAdminIds as exc:
            await self._audit(actor, "channel_admins", outcome="error", reason="invalid_ids")
            return f"{exc}. Nothing changed."
        account_id = await get_or_create_account(self._runtime, actor)
        async with self._runtime.sessionmaker.begin() as session:
            if users:
                await set_channel_admins(
                    session,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    channel_id=channel_id,
                    role_ids=[],
                    user_ids=users,
                    actor_account_id=account_id,
                )
            else:
                await delete_channel_admins(
                    session, tenant_id=actor.tenant_id, platform="teams", channel_id=channel_id
                )
        await self._audit(actor, "channel_admins", outcome="allowed", reason="completed")
        log.info("teams.channel_settings.channel_admins_saved", users=len(users))
        return "Channel admins saved." if users else "The channel has no admins of its own now."

    async def _save_skills(
        self,
        actor: Actor,
        channel_id: str,
        data: Mapping[str, object],
    ) -> str:
        """A server admin's channel skills, as Slack's form saves them: removals, then an add."""
        picked = str(data.get("skill_remove") or "")
        remove = [skill_id for skill_id in picked.split(",") if skill_id]
        add = str(data.get("skill_add") or "").strip()
        if not remove and not add:
            return "Name a skill to add or tick one to remove. Nothing changed."
        notes: list[str] = []
        if remove:
            # Counted from the rows deleted: a forged id or a second Save removes nothing.
            removed = 0
            async with self._runtime.sessionmaker.begin() as session:
                for skill_id in remove:
                    removed += await remove_channel_skill(
                        session,
                        tenant_id=actor.tenant_id,
                        platform="teams",
                        channel_id=channel_id,
                        skill_id=skill_id,
                    )
            await self._audit(actor, "channel_skills", outcome="allowed", reason="completed")
            notes.append(f"Removed {removed}." if removed else "Those were already removed.")
        if add:
            notes.append(await self._add_skill(actor, channel_id, add))
        return " ".join(notes)

    async def _add_skill(self, actor: Actor, channel_id: str, skill: str) -> str:
        account_id = await get_or_create_account(self._runtime, actor)
        try:
            async with self._runtime.sessionmaker.begin() as session:
                added = await add_skill_to_channel(
                    session,
                    self._runtime.anthropic,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    channel_id=channel_id,
                    skill=skill,
                    default=self._runtime.deployment_default,
                    actor_account_id=account_id,
                )
        except (SkillsListTruncatedError, anthropic.APIError):
            await self._audit(actor, "channel_skills", outcome="error", reason="skills_unreadable")
            return SKILLS_UNREADABLE
        if isinstance(added, str):
            await self._audit(actor, "channel_skills", outcome="denied", reason=added)
            return REFUSALS[added]
        await self._audit(actor, "channel_skills", outcome="allowed", reason="completed")
        log.info("teams.channel_settings.skill_added", skill_id=added.skill_id)
        return f"Added {added.name}."
