"""The channel skills form's submission: extra skills one channel's agent runs with.

The form is pushed from Who answers where for workspace admins only; a
channel's own admins can't change them. The submission re-checks admin status
live, removes the ticked skills, adds the named one, and refreshes the routing
view underneath. What a channel may add is `daimon.core.channel_skills`'s.
"""

from __future__ import annotations

import dataclasses
import functools
from typing import Any, Final

import anthropic
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.actions import (
    CHANNEL_SKILLS_NEED_ADMIN_MESSAGE,
    load_routing_view,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    CHANNEL_SKILLS_ADD_INPUT_ID,
    CHANNEL_SKILLS_REMOVE_INPUT_ID,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.channel_skills import REFUSALS, ChannelSkillRefusal, add_skill_to_channel
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.channel_skills import remove_channel_skill
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

UNREADABLE: Final = "Couldn't read all the skills.\n\nNo skill was added."


@dataclasses.dataclass(frozen=True)
class ChannelSkillsSubmission:
    meta: PanelMetadata
    add: str
    remove: tuple[str, ...]


def evaluate_channel_skills_submission(payload: dict[str, Any]) -> ChannelSkillsSubmission | None:
    """The form's panel metadata, skill to add and skills to remove, or None. Pure."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None or not meta.channel_id:
        return None
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    add_block: dict[str, Any] = values.get(CHANNEL_SKILLS_ADD_INPUT_ID) or {}
    add_input: dict[str, Any] = add_block.get(CHANNEL_SKILLS_ADD_INPUT_ID) or {}
    add = str(add_input.get("value") or "").strip()
    remove_block: dict[str, Any] = values.get(CHANNEL_SKILLS_REMOVE_INPUT_ID) or {}
    picked: dict[str, Any] = remove_block.get(CHANNEL_SKILLS_REMOVE_INPUT_ID) or {}
    options: list[dict[str, Any]] = picked.get("selected_options") or []
    remove = tuple(str(option.get("value") or "") for option in options)
    return ChannelSkillsSubmission(meta=meta, add=add, remove=tuple(r for r in remove if r))


async def run_channel_skills_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    submission: ChannelSkillsSubmission,
) -> None:
    """Remove the ticked skills, add the named one, then refresh Who answers where."""
    meta = submission.meta
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    audit = functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="channel_skills",
    )

    async def refuse(text: str) -> None:
        await post_ephemeral(client, channel_id=meta.channel_id, user_id=user_id, text=text)

    if not await resolve_is_admin(client, user_id=user_id):
        await audit(outcome="denied", reason="needs_admin")
        await refuse(CHANNEL_SKILLS_NEED_ADMIN_MESSAGE)
        return
    async with runtime.sessionmaker.begin() as session:
        for skill_id in submission.remove:
            await remove_channel_skill(
                session,
                tenant_id=tenant_id,
                platform="slack",
                channel_id=meta.channel_id,
                skill_id=skill_id,
            )
    if submission.remove:
        await audit(outcome="allowed", reason="completed")
    added: ChannelSkillRow | ChannelSkillRefusal | None = None
    if submission.add:
        try:
            async with runtime.sessionmaker.begin() as session:
                actor = await get_or_create_platform_principal(
                    session, platform="slack", external_id=user_id, tenant_id=tenant_id
                )
                added = await add_skill_to_channel(
                    session,
                    runtime.anthropic,
                    tenant_id=tenant_id,
                    platform="slack",
                    channel_id=meta.channel_id,
                    skill=submission.add,
                    default=runtime.deployment_default,
                    actor_account_id=actor.account_id,
                )
        except (SkillsListTruncatedError, anthropic.APIError):
            await audit(outcome="error", reason="skills_unreadable")
            await refuse(UNREADABLE)
    if isinstance(added, str):
        await audit(outcome="denied", reason=added)
        await refuse(REFUSALS[added])
    elif added is not None:
        await audit(outcome="allowed", reason="completed")
        log.info("slack.agent_setup.channel_skills.added", skill_id=added.skill_id)
    if meta.root_view_id:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=meta.root_view_id,
            view=await load_routing_view(
                runtime, client, tenant_id=tenant_id, meta=meta.with_view("routing"), is_admin=True
            ),
        )
