"""Long jobs spend money in the background: only a server admin sets them, as on main."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.features.registry import FEATURES, Tier
from daimon.core.features.service import FeatureActor, may_change

_LONG_JOB_KEYS = ("long_jobs", "long_jobs_max_usd", "long_jobs_max_deadline")
_TIERS: tuple[Tier, ...] = ("workspace", "agent")


def _actor(
    *, server_admin: bool, delegated_admin: bool = False, owner: bool = False
) -> FeatureActor:
    """An actor whose admin answer is core's `admin_for_action`, as chat and MCP give it."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin_for_action(
            platform_admin=server_admin, daimon_admin=server_admin or delegated_admin, action=action
        )

    async def owns_agent(_: str) -> bool:
        return owner

    return FeatureActor(uuid.uuid4(), None, "chat", None, admin_for, owns_agent=owns_agent)


@pytest.mark.parametrize("key", _LONG_JOB_KEYS)
@pytest.mark.parametrize("tier", _TIERS)
async def test_an_owner_and_a_delegated_admin_cannot_change_a_long_job_feature(
    key: str, tier: Tier
) -> None:
    spec = FEATURES[key]
    owner = _actor(server_admin=False, owner=True)
    delegated = _actor(server_admin=False, delegated_admin=True)
    assert not await may_change(owner, spec, tier, locked=False, agent_id="ag"), (
        "an agent's owner must not raise a long job cap or turn long jobs on"
    )
    assert not await may_change(delegated, spec, tier, locked=False, agent_id="ag"), (
        "a delegated admin is not a server admin"
    )


@pytest.mark.parametrize("key", _LONG_JOB_KEYS)
@pytest.mark.parametrize("tier", _TIERS)
async def test_a_server_admin_changes_a_long_job_feature(key: str, tier: Tier) -> None:
    actor = _actor(server_admin=True)
    assert await may_change(actor, FEATURES[key], tier, locked=False, agent_id="ag"), (
        "a server admin keeps the right on both tiers"
    )


async def test_an_agent_owner_still_sets_an_ordinary_agent_feature() -> None:
    owner = _actor(server_admin=False, owner=True)
    assert await may_change(owner, FEATURES["routines"], "agent", locked=False, agent_id="ag"), (
        "only the long-job features drop the owner path"
    )
