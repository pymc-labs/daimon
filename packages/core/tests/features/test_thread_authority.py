"""Who may set a thread's value is one core rule, which chat and the web both ask."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, may_change, thread_open_to_anyone

_THREAD_KEYS = tuple(key for key, spec in FEATURES.items() if "thread" in spec.tiers)


async def _no_admin(_: DelegatedAction | None) -> bool:
    return False


def _member() -> FeatureActor:
    return FeatureActor(uuid.uuid4(), None, "chat", None, _no_admin)


def test_some_features_are_open_to_anyone_in_a_thread_and_most_are_not() -> None:
    open_keys = [key for key in _THREAD_KEYS if thread_open_to_anyone(key)]
    assert open_keys, "the rule must open at least one feature to anyone"
    assert len(open_keys) < len(_THREAD_KEYS), "the rule must not open every feature"


@pytest.mark.parametrize("key", _THREAD_KEYS)
async def test_a_member_may_set_a_thread_value_only_where_the_rule_opens_it(key: str) -> None:
    allowed = await may_change(_member(), FEATURES[key], "thread", locked=False, agent_id=None)
    assert allowed is thread_open_to_anyone(key), (
        f"{key}: may_change and thread_open_to_anyone must agree"
    )


@pytest.mark.parametrize("key", _THREAD_KEYS)
async def test_a_member_may_never_lock_a_thread_value(key: str) -> None:
    assert not thread_open_to_anyone(key, locked=True), f"{key}: a Lock is an admin's"
    assert not await may_change(_member(), FEATURES[key], "thread", locked=True, agent_id=None), (
        f"{key}: a member's Lock must be refused"
    )


def test_an_unknown_key_is_not_open_to_anyone() -> None:
    assert not thread_open_to_anyone("no_such_feature"), "an unknown key opens nothing"
