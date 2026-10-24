"""Unit tests for the pure operation-policy table."""

from __future__ import annotations

from typing import get_args

import pytest
from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.operation_policy import (
    OperationKind,
    TargetFacts,
    decide_operation,
    delegated_action_for_operation,
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


def test_server_wide_github_connect_stays_admin_only() -> None:
    no_target = TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=False)
    assert decide_operation("github_connect", is_admin=False, target=no_target) == "needs_admin"
    assert decide_operation("github_connect", is_admin=True, target=no_target) == "allow"


def test_posted_token_operations_allow_non_admin_on_shared_agent() -> None:
    shared = _facts(managed=True, reachable=True)
    for operation in ("key_add", "keys_import"):
        outcome = decide_operation(operation, is_admin=False, target=shared)
        assert outcome == "allow", (
            f"{operation} is a posted-token contribution scoped to the "
            "requester alone, so it must stay open even on a shared agent"
        )


def test_key_replace_and_remove_need_admin_on_shared_agent() -> None:
    reachable_not_managed = _facts(managed=False, reachable=True)
    for operation in (
        "key_replace",
        "key_remove",
        "mcp_replace",
        "mcp_remove",
        "repo_bind",
        "skill_repo_connect",
    ):
        outcome = decide_operation(operation, is_admin=False, target=reachable_not_managed)
        assert outcome == "needs_admin", (
            f"{operation} is a destructive attachment write on a reachable "
            "agent, so a non-admin must be refused"
        )


def test_skill_repo_connect_refuses_non_admin_on_managed_agent() -> None:
    outcome = decide_operation(
        "skill_repo_connect", is_admin=False, target=_facts(managed=True, reachable=False)
    )
    assert outcome == "managed_agent", (
        "a skill-repo import attaches skills to the agent, so a member must not "
        "be able to point one at the seeded agent"
    )


def test_skill_repo_connect_refuses_non_admin_on_unbound_agent() -> None:
    outcome = decide_operation(
        "skill_repo_connect", is_admin=False, target=_facts(managed=False, reachable=False)
    )
    assert outcome == "needs_admin", "unreachable does not establish ownership"


def test_unreachable_attachment_write_requires_admin() -> None:
    unreachable = _facts(managed=False, reachable=False)
    outcome = decide_operation("key_remove", is_admin=False, target=unreachable)
    assert outcome == "needs_admin", "unbound agents may hold shared state"


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
                outcome_owned = decide_operation(
                    operation,
                    is_admin=is_admin,
                    target=_facts(managed=is_daimon_managed, reachable=False).model_copy(
                        update={"owned_by_caller": True}
                    ),
                )
                assert outcome_owned == outcome_unreachable, (
                    f"needs_reachability_read skipped the owner read for {operation} "
                    f"(admin={is_admin}, managed={is_daimon_managed})"
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


def test_reachable_agent_still_needs_admin() -> None:
    outcome = decide_operation(
        "agent_spec_edit", is_admin=False, target=_facts(managed=False, reachable=True)
    )
    assert outcome == "needs_admin"


def test_skill_add_and_remove_follow_the_spec_family() -> None:
    """Managed refused for all; admin allowed; other callers refused."""
    for operation in ("skill_add", "skill_remove"):
        managed = decide_operation(
            operation, is_admin=True, target=_facts(managed=True, reachable=False)
        )
        assert managed == "managed_agent", f"{operation}: the managed agent is forked, not edited"
        shared = _facts(managed=False, reachable=True)
        assert decide_operation(operation, is_admin=True, target=shared) == "allow", operation
        assert decide_operation(operation, is_admin=False, target=shared) == "needs_admin", (
            f"{operation}: a member may not change an agent others talk to"
        )
        unrouted = _facts(managed=False, reachable=False)
        assert decide_operation(operation, is_admin=False, target=unrouted) == "needs_admin", (
            f"{operation}: unbound agents require an admin"
        )


_OWNED = TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=False, owned_by_caller=True)


@pytest.mark.parametrize(
    "operation",
    ["agent_spec_edit", "skill_add", "skill_remove", "mcp_connect", "mcp_replace", "mcp_remove"],
)
def test_the_owner_may_change_its_agents_spec_and_connections(operation: OperationKind) -> None:
    assert decide_operation(operation, is_admin=False, target=_OWNED) == "allow"
    unowned = _OWNED.model_copy(update={"owned_by_caller": False})
    assert decide_operation(operation, is_admin=False, target=unowned) == "needs_admin"


@pytest.mark.parametrize(
    "operation", ["repo_bind", "skill_repo_connect", "github_connect", "github_grant"]
)
def test_the_owner_may_not_spend_the_workspaces_github_installation(
    operation: OperationKind,
) -> None:
    assert decide_operation(operation, is_admin=False, target=_OWNED) == "needs_admin"


def test_ownership_never_unlocks_a_defaults_managed_agent() -> None:
    managed = _OWNED.model_copy(update={"is_daimon_managed": True})
    assert decide_operation("agent_spec_edit", is_admin=False, target=managed) == "managed_agent"
    assert decide_operation("mcp_connect", is_admin=False, target=managed) == "managed_agent"


def test_a_member_s_change_to_an_unmanaged_agent_reads_its_owner() -> None:
    assert needs_reachability_read("agent_spec_edit", is_admin=False, is_daimon_managed=False)
    assert needs_reachability_read("mcp_connect", is_admin=False, is_daimon_managed=False)
    assert not needs_reachability_read("repo_bind", is_admin=False, is_daimon_managed=False)
    assert not needs_reachability_read("key_add", is_admin=False, is_daimon_managed=False)
    assert not needs_reachability_read("agent_spec_edit", is_admin=False, is_daimon_managed=True)


def test_delegation_never_exempts_credential_or_connection_operations() -> None:
    shared = _facts(managed=False, reachable=True)
    for operation in get_args(OperationKind):
        action = delegated_action_for_operation(operation)
        effective = admin_for_action(platform_admin=False, daimon_admin=True, action=action)
        if operation in {"agent_spec_edit", "skill_add", "skill_remove"}:
            assert effective
            assert decide_operation(operation, is_admin=effective, target=shared) == "allow"
        else:
            assert action is None and not effective
            if operation not in {"key_add", "keys_import"}:
                assert (
                    decide_operation(operation, is_admin=effective, target=shared) == "needs_admin"
                )
    assert "credentials" not in set(DelegatedAction)
    assert "mcp_connections" not in set(DelegatedAction)
