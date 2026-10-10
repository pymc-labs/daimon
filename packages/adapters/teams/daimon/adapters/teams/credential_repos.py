"""What the repo and skill-repo dialogs write: the agent's GitHub token, and the skill attach.

Teams twins of Slack's `store_inline_pat` and `_attach_skills_to_requested_agent`;
adapters never import each other, so the rules are restated here.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence

import anthropic
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, find_attach_mount_collision
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import upsert_credential_encrypted
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.stores.agent_github_binding import set_agent_github_binding

log = structlog.get_logger()


async def store_agent_pat(runtime: TeamsRuntime, *, agent_id: uuid.UUID, pat: str) -> str:
    """Encrypt `pat` as the agent's own GitHub credential; returns its `ma_secret_ref`.

    Stored under the agent as principal, so no other agent resolves it.
    """
    fernet = runtime.turn_deps.fernet
    if fernet is None:
        raise DaimonError("no encryption keys configured")
    await upsert_credential_encrypted(
        sessionmaker=runtime.sessionmaker,
        fernet=fernet,
        principal_id=agent_id,
        github_login="(inline-pat)",
        plaintext_token=pat,
        scopes=tuple(runtime.settings.github.oauth_scopes),
    )
    async with runtime.sessionmaker.begin() as session:
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=agent_id)
    return f"inline-pat:{agent_id}"


@dataclasses.dataclass(frozen=True, slots=True)
class SkillAttach:
    """Whether the imported skills reached the agent, and the card's note when not."""

    note: str
    attached: bool
    agent_name: str | None


async def attach_imported_skills(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    outcomes: Sequence[ResourceOutcome],
) -> SkillAttach:
    """Attach the just-imported skills to the agent the request named. Never raises:
    the import already succeeded, so a failure here is partial and reported."""
    skill_ids = sorted(
        o.anthropic_id
        for o in outcomes
        if o.anthropic_id is not None and o.action in (Action.CREATED, Action.UPDATED)
    )
    if not skill_ids:
        return SkillAttach(note="Nothing new to attach.", attached=False, agent_name=None)
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        return SkillAttach(note="That agent no longer exists.", attached=False, agent_name=None)
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        # Admins included: an attach never stamps the reconciler's spec hash.
        note = f"{agent.name} is a built-in agent.\n\nAsk me to make a copy and add them there."
        return SkillAttach(note=note, attached=False, agent_name=agent.name)
    new_skills: list[BetaManagedAgentsSkillParams] = [
        {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
    ]

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        merged = merge_skills_with_ma(new_skills, fresh)
        collision = await find_attach_mount_collision(
            runtime.anthropic, tenant_id=tenant_id, skills=merged
        )
        if collision is not None:
            raise DaimonError(f"cannot attach: {collision}")
        return await runtime.anthropic.beta.agents.update(
            fresh.id, version=fresh.version, skills=merged
        )

    try:
        await update_agent_with_version_retry(runtime.anthropic, agent.id, _apply)
    except (DaimonError, anthropic.APIStatusError) as err:
        log.warning("teams.credential.skill_attach_failed", err_type=type(err).__name__)
        note = "Attaching them did not finish. Ask again to retry."
        return SkillAttach(note=note, attached=False, agent_name=agent.name)
    note = f"Attached {len(skill_ids)} to `{agent.name}`."
    return SkillAttach(note=note, attached=True, agent_name=agent.name)
