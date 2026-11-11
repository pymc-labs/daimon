"""Every profile that installs a skill bundle attaches an agent's custom and channel skills."""

from __future__ import annotations

from daimon.core.turn.session_skills import attaches_session_skills
from mux.profiles import PROFILES

#: Profiles that take a bundle but carry no template's skills per session, and why.
OTHER_WAY = {
    "anthropic.managed_agents": "the source provider: its skills ride on the agent itself",
    "gemini.inline_reuse": "not yet: its sessions share one catalogue agent; a named loss",
    "selfhosted.loop": "its skills support is unknown until the server is probed",
}


def test_every_profile_with_a_skills_bundle_attaches_custom_and_channel_skills() -> None:
    for profile_id, profile in PROFILES.items():
        if profile.support_for("skills_bundle") == "unsupported":
            assert not attaches_session_skills(profile), profile_id
            continue
        assert attaches_session_skills(profile) or profile_id in OTHER_WAY, profile_id
    for profile_id in OTHER_WAY:
        assert not attaches_session_skills(PROFILES[profile_id]), f"{profile_id} closed its gap"
