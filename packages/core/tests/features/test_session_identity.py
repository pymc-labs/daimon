"""Session-identity features: what a new session is built with, and what a stored one reads as."""

from __future__ import annotations

import uuid

from anthropic.types.beta import BetaManagedAgentsCustomSkill
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.session_identity import (
    DEFAULT_IDENTITY,
    IDENTITY_KEYS,
    SessionIdentity,
)
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one
from daimon.core.session_snapshot import SessionSnapshot, session_skills
from daimon.testing.ma_models import ma_agent

_PLATFORM = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_platform", version="1")
_CHANNEL = BetaManagedAgentsCustomSkill(type="custom", skill_id="skill_channel", version="1")


def _snapshot(**values: str) -> FeatureSnapshot:
    place = FeaturePlace(uuid.uuid4(), "discord", "c1", agent_id="a")
    resolved = {key: ResolvedFeature(key, value, "channel") for key, value in values.items()}
    return FeatureSnapshot(place, 1, resolved)


def test_a_session_recorded_before_the_features_reads_as_everything_on() -> None:
    """No stored values means every identity feature on, as those sessions ran."""
    old = SessionIdentity.frozen(())
    assert all(old.on(key) for key in IDENTITY_KEYS), "every key reads on"
    assert SessionIdentity.of(None) == DEFAULT_IDENTITY, "a hand-built admission is the default"


def test_only_values_off_their_default_are_stored() -> None:
    """An all-default identity stores nothing, so its snapshot JSON is unchanged."""
    identity = SessionIdentity.of(_snapshot(platform_prompt="on", memory_shared="off"))
    assert identity.stored() == (("memory_shared", "off"),), "only the change is stored"
    assert SessionIdentity.frozen(identity.stored()) == SessionIdentity({"memory_shared": "off"}), (
        "a stored identity reads back the same"
    )
    dumped = SessionSnapshot.model_fields.get("identity_features")
    assert dumped is not None and dumped.default == (), "absent from old snapshots"


def test_skill_features_pick_platform_channel_and_agent_skills() -> None:
    """Platform and channel skills follow their own feature; agent skills leave the agent's."""
    extra = (_PLATFORM, _CHANNEL)
    ids = frozenset({"skill_platform"})
    no_platform = SessionIdentity.of(_snapshot(platform_skills="off"))
    no_channel = SessionIdentity.of(_snapshot(channel_skills="off"))
    assert no_platform.extra_skills(extra, ids) == (_CHANNEL,), "platform skills off"
    assert no_channel.extra_skills(extra, ids) == (_PLATFORM,), "channel skills off"
    agent = ma_agent(id="a", skills=[{"type": "custom", "skill_id": "skill_own", "version": "1"}])
    own = session_skills(agent, (), agent_skills=True)
    assert [skill.skill_id for skill in own] == ["skill_own"], "the agent's own by default"
    assert session_skills(agent, (), agent_skills=False) == [], "agent skills off"


def test_platform_prompt_off_sends_the_agents_own_prompt() -> None:
    """Off sends no platform variant, so the session runs the agent's own system prompt."""
    off = SessionIdentity.of(_snapshot(platform_prompt="off"))
    assert off.system("Be helpful.", "discord") is None, "no override when off"


def test_memory_features_narrow_each_store() -> None:
    """Each memory feature turns its own store off and leaves the others."""
    identity = SessionIdentity.of(_snapshot(memory_personal="off", memory_channel="off"))
    assert not identity.personal_memory and not identity.channel_memory, "both off"
    assert identity.shared_memory, "shared memory stays on"


def test_backends_without_a_store_fix_the_memory_feature() -> None:
    """Gemini and OpenAI mount the agent-wide store only, so personal memory is fixed off."""
    place = FeaturePlace(uuid.uuid4(), "discord", "c1", agent_id="a", backend="gemini")
    personal = resolve_one(FEATURES["memory_personal"], None, place, [])
    shared = resolve_one(FEATURES["memory_shared"], None, place, [])
    assert (personal.value, personal.unavailable) == ("off", "backend"), "no personal store"
    assert (shared.value, shared.unavailable) == ("on", "backend"), "agent-wide store fixed on"
