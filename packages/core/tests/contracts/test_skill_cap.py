"""Contract probe: the provider accepts an agent with ``AGENT_SKILL_CAP`` skills.

The setup panel and the chat update path enforce a shared product cap,
``daimon.core.constants.AGENT_SKILL_CAP``, chosen so every backend carries it.
The provider's own per-agent limit is higher, but it has moved before (a code
comment once recorded 20 as the ceiling, and a later run attached 21 skills to
one agent successfully), so this probe checks the one fact the product relies
on: an agent with that many distinct skills is accepted. Run it whenever the
cap changes:

    uv run pytest packages/core/tests/contracts/test_skill_cap.py -m contract

It creates and then deletes ``AGENT_SKILL_CAP`` skills and one agent.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from anthropic import AsyncAnthropic
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.ma import delete_skill_and_versions

pytestmark = pytest.mark.contract

_BASE_AGENT_TOOL_NAMES = ("bash", "read", "edit", "grep", "glob", "write")


def _skill_name() -> str:
    return f"contract-test-skill-cap-{uuid.uuid4().hex[:8]}"


def _agent_name() -> str:
    return f"contract-test-skill-cap-agent-{uuid.uuid4().hex[:8]}"


async def test_provider_accepts_an_agent_with_the_product_cap_of_skills(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    skill_ids: list[str] = []
    agent_id: str | None = None
    try:
        # Sequential, not concurrent: skill creation is rate-sensitive.
        for _ in range(AGENT_SKILL_CAP):
            with open(skill_zip_path, "rb") as f:
                skill = await anthropic_client.beta.skills.create(
                    display_title=_skill_name(),
                    files=[("skill.zip", f, "application/zip")],
                )
            skill_ids.append(skill.id)

        # Skills require the read tool to be usable: an agent without the base
        # toolset 400s for an unrelated reason.
        base_toolset: dict[str, Any] = {
            "type": "agent_toolset_20260401",
            "configs": [{"name": name} for name in _BASE_AGENT_TOOL_NAMES],
            "default_config": {"permission_policy": {"type": "always_allow"}},
        }
        agent = await anthropic_client.beta.agents.create(
            name=_agent_name(),
            model={"id": "claude-haiku-4-5"},
            tools=[base_toolset],  # type: ignore[list-item]
        )
        agent_id = agent.id
        skill_params: list[dict[str, Any]] = [
            {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
        ]
        updated = await anthropic_client.beta.agents.update(
            agent.id,
            version=agent.version,
            skills=skill_params,  # type: ignore[arg-type]
        )
        assert len(updated.skills) == AGENT_SKILL_CAP, (
            f"the provider must accept {AGENT_SKILL_CAP} skills on one agent; "
            "AGENT_SKILL_CAP in daimon.core.constants is above its limit otherwise"
        )
    finally:
        if agent_id is not None:
            await anthropic_client.beta.agents.archive(agent_id)
        for skill_id in skill_ids:
            await delete_skill_and_versions(anthropic_client, skill_id)
