"""J3/J4/J10: an agent with a memory store still runs a fleet when its root also carries
the caller's own private resources; workers run without those (declared), and what
does block a fleet is named, no longer hidden behind 'memory'.

STAGING 2026-10-24 ('Fork across Argon and Astra'): "This agent can't run a fleet yet:
its memory can't be shared with fleet workers". Workers share the root's store only when
it is the root's only resource; a root that also carried the caller's private carry was
refused, and the memory text masked the obstacle that remained.
"""

from daimon.core.neutral_resources import (
    fleet_root_obstacle,
    fleet_root_obstacles,
    fleet_root_permissions,
)
from daimon.core.sessions import CALLER_RESOURCES_ID
from mux.contracts.resources import ResourceBinding

from .test_fleet_source_permissions import NOW
from .test_fleet_source_permissions import captured as captured
from .test_fleet_source_permissions import owned as owned


def _with(source, *bindings):
    return source.model_copy(
        update={"resource_requirements": (*source.resource_requirements, *bindings)}
    )


async def test_a_memory_root_carrying_the_callers_resources_runs_a_fleet(captured):
    """The caller's private carry is theirs: workers run without it, declared, and the
    root's memory, again its only resource, is still shared read-only."""
    source, admission, grant, fleet, policy, trace = captured
    assert source.memory_store_id is not None
    source = _with(source, ResourceBinding(id=CALLER_RESOURCES_ID, kind="native"))
    assert fleet_root_obstacle(source) is None
    permissions = fleet_root_permissions(
        source, admission, grant, fleet=fleet, policy=policy, now=NOW
    )
    assert "credentials" in permissions.spec.declared_drops
    assert not any(r.id == CALLER_RESOURCES_ID for r in permissions.resources)
    (mount,) = [
        r for r in permissions.resources if r.resource and r.resource.kind == "memory_store"
    ]
    assert mount.native.value["resource"]["access"] == "read_only"
    assert len(trace.requests) == 1


async def test_a_sole_read_only_store_is_still_shared_with_workers(captured):
    """Where sharing works it is unchanged: the store stays mounted read-only."""
    source, admission, grant, fleet, policy, _ = captured
    assert source.memory_read_only is True and not source.resource_requirements
    assert fleet_root_obstacle(source) is None
    permissions = fleet_root_permissions(
        source, admission, grant, fleet=fleet, policy=policy, now=NOW
    )
    assert "memory" not in permissions.spec.declared_drops
    (mount,) = [
        r for r in permissions.resources if r.resource and r.resource.kind == "memory_store"
    ]
    assert mount.native.value["resource"]["access"] == "read_only"


async def test_what_blocks_a_fleet_is_named_not_hidden_behind_memory(captured):
    """An opaque resource workers can't carry still blocks, and it is what is shown:
    memory no longer blocks (it is dropped when it can't be shared)."""
    source = _with(captured[0], ResourceBinding(id="opaque-carry", kind="native"))
    obstacles = fleet_root_obstacles(source)
    assert obstacles == ("source_definition",)
    assert fleet_root_obstacle(source) == "source_definition"
