"""Pure predicates of the tenant access policy."""

from __future__ import annotations

import pytest
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    is_invoker_allowed,
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
