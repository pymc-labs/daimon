"""A session that can't be created because one of its skills is gone.

Managed Agents rejects a session whose agent pins, or whose channel adds, a
skill (or skill version) that no longer exists: `400 Could not resolve one or
more skills: skill "<id>" version "<v>" not found`. Every turn fails that way
until the reference is removed, so "try again" is the wrong advice. This turns
that rejection into copy that names the skill and says who must remove it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import anthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
)
from daimon.core.errors import UserFacingError

_UNRESOLVED = re.compile(
    r"Could not resolve one or more skills: skill \"(?P<skill_id>[^\"]+)\""
    r"(?: version \"(?P<version>[^\"]*)\")?"
)


class SkillUnavailable(UserFacingError):
    """The session was refused because a skill it mounts no longer exists.

    `skill_id` names it when known, so a caller may retry without a skill it
    added itself (`daimon.core.turn.prepare.create_ma_session`).
    """

    def __init__(self, message: str, *, skill_id: str | None = None) -> None:
        super().__init__(message)
        self.skill_id = skill_id


def unresolved_skill_id(error: BaseException) -> tuple[str, str | None] | None:
    """The skill id (and version) a session create was refused for, or None."""
    if not isinstance(error, anthropic.BadRequestError):
        return None
    match = _UNRESOLVED.search(str(error))
    if match is None:
        return None
    return match.group("skill_id"), match.group("version")


def skill_unavailable(
    *,
    skill_id: str,
    agent: BetaManagedAgentsAgent,
    channel_skills: Sequence[BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill],
    channel_skill_name: str | None,
) -> SkillUnavailable:
    """The user-facing error for `skill_id`, attributed to the channel or the agent.

    A deleted skill's title is gone from Managed Agents, so a channel skill is
    named from its channel rule and an agent's skill by its id.
    """
    from_channel = any(skill.skill_id == skill_id for skill in channel_skills) and not any(
        skill.skill_id == skill_id for skill in agent.skills
    )
    if from_channel and channel_skill_name:
        label = f'"{channel_skill_name}" ({skill_id})'
    else:
        label = f'"{skill_id}"'
    if from_channel:
        where = "this channel adds"
        fix = (
            "Ask an admin to remove it from this channel's skills "
            "(remove_channel_skill, or `daimon channels skills remove`), then ask again."
        )
    else:
        where = f"{agent.name} uses"
        fix = (
            f"Ask an admin to remove it from {agent.name}'s skills "
            "(remove_skill), or add it again, then ask again."
        )
    return SkillUnavailable(
        f"Daimon couldn't start this conversation: the skill {label} that {where} "
        f"no longer exists. {fix}",
        skill_id=skill_id,
    )


def report_skill_unavailable(skill_id: str) -> SkillUnavailable:
    """The error for a report reader whose copied skill is gone: only re-publishing repairs it.

    A report reader's agent is re-derived from its source when the report is
    re-published, without the deleted skill. The report host matches on
    "Ask the report's publisher to re-publish", so keep that wording.
    """
    return SkillUnavailable(
        f'This report\'s agent uses the skill "{skill_id}", which no longer exists, so it '
        "can't answer. Ask the report's publisher to re-publish it."
    )


__all__ = [
    "SkillUnavailable",
    "report_skill_unavailable",
    "skill_unavailable",
    "unresolved_skill_id",
]
