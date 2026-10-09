"""The declared provider profiles and a lookup by id.

Support levels are what the provider documents today. Anything without
evidence is left undeclared, which reads as `unknown` and is refused at
admission; the conformance suite is what moves a capability out of
`unknown`.
"""

from __future__ import annotations

from collections.abc import Mapping

from mux.contracts.errors import InvalidConfig
from mux.contracts.profile import Profile
from mux.profiles.anthropic import MANAGED_AGENTS
from mux.profiles.gemini import INLINE_REUSE
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE

PROFILES: Mapping[str, Profile] = {
    p.profile_id: p for p in (MANAGED_AGENTS, PERSISTENT_WORKSPACE, CONVERSATION_ONLY, INLINE_REUSE)
}


def get_profile(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise InvalidConfig(f"unknown profile {profile_id!r}") from None


__all__ = [
    "CONVERSATION_ONLY",
    "INLINE_REUSE",
    "MANAGED_AGENTS",
    "PERSISTENT_WORKSPACE",
    "PROFILES",
    "get_profile",
]
