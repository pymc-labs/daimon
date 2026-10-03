"""The routine, delivery, turn-reply and shared-agent rules decide exactly as before.

The second half of `test_authz_equivalence`: each `_old_*` below is what a
caller decided before it was routed through `daimon.core.authz.authorize`,
copied from main with its predicates unchanged (I/O and copy stripped). Every
test runs the old and new decisions over a grid and asserts they agree at
every point; a drift fails with the counterexample.
"""

from __future__ import annotations

import itertools
from typing import get_args

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize, build_subject
from daimon.core.operation_policy import OperationKind, PolicyOutcome, TargetFacts
from daimon.core.operation_policy import _decide_operation as new_decide_operation
from daimon.core.routine_delivery import DeliveryTarget, creator_refusal_for, delivery_refusal
from daimon.core.turn.protection import _may_post

# --- main's predicates, copied unchanged ------------------------------------------------------


def _old_is_invoker_allowed(
    policy: TenantAccessPolicy, *, external_user_id: str, is_admin: bool
) -> bool:
    # access_policy.is_invoker_allowed
    if is_admin or not policy.invoker_user_ids:
        return True
    return external_user_id in policy.invoker_user_ids


def _old_is_write_protected(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> bool:
    # access_policy.is_write_protected
    if channel_id in policy.protected_channel_ids:
        return True
    if parent_channel_id is not None and parent_channel_id in policy.protected_channel_ids:
        return True
    if category_unresolved and policy.protected_category_ids:
        return True
    return category_id is not None and category_id in policy.protected_category_ids


def _old_creator_refusal(
    policy: TenantAccessPolicy, *, creator_platform_user_id: str | None, creator_is_admin: bool
) -> str | None:
    # routine_delivery.creator_refusal_for
    if creator_platform_user_id is None or not _old_is_invoker_allowed(
        policy, external_user_id=creator_platform_user_id, is_admin=creator_is_admin
    ):
        return "invoker_not_allowed"
    return None


def _old_delivery_refusal(
    policy: TenantAccessPolicy,
    *,
    target: DeliveryTarget,
    creator_platform_user_id: str | None,
    creator_is_admin: bool,
    parent_channel_id: str | None,
    category_id: str | None,
) -> str | None:
    # routine_delivery.delivery_refusal
    creator_refusal = _old_creator_refusal(
        policy, creator_platform_user_id=creator_platform_user_id, creator_is_admin=creator_is_admin
    )
    if creator_refusal is not None:
        return creator_refusal
    if _old_is_write_protected(
        policy,
        channel_id=target.channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        return "protected_channel"
    return None


_OLD_SPEC: frozenset[str] = frozenset({"agent_spec_edit", "skill_add", "skill_remove"})
_OLD_POSTED_TOKEN: frozenset[str] = frozenset({"key_add", "keys_import", "mcp_connect"})


def _old_reachable_outcome(target: TargetFacts) -> PolicyOutcome:
    # Locality counts for a channel admin only on an agent that is theirs.
    local = target.is_local_to_caller_channels and target.is_held_by_caller
    if target.is_reachable_in_tenant and not local:
        return "needs_admin"
    return "allow"


def _old_decide_operation(operation: str, *, is_admin: bool, target: TargetFacts) -> PolicyOutcome:
    # operation_policy._decide_operation
    if operation in _OLD_POSTED_TOKEN:
        return "allow"
    if operation in _OLD_SPEC:
        if target.is_daimon_managed:
            return "managed_agent"
        if is_admin:
            return "allow"
        return _old_reachable_outcome(target)
    if is_admin:
        return "allow"
    if target.is_daimon_managed:
        return "managed_agent"
    return _old_reachable_outcome(target)


# --- the grid ---------------------------------------------------------------------------------

POLICIES = [
    TenantAccessPolicy(
        protected_channel_ids=protected,
        protected_category_ids=categories,
        invoker_user_ids=invokers,
        agent_channel_pins=pins,
    )
    for protected, categories, invokers, pins in itertools.product(
        [(), ("C1",), ("T1",), ("C1", "C2")],
        [(), ("CAT1",)],
        [(), ("u1",), ("u1", "")],
        # Pins never enter these decisions; a pin, an empty pin and a pin on an
        # empty name prove it.
        [{}, {"acme": ("C2",)}, {"": ()}],
    )
]
CHANNELS = ["C1", "C2", "C3", "T1", ""]
PARENTS = [None, "C1", "C3", ""]
CATEGORIES = [None, "CAT1", "CAT2", ""]
CREATORS = [None, "", "u1", "u2"]


def test_protection_decisions_match() -> None:
    """Turn-reply protection, routine save and routine delivery posts."""
    for policy, channel, parent, category, unresolved in itertools.product(
        POLICIES, CHANNELS, PARENTS, CATEGORIES, (False, True)
    ):
        old = _old_is_write_protected(
            policy,
            channel_id=channel,
            parent_channel_id=parent,
            category_id=category,
            category_unresolved=unresolved,
        )
        place = Place(
            channel_id=channel,
            parent_channel_id=parent,
            category_id=category,
            category_unresolved=unresolved,
        )
        # turn/protection.protection_state
        assert _may_post(policy, place) is not old, (policy, place)
        # the routine callers (scheduler, routine save, Discord/Slack delivery)
        for surface in (Surface.ROUTINE, Surface.CHANNEL):
            new = authorize(
                policy, subject=Subject(), action=Action.POST, surface=surface, place=place
            )
            assert bool(new) is not old, (policy, place, surface)
            assert old is (new.reason == "channel_protected")


def test_creator_decisions_match() -> None:
    """Routine fire (scheduler) and routine delivery's creator gate."""
    for policy, creator, is_admin in itertools.product(POLICIES, CREATORS, (False, True)):
        old = _old_creator_refusal(
            policy, creator_platform_user_id=creator, creator_is_admin=is_admin
        )
        assert (
            creator_refusal_for(policy, creator_platform_user_id=creator, creator_is_admin=is_admin)
            == old
        ), (policy, creator, is_admin)
        if creator is not None:
            # scheduler.main: the fire refuses when the old allowlist check refused
            fire = authorize(
                policy,
                subject=build_subject(is_admin=is_admin, platform_user_id=creator),
                action=Action.ACT_FOR_CREATOR,
                surface=Surface.ROUTINE,
            )
            assert bool(fire) is _old_is_invoker_allowed(
                policy, external_user_id=creator, is_admin=is_admin
            ), (policy, creator, is_admin)


def test_delivery_refusals_match() -> None:
    for policy, channel, parent, category, creator, is_admin in itertools.product(
        POLICIES, ("C1", "C3", "T1"), PARENTS, CATEGORIES, CREATORS, (False, True)
    ):
        target = DeliveryTarget(channel_id=channel, thread_ts=None)
        kwargs = {
            "target": target,
            "creator_platform_user_id": creator,
            "creator_is_admin": is_admin,
            "parent_channel_id": parent,
            "category_id": category,
        }
        assert delivery_refusal(policy, **kwargs) == _old_delivery_refusal(policy, **kwargs), (
            policy,
            kwargs,
        )


def test_no_agent_post_ignores_subject_and_pins() -> None:
    """The protection-only post is the same for every caller, agent key or admin."""
    policy = TenantAccessPolicy(protected_channel_ids=("C1",), agent_channel_pins={"acme": ()})
    for subject in (
        Subject(),
        Subject(is_admin=True, platform_user_id="u1"),
        Subject(is_admin=True, platform_user_id="u1", via_agent_key=True),
    ):
        for channel in ("C1", "C2"):
            decision = authorize(
                policy,
                subject=subject,
                action=Action.POST,
                agent=AgentRef.none(),
                place=Place(channel_id=channel),
            )
            assert bool(decision) is (channel != "C1")


@pytest.mark.parametrize("operation", get_args(OperationKind))
def test_shared_agent_table_matches(operation: OperationKind) -> None:
    for is_admin, managed, reachable, local, held, unattended, unplaced in itertools.product(
        (False, True), repeat=7
    ):
        target = TargetFacts(
            is_daimon_managed=managed,
            is_reachable_in_tenant=reachable,
            is_local_to_caller_channels=local,
            is_held_by_caller=held,
            runs_unattended_beyond_caller=unattended,
            has_unplaced_run=unplaced,
        )
        assert new_decide_operation(
            operation, is_admin=is_admin, target=target
        ) == _old_decide_operation(operation, is_admin=is_admin, target=target), (
            operation,
            is_admin,
            target,
        )


def test_every_operation_kind_is_in_one_old_family() -> None:
    """The vendored table covers every kind main had (a new kind needs a row here)."""
    attachment = {
        "key_replace",
        "key_remove",
        "mcp_replace",
        "mcp_remove",
        "repo_bind",
        "skill_repo_connect",
    }
    assert set(get_args(OperationKind)) == attachment | _OLD_SPEC | _OLD_POSTED_TOKEN
