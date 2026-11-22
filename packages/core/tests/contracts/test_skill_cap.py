"""Contract probe for the provider's real per-agent skill limit.

The Discord panel and the chat update path enforce a shared product cap,
``daimon.core.constants.AGENT_SKILL_CAP``, deliberately set below the
provider's own per-agent limit (500) so every backend supports it. That
provider limit is not hardcoded anywhere in this codebase because it has moved
before: a code comment once recorded 20 as the observed ceiling, and a later
run attached 21 skills to a single agent successfully. Hardcoding a second
number invites exactly that kind of silent staleness.

This probe re-establishes the provider's current limit on demand instead of
trusting a stale citation: it lists more skills than any previously observed
ceiling on one agent, reads the provider's own rejection message,
and extracts the number from it with the same phrasing match
``daimon.core.skill_sync.orchestrator._looks_like_skill_cap`` already uses
in production. Run it whenever the product cap needs re-justifying:

    uv run pytest packages/core/tests/contracts/test_skill_cap.py -m contract

Unrun in this environment (no ``DAIMON_TEST_ANTHROPIC_API_KEY``) as of this
plan's execution — see the plan's summary for the operator follow-up.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from anthropic import AsyncAnthropic, BadRequestError
from daimon.core.constants import AGENT_SKILL_CAP

pytestmark = pytest.mark.contract

# Past the documented per-agent limit (500), so the probe trips the limit and
# reads the provider's current number out of the rejection.
_PROBE_SKILL_COUNT = 600

_BASE_AGENT_TOOL_NAMES = ("bash", "read", "edit", "grep", "glob", "write")

# Matches daimon.core.skill_sync.orchestrator._looks_like_skill_cap's phrasing
# ("skills: <n> exceeds maximum of <m> for this organization") and captures the
# provider's maximum.
_CAP_MESSAGE_RE = re.compile(r"skills:.*exceeds maximum of (\d+)")


def _skill_name() -> str:
    return f"contract-test-skill-cap-{uuid.uuid4().hex[:8]}"


def _agent_name() -> str:
    return f"contract-test-skill-cap-agent-{uuid.uuid4().hex[:8]}"


async def test_provider_reports_its_per_agent_skill_limit(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    """Attach past the provider's real limit and read it out of the 400 body."""
    # One skill listed 600 times: creating 600 distinct skills only to be refused
    # is wasteful. If the provider ever rejects the repeats before it counts
    # them, create distinct skills instead.
    with open(skill_zip_path, "rb") as f:
        skill = await anthropic_client.beta.skills.create(
            display_title=_skill_name(),
            files=[("skill.zip", f, "application/zip")],
        )
    skill_ids = [skill.id] * _PROBE_SKILL_COUNT

    # Skills require the read tool to be usable — an agent without the base
    # toolset 400s for an unrelated reason before the cap is ever reached.
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

    skill_params: list[dict[str, Any]] = [
        {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
    ]
    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.agents.update(
            agent.id,
            version=agent.version,
            skills=skill_params,  # type: ignore[arg-type]
        )

    msg = (exc_info.value.message or "").lower()
    match = _CAP_MESSAGE_RE.search(msg)
    assert match is not None, (
        f"could not extract the provider's per-agent skill limit from its rejection "
        f"message; the phrasing may have changed — update _CAP_MESSAGE_RE. Message: {msg!r}"
    )
    provider_limit = int(match.group(1))
    print(f"provider per-agent skill limit observed: {provider_limit}")

    assert provider_limit > AGENT_SKILL_CAP, (
        f"the provider's per-agent skill limit ({provider_limit}) is no longer greater than "
        f"our product cap (AGENT_SKILL_CAP={AGENT_SKILL_CAP}) — the product cap is now at or "
        "above the provider's own limit and AGENT_SKILL_CAP must be lowered in "
        "daimon.core.constants."
    )
