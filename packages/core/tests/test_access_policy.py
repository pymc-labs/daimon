"""Pure predicates of the tenant access policy."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    is_invoker_allowed,
    is_outside_agent_pin,
    is_sealed,
    is_write_protected,
)
from pydantic import ValidationError


def test_open_policy_admits_everyone_and_protects_nothing() -> None:
    assert is_invoker_allowed(OPEN_ACCESS_POLICY, external_user_id="u1", is_admin=False)
    assert not is_write_protected(OPEN_ACCESS_POLICY, channel_id="c1", parent_channel_id="p1")
    assert not is_sealed(OPEN_ACCESS_POLICY, channel_id="c1", parent_channel_id="p1")


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
        ("client", None, None, True),
        ("thread", "client", None, True),
        ("other", None, "clients-cat", True),
        ("other", "general", "internal-cat", False),
    ],
)
def test_write_protection_covers_channel_thread_parent_and_category(
    channel_id: str, parent_channel_id: str | None, category_id: str | None, expected: bool
) -> None:
    policy = TenantAccessPolicy(
        protected_channel_ids=("client",), protected_category_ids=("clients-cat",)
    )

    assert (
        is_write_protected(
            policy,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
            category_id=category_id,
        )
        is expected
    )


def test_sealed_covers_the_channel_and_threads_under_it() -> None:
    policy = TenantAccessPolicy(sealed_channel_ids=("vault",))

    assert is_sealed(policy, channel_id="vault")
    assert is_sealed(policy, channel_id="thread", parent_channel_id="vault")
    assert not is_sealed(policy, channel_id="general")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TenantAccessPolicy.model_validate({"invoker_user_ids": [], "typo_ids": []})


_PINNED = TenantAccessPolicy(agent_channel_pins={"daimon-rx": ("rx-1", "rx-2")})


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
        "pinned-channel",
        "thread-under-pinned",
        "other-channel",
        "thread-under-other",
        "no-channel",
        "unpinned-agent",
        "pin-on-metadata-name",
        "unpinned-dm",
    ],
)
def test_agent_pin_confines_only_the_pinned_agent(
    names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None,
    outside: bool,
) -> None:
    assert (
        is_outside_agent_pin(
            _PINNED, agent_names=names, channel_id=channel_id, parent_channel_id=parent_channel_id
        )
        is outside
    )


def test_open_policy_pins_no_agent() -> None:
    assert not is_outside_agent_pin(OPEN_ACCESS_POLICY, agent_names=("daimon-rx",), channel_id=None)


def test_an_empty_stored_pin_refuses_every_channel() -> None:
    """A name pinned to no channels runs nowhere but an exempt surface."""
    policy = TenantAccessPolicy(agent_channel_pins={"acme": ()})
    for channel in ("C111", None):
        assert is_outside_agent_pin(policy, agent_names=("acme",), channel_id=channel)


def test_every_pinned_name_must_allow_the_channel() -> None:
    policy = TenantAccessPolicy(
        agent_channel_pins={"Acme Display": ("C111",), "acme": ("C111", "C999")}
    )
    names = ("Acme Display", "acme")
    assert not is_outside_agent_pin(policy, agent_names=names, channel_id="C111")
    assert is_outside_agent_pin(policy, agent_names=names, channel_id="C999")
