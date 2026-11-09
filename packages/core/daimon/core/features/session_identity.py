"""Session-identity features: what a new session is built with, frozen in its snapshot.

A live session keeps the values it was created with (`SessionSnapshot.identity_features`),
so changing one never replaces it; `/new` or a replacement picks up the current values.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeatureSnapshot

if TYPE_CHECKING:
    from daimon.core.session_snapshot import MaSkill

IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "platform_prompt",
    "platform_skills",
    "agent_skills",
    "channel_skills",
    "memory_personal",
    "memory_shared",
    "memory_channel",
)
IDENTITY_RESOLVE_KEYS: Final[tuple[str, ...]] = (*IDENTITY_KEYS, "tool.read")
"""What to resolve for `SessionIdentity.of`: `channel_skills` depends on the read tool."""


@dataclass(frozen=True)
class SessionIdentity:
    """Values by key; a missing key is its default, so a session recorded before these
    features existed reads as everything on, as it ran."""

    values: Mapping[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def of(cls, features: FeatureSnapshot | None) -> SessionIdentity:
        """The current values at the place `features` was resolved."""
        if features is None:
            return DEFAULT_IDENTITY
        found = features.features
        return cls({key: found[key].value for key in IDENTITY_KEYS if key in found})

    @classmethod
    def frozen(cls, stored: Collection[tuple[str, str]]) -> SessionIdentity:
        """What a recorded session was created with (`SessionSnapshot.identity_features`)."""
        return cls(dict(stored))

    def on(self, key: str) -> bool:
        return self.values.get(key, FEATURES[key].default) != "off"

    def stored(self) -> tuple[tuple[str, str], ...]:
        """Only values off their default, so an all-default snapshot keeps its JSON."""
        return tuple(sorted((k, v) for k, v in self.values.items() if v != FEATURES[k].default))

    def system(self, agent_system: str | None, platform: str | None) -> str | None:
        """The prompt sent in place of the agent's own (`system_for_platform`), if any."""
        from daimon.core.platform_system import system_for_platform

        return system_for_platform(agent_system, platform) if self.on("platform_prompt") else None

    def extra_skills(
        self, channel_skills: Sequence[MaSkill], platform_skill_ids: Collection[str]
    ) -> tuple[MaSkill, ...]:
        """The turn's platform and channel skills these values keep."""
        platform, channel = self.on("platform_skills"), self.on("channel_skills")
        return tuple(
            skill
            for skill in channel_skills
            if (platform if skill.skill_id in platform_skill_ids else channel)
        )

    @property
    def agent_skills(self) -> bool:
        return self.on("agent_skills")

    @property
    def personal_memory(self) -> bool:
        """Only ever narrows a caller's own rule for mounting personal memory."""
        return self.on("memory_personal")

    @property
    def shared_memory(self) -> bool:
        return self.on("memory_shared")

    @property
    def channel_memory(self) -> bool:
        return self.on("memory_channel")


DEFAULT_IDENTITY: Final = SessionIdentity()
