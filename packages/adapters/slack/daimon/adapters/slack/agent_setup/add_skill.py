"""The Add skill form's submission: preview a pasted SKILL.md, then add it.

The first submit, or one whose text changed, comes back as the same form with
a preview and the previewed hash. A submit of unchanged text closes the form,
and the upload runs after the ack: the caller and the agent are re-checked
live (the pin rule with the panel's channel as the place, then sharing, with
the caller's own sessions left out), then the skill is added as the agent's
own and Details refreshes. The same checks run on the fresh agent right before
the upload and the attach.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import anthropic
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import (
    AGENT_GONE_MESSAGE,
    MANAGED_AGENT_MESSAGE,
    refuse_unless_allowed_for_agent_name,
    refuse_unless_pin_allows,
)
from daimon.adapters.slack.agent_setup.actions import load_details_view
from daimon.adapters.slack.agent_setup.panel_views import ADD_SKILL_INPUT_ID, build_add_skill_form
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_markdown
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

_MAX_ERROR_CHARS = 300


@dataclasses.dataclass(frozen=True)
class AddSkillDecision:
    """The ack body, and what the background run adds when it should run."""

    response_payload: dict[str, Any] | None
    meta: PanelMetadata | None = None
    bundle: SkillBundle | None = None

    @property
    def proceed(self) -> bool:
        return self.meta is not None and self.bundle is not None


class _AddRefused(Exception):
    """A last-moment re-check refused the add; its refusal is already posted."""


def evaluate_add_skill_submission(payload: dict[str, Any]) -> AddSkillDecision:
    """Check the pasted SKILL.md and decide: errors, a (new) preview, or add. Pure."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None or not meta.agent_name:
        return AddSkillDecision(response_payload=None)
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(ADD_SKILL_INPUT_ID) or {}
    typed: dict[str, Any] = block.get(ADD_SKILL_INPUT_ID) or {}
    text = str(typed.get("value") or "")
    try:
        bundle = bundle_from_markdown(text.strip())
    except SkillIngestError as exc:
        return AddSkillDecision(
            response_payload={
                "response_action": "errors",
                "errors": {ADD_SKILL_INPUT_ID: str(exc)[:_MAX_ERROR_CHARS]},
            }
        )
    content_hash = bundle.preview.content_hash
    if meta.skill_hash != content_hash:
        form = build_add_skill_form(
            meta=dataclasses.replace(meta, skill_hash=content_hash),
            text=text,
            preview=bundle.preview,
        )
        return AddSkillDecision(response_payload={"response_action": "update", "view": form})
    return AddSkillDecision(response_payload=None, meta=meta, bundle=bundle)


async def run_add_skill_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    decision: AddSkillDecision,
) -> None:
    """Re-check, add the previewed skill, tell the caller, refresh Details."""
    meta, bundle = decision.meta, decision.bundle
    if meta is None or bundle is None or not meta.agent_name:
        return
    agent_name, name = meta.agent_name, bundle.preview.name
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    async def tell(text: str) -> None:
        await post_ephemeral(
            client, channel_id=meta.channel_id or user_id, user_id=user_id, text=text
        )

    is_admin = await resolve_is_admin(client, user_id=user_id)
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )

    async def refused(agent: BetaManagedAgentsAgent | None) -> bool:
        """Post why the caller may not add to `agent_name` from this panel now, if so."""
        if agent is not None and (
            agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            or MA_METADATA_KEY_ACCOUNT not in agent.metadata
        ):
            await tell(MANAGED_AGENT_MESSAGE)
            return True
        return await refuse_unless_pin_allows(
            runtime,
            client,
            tenant_id=tenant_id,
            agent_name=agent_name,
            channel_id=meta.channel_id,
            user_id=user_id,
            is_admin=is_admin,
            agent=agent,
        ) or await refuse_unless_allowed_for_agent_name(
            runtime,
            client,
            operation="skill_add",
            tenant_id=tenant_id,
            agent_name=agent_name,
            channel_id=meta.channel_id,
            user_id=user_id,
            caller_account_id=actor.account_id,
            agent=agent,
        )

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        if await refused(fresh):
            raise _AddRefused

    found = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    if found is None:
        await tell(AGENT_GONE_MESSAGE)
        return
    try:
        agent = await runtime.anthropic.beta.agents.retrieve(found.id)
        if await refused(agent):
            return
        result = await add_agent_skill(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=tenant_id,
            agent=agent,
            agent_name=agent_name,
            bundle=bundle,
            origin="pasted",
            added_by_account_id=actor.account_id,
            recheck=recheck,
        )
    except _AddRefused:
        return
    except SkillIngestError as exc:
        await tell(f"{exc} Nothing was added.")
        return
    except (DaimonError, anthropic.APIError):
        log.exception("slack.agent_setup.add_skill.failed", agent_name=agent_name)
        await tell(
            f"Adding {escape_mrkdwn(name)} to {escape_mrkdwn(agent_name)} failed. Try again."
        )
        return
    log.info("slack.agent_setup.add_skill.added", agent_name=agent_name, action=result.action)
    done = "already had" if result.action == "unchanged" else "now has"
    await tell(f"{escape_mrkdwn(agent_name)} {done} the skill *{escape_mrkdwn(name)}*.")
    if meta.root_view_id:
        details = await load_details_view(
            runtime,
            tenant_id=tenant_id,
            meta=meta.with_view("details", agent_name=agent_name),
            agent_name=agent_name,
            is_admin=is_admin,
        )
        if details is not None:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=meta.root_view_id, view=details
            )
