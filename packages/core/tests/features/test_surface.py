"""The words, choices and schema every surface draws a feature from."""

from __future__ import annotations

import pytest
from daimon.core.features.registry import DEFAULT, FEATURES, TIERS
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.service import TierSetting
from daimon.core.features.surface import (
    CATEGORIES,
    Choice,
    choice,
    feature_schema,
    parse_choice,
    source_text,
    tier_options,
    tier_word,
    where,
)


def test_every_registered_key_is_in_the_schema() -> None:
    schema = feature_schema()
    assert [f["key"] for f in schema] == list(FEATURES)
    for row in schema:
        spec = FEATURES[row["key"]]
        assert row["values"][0]["value"] == next(iter(spec.values))
        assert row["category"] in CATEGORIES and row["tiers"] == list(spec.tiers)


def test_choices_round_trip_for_every_key_tier_and_value() -> None:
    for spec in FEATURES.values():
        for tier in spec.tiers:
            if tier == "deployment":
                continue
            for value in (*spec.values, DEFAULT):
                for locked in (False, True):
                    agent = "a1" if tier == "agent" else None
                    raw = choice(spec.key, tier, value, locked=locked, agent=agent)
                    assert parse_choice(raw) == Choice(spec.key, tier, value, locked, agent)


@pytest.mark.parametrize(
    "raw",
    [
        "approvals:thread:maybe",
        "colour:server:on",
        "a:b",
        "approvals:server:off:x",
        "approvals:deployment:on",
        "approvals:thread:on@a1",
        "approvals:agent:on@",
        None,
        3,
    ],
)
def test_a_forged_choice_names_nothing(raw: object) -> None:
    assert parse_choice(raw) is None


def test_tier_options_leave_out_what_is_already_set() -> None:
    spec = FEATURES["participation"]
    own = TierSetting("channel", "on")
    offered = tier_options(spec, "channel", own, can_lock=True)
    assert choice("participation", "channel", "on") not in [c for c, _ in offered]
    assert choice("participation", "channel", DEFAULT) in [c for c, _ in offered]
    assert any(c.endswith(":lock") for c, _ in offered)
    assert not any(
        c.endswith(":lock") for c, _ in tier_options(spec, "channel", own, can_lock=False)
    )


def test_a_builtin_tool_at_the_agent_tier_is_off_or_default_never_locked() -> None:
    spec = FEATURES["tool.bash"]
    offered = tier_options(spec, "agent", TierSetting("agent", "on"), can_lock=True, agent="a1")
    assert [c for c, _ in offered] == [
        choice("tool.bash", "agent", value, agent="a1") for value in ("off", DEFAULT)
    ]


def test_native_authority_keys_are_never_lockable() -> None:
    spec = FEATURES["approvals"]
    assert spec.authority.native
    offered = tier_options(spec, "channel", TierSetting("channel", "on"), can_lock=True)
    assert not any(c.endswith(":lock") for c, _ in offered)


def test_words_follow_the_platform() -> None:
    assert tier_word("workspace", "discord") == "server"
    assert tier_word("workspace", "slack") == "workspace"
    assert where("channel", "discord") == "this channel" and where("agent", None) == "the agent"
    assert set(TIERS) >= {"thread", "channel", "workspace", "agent", "deployment"}


def test_source_text_says_where_the_value_comes_from() -> None:
    spec = FEATURES["cost_footer"]
    on, off = next(iter(spec.values)), list(spec.values)[1]
    default = ResolvedFeature(spec.key, on, "default", False, None)
    assert source_text(spec, default, "discord").endswith("the default")
    locked = ResolvedFeature(spec.key, off, "workspace", True, None)
    assert source_text(spec, locked, "discord").endswith("locked by the server")
    gone = ResolvedFeature(spec.key, on, "default", False, "platform")
    assert source_text(spec, gone, "discord") == "Not available here"
