"""Model-input features: the guidance predicate, turn-control level and session toolset."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.features.model_input import (
    FULL_HISTORY,
    HistoryDepth,
    SessionToolset,
    guidance_applies,
    turn_controls,
)
from daimon.core.features.registry import FEATURES, limit_int
from daimon.core.features.resolver import ResolvedFeature, TierValue, resolve_feature
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one

PLACE = FeaturePlace(uuid.uuid4(), "web", agent_id="agent_1", backend="anthropic")


def _snapshot(*found: ResolvedFeature) -> FeatureSnapshot:
    defaults = {key: resolve_one(spec, None, PLACE, []) for key, spec in FEATURES.items()}
    return FeatureSnapshot(PLACE, 1, {**defaults, **{f.key: f for f in found}})


def test_model_features_apply_off_chat_platforms() -> None:
    """Model features apply on web, CLI and MCP, unlike the chat-only ones."""
    found = resolve_one(FEATURES["daimon_tools"], None, PLACE, [])
    assert found.unavailable is None, "daimon_tools must apply on the web platform"


def test_guidance_never_applies_to_isolated_agents() -> None:
    """Isolation is fixed: guidance On does not reach an isolated agent."""
    snapshot = _snapshot()
    assert guidance_applies(isolated=False, features=snapshot), "default guidance is On"
    assert not guidance_applies(isolated=True, features=snapshot), "isolated agents get none"
    off = _snapshot(ResolvedFeature("guidance", "off", "agent"))
    assert not guidance_applies(isolated=False, features=off), "Off removes guidance"


def test_daimon_tools_lift_minimal_turn_controls_to_origin() -> None:
    """Daimon tools need the origin ids, so Minimal becomes Origin while they are On."""
    minimal = ResolvedFeature("turn_controls", "minimal", "agent")
    assert turn_controls(_snapshot(minimal)) == "origin", "tools On lift Minimal"
    tools_off = ResolvedFeature("daimon_tools", "off", "agent")
    assert turn_controls(_snapshot(minimal, tools_off)) == "minimal", "tools Off keep Minimal"


def test_toolset_defaults_change_nothing() -> None:
    """With nothing set the session keeps the agent's own tools, minus default-Off capabilities."""
    toolset = SessionToolset.of(_snapshot(), {})
    assert toolset.off_capabilities == {"long_jobs"}, "only the default-Off capability is hidden"
    assert (toolset.daimon_tools, toolset.builtins, toolset.repo_memory, toolset.env_file) == (
        True,
        {},
        True,
        True,
    ), "defaults must not change anything else"


def test_agent_tool_config_is_the_agent_tier() -> None:
    """An unlocked server value yields to the agent's own config; a lock or channel wins."""
    server_off = ResolvedFeature("tool.bash", "off", "workspace")
    assert SessionToolset.of(_snapshot(server_off), {"bash": True}).builtins == {}, (
        "the agent's explicit bash setting beats an unlocked server value"
    )
    assert SessionToolset.of(_snapshot(server_off), {}).builtins == {"bash": False}, (
        "an unset agent tool follows the server"
    )
    locked = ResolvedFeature("tool.bash", "off", "workspace", locked=True)
    assert SessionToolset.of(_snapshot(locked), {"bash": True}).builtins == {"bash": False}
    channel_on = ResolvedFeature("tool.bash", "on", "channel")
    assert SessionToolset.of(_snapshot(channel_on), {"bash": False}).builtins == {"bash": True}


def test_isolated_agent_tools_only_shrink() -> None:
    """A channel cannot switch a builtin on for an isolated agent."""
    channel_on = ResolvedFeature("tool.bash", "on", "channel")
    toolset = SessionToolset.of(_snapshot(channel_on), {"bash": False}, isolated=True)
    assert toolset.builtins == {}, "isolated agents never gain a tool from a place"


def test_chat_history_takes_the_smallest_setting_and_caps_the_channel() -> None:
    """Chat history is a limit: the smallest tier wins, and the channel reads at most 25."""
    spec = FEATURES["history_depth"]
    values = [TierValue("workspace", "25"), TierValue("channel", "50")]
    found = resolve_feature(spec, values)
    assert (found.value, found.tier) == ("25", "workspace"), "a channel cannot raise the limit"
    depth = HistoryDepth.of(_snapshot(found))
    assert (depth.replay, depth.backfill) == (25, 25), "the channel backfill follows the limit"
    ten = HistoryDepth.of(_snapshot(ResolvedFeature("history_depth", "10", "channel")))
    assert (ten.replay, ten.backfill) == (10, 10), "under 25 the backfill shrinks with it"
    assert (FULL_HISTORY.replay, FULL_HISTORY.backfill) == (100, 25), "the default reads 100"
    assert HistoryDepth.of(None) == FULL_HISTORY, "a hand-built admission keeps the default"
    assert all(int(value) >= 0 for value in spec.values), "limit values are counts"


def test_a_limit_is_a_finite_number_read_the_same_way_everywhere() -> None:
    spec = FEATURES["outbound_people"]
    assert spec.accepts("12.0", "deployment") and spec.accepts("0", "deployment")
    assert not any(spec.accepts(bad, "deployment") for bad in ("inf", "-inf", "nan", "1e999", "-1"))
    assert limit_int("12.0") == 12 and limit_int("7") == 7
    for bad in ("inf", "nan", "1e999"):
        with pytest.raises(ValueError):
            limit_int(bad)
    assert (
        HistoryDepth.of(_snapshot(ResolvedFeature("history_depth", "12.0", "deployment"))).replay
        == 12
    )
