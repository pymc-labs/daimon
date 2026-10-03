"""The tenant access policy: its rules, and rows an older build wrote."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    AgentRule,
    ChannelRule,
    TenantAccessPolicy,
    is_invoker_allowed,
)
from daimon.core.permissions import agent_permissions, channel_permissions, outside_runs_in
from pydantic import ValidationError


def test_open_policy_admits_everyone_and_limits_nothing() -> None:
    assert is_invoker_allowed(OPEN_ACCESS_POLICY, external_user_id="u1", is_admin=False)
    here = channel_permissions(OPEN_ACCESS_POLICY, channel_id="c1", parent_channel_id="p1")
    assert (here.readers, here.writers) == ("any", "any")


def test_allowlist_admits_listed_users_and_admins_only() -> None:
    policy = TenantAccessPolicy(invoker_user_ids=("u1",))

    assert is_invoker_allowed(policy, external_user_id="u1", is_admin=False)
    assert not is_invoker_allowed(policy, external_user_id="u2", is_admin=False)
    assert is_invoker_allowed(policy, external_user_id="u2", is_admin=True), (
        "an admin must never be locked out by the allowlist"
    )


@pytest.mark.parametrize(
    ("channel_id", "parent_channel_id", "category_id", "expected"),
    [
        ("client", None, None, "none"),
        ("thread", "client", None, "none"),
        ("other", None, "clients-cat", "none"),
        ("other", "general", "internal-cat", "any"),
    ],
)
def test_writers_none_covers_channel_thread_parent_and_category(
    channel_id: str, parent_channel_id: str | None, category_id: str | None, expected: str
) -> None:
    policy = TenantAccessPolicy(
        channel_rules={"client": ChannelRule(writers="none")},
        category_rules={"clients-cat": ChannelRule(writers="none")},
    )
    here = channel_permissions(
        policy, channel_id=channel_id, parent_channel_id=parent_channel_id, category_id=category_id
    )
    assert here.writers == expected


def test_readers_inside_cover_the_channel_and_threads_under_it() -> None:
    policy = TenantAccessPolicy(channel_rules={"vault": ChannelRule(readers="inside")})

    for channel, parent in (("vault", None), ("thread", "vault")):
        here = channel_permissions(policy, channel_id=channel, parent_channel_id=parent)
        assert here.readers == "inside"
    assert channel_permissions(policy, channel_id="general").readers == "any"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TenantAccessPolicy.model_validate({"invoker_user_ids": [], "typo_ids": []})


@pytest.mark.parametrize(
    ("readers", "writers"), [("any", "own"), ("inside", "own"), ("own", "any")]
)
def test_own_goes_on_both_sides(readers: str, writers: str) -> None:
    """Own agents write only a channel they alone read, and read one only they or nobody write."""
    with pytest.raises(ValidationError):
        ChannelRule.model_validate({"readers": readers, "writers": writers})


def test_open_rules_are_not_kept() -> None:
    policy = TenantAccessPolicy(channel_rules={"a": ChannelRule()}, agent_rules={"x": AgentRule()})
    assert policy == OPEN_ACCESS_POLICY


def test_a_row_of_id_lists_reads_as_rules() -> None:
    """A row an older build wrote converts on read; no stored data is rewritten."""
    policy = TenantAccessPolicy.model_validate(
        {
            "protected_channel_ids": ["p", "both"],
            "protected_category_ids": ["cat"],
            "sealed_channel_ids": ["s", "both", "iso"],
            "isolated_channel_ids": ["iso"],
            "agent_channel_pins": {"x": ["iso"], "y": []},
        }
    )
    assert policy == TenantAccessPolicy(
        channel_rules={
            "p": ChannelRule(writers="none"),
            "both": ChannelRule(readers="inside", writers="none"),
            "s": ChannelRule(readers="inside"),
            "iso": ChannelRule(readers="own", writers="own"),
        },
        category_rules={"cat": ChannelRule(writers="none")},
        agent_rules={"x": AgentRule(runs_in=("iso",)), "y": AgentRule(runs_in=())},
    )


def test_a_legacy_row_must_seal_what_it_isolates() -> None:
    with pytest.raises(ValidationError, match="must also be sealed"):
        TenantAccessPolicy.model_validate({"isolated_channel_ids": ["c"]})


def test_a_row_holds_rules_or_lists_not_both() -> None:
    with pytest.raises(ValidationError, match="not both"):
        TenantAccessPolicy.model_validate(
            {"sealed_channel_ids": ["c"], "channel_rules": {"d": {"readers": "inside"}}}
        )


_RULED = TenantAccessPolicy(agent_rules={"daimon-rx": AgentRule(runs_in=("rx-1", "rx-2"))})


@pytest.mark.parametrize(
    ("names", "channel_id", "parent_channel_id", "outside"),
    [
        (("daimon-rx",), "rx-1", None, False),
        (("daimon-rx",), "thr-9", "rx-2", False),
        (("daimon-rx",), "general", None, True),
        (("daimon-rx",), "thr-9", "general", True),
        (("daimon-rx",), None, None, True),
        (("daimon",), "general", None, False),
        ((None, "daimon-rx"), "general", None, True),
        (("daimon", None), None, None, False),
    ],
    ids=[
        "named-channel",
        "thread-under-named",
        "other-channel",
        "thread-under-other",
        "no-channel",
        "agent-without-rule",
        "rule-on-metadata-name",
        "dm-without-rule",
    ],
)
def test_runs_in_limits_only_the_agent_it_is_on(
    names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None,
    outside: bool,
) -> None:
    agent = agent_permissions(_RULED, names)
    assert outside_runs_in(agent, channel_id, parent_channel_id) is outside


def test_runs_in_nothing_refuses_every_channel() -> None:
    """A rule naming no channel runs the agent nowhere but an exempt surface."""
    agent = agent_permissions(
        TenantAccessPolicy(agent_rules={"acme": AgentRule(runs_in=())}), ["acme"]
    )
    for channel in ("C111", None):
        assert outside_runs_in(agent, channel)


def test_every_named_rule_must_allow_the_channel() -> None:
    policy = TenantAccessPolicy(
        agent_rules={
            "Acme Display": AgentRule(runs_in=("C111",)),
            "acme": AgentRule(runs_in=("C111", "C999")),
        }
    )
    agent = agent_permissions(policy, ("Acme Display", "acme"))
    assert not outside_runs_in(agent, "C111")
    assert outside_runs_in(agent, "C999")
