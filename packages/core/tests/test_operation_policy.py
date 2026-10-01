"""Unit tests for the pure operation-policy table."""

from __future__ import annotations

from typing import get_args

from daimon.core.operation_policy import (
    OperationKind,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)


def _facts(*, managed: bool, reachable: bool) -> TargetFacts:
    return TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=reachable)


def test_spec_edit_refuses_managed_agent_even_for_admin() -> None:
    outcome = decide_operation(
        "agent_spec_edit",
        is_admin=True,
        target=_facts(managed=True, reachable=False),
    )
    assert outcome == "managed_agent", (
        "a spec edit never stamps the reconciler's spec hash, so a managed "
        "agent must refuse even an admin caller"
    )


def test_attachment_allows_admin_on_managed_agent() -> None:
    outcome = decide_operation(
        "repo_bind",
        is_admin=True,
        target=_facts(managed=True, reachable=False),
    )
    assert outcome == "allow", (
        "an admin binding a repo to the seeded/managed agent is the "
        "first-run onboarding step this family exists to allow"
    )


def test_posted_token_operations_allow_non_admin_on_shared_agent() -> None:
    shared = _facts(managed=True, reachable=True)
    for operation in ("key_add", "keys_import", "mcp_connect", "skill_repo_connect"):
        outcome = decide_operation(operation, is_admin=False, target=shared)
        assert outcome == "allow", (
            f"{operation} is a posted-token contribution scoped to the "
            "requester alone, so it must stay open even on a shared agent"
        )


def test_key_replace_and_remove_need_admin_on_shared_agent() -> None:
    reachable_not_managed = _facts(managed=False, reachable=True)
    for operation in ("key_replace", "key_remove", "mcp_replace", "mcp_remove", "repo_bind"):
        outcome = decide_operation(operation, is_admin=False, target=reachable_not_managed)
        assert outcome == "needs_admin", (
            f"{operation} is a destructive attachment write on a reachable "
            "agent, so a non-admin must be refused"
        )


def test_unreachable_attachment_write_is_open_to_non_admin() -> None:
    unreachable = _facts(managed=False, reachable=False)
    outcome = decide_operation("key_remove", is_admin=False, target=unreachable)
    assert outcome == "allow", (
        "an unreachable, unmanaged agent has no shared state to defend, so "
        "any member may write its attachments"
    )


def test_needs_reachability_read_is_false_for_admin_attachment_write() -> None:
    result = needs_reachability_read("repo_bind", is_admin=True, is_daimon_managed=False)
    assert result is False, (
        "an admin's attachment write is decided by the admin check alone, "
        "before reachability would ever be consulted"
    )


def test_needs_reachability_read_matches_decide_operation() -> None:
    for operation in get_args(OperationKind):
        for is_admin in (True, False):
            for is_daimon_managed in (True, False):
                if needs_reachability_read(
                    operation, is_admin=is_admin, is_daimon_managed=is_daimon_managed
                ):
                    continue
                outcome_reachable = decide_operation(
                    operation,
                    is_admin=is_admin,
                    target=_facts(managed=is_daimon_managed, reachable=True),
                )
                outcome_unreachable = decide_operation(
                    operation,
                    is_admin=is_admin,
                    target=_facts(managed=is_daimon_managed, reachable=False),
                )
                assert outcome_reachable == outcome_unreachable, (
                    f"needs_reachability_read said {operation} (admin={is_admin}, "
                    f"managed={is_daimon_managed}) does not depend on reachability, "
                    "but decide_operation disagreed for the two reachability values"
                )


def test_every_operation_kind_has_a_rule() -> None:
    facts_by_scenario = (
        _facts(managed=False, reachable=False),
        _facts(managed=False, reachable=True),
        _facts(managed=True, reachable=False),
        _facts(managed=True, reachable=True),
    )
    for operation in get_args(OperationKind):
        for is_admin in (True, False):
            for target in facts_by_scenario:
                outcome = decide_operation(operation, is_admin=is_admin, target=target)
                assert outcome in ("allow", "needs_admin", "managed_agent"), (
                    f"{operation} produced an outcome outside PolicyOutcome: {outcome!r}"
                )
