"""J3/J4/J10: an agent with a memory store, carrying the caller's private resources,
still runs a fleet. Workers never carry the caller's logins or private carry, or a GitHub
App session's vault; they share the root's memory only where its mount carries over
exactly, and otherwise run without it; what does block a fleet is named.

STAGING 2026-10-24 ('Fork across Argon and Astra'): "This agent can't run a fleet yet:
its memory can't be shared with fleet workers". The worker ceiling kept the projector's
caller-private bindings, so the memory proof (memory as the only resource) failed and the
'memory' text masked the rest (with the dsh reviews of this incident).
"""

from daimon.core.caller_resources import (
    APP_SESSION_VAULT_PREFIX,
    CALLER_RESOURCES_ID,
    CALLER_VAULT_PREFIX,
)
from daimon.core.neutral_resources import (
    fleet_root_obstacle,
    fleet_root_obstacles,
    fleet_root_permissions,
)
from mux.contracts.resources import ResourceBinding

from .test_fleet_source_permissions import NOW
from .test_fleet_source_permissions import captured as captured
from .test_fleet_source_permissions import owned as owned


def _with(source, *bindings):
    return source.model_copy(
        update={"resource_requirements": (*source.resource_requirements, *bindings)}
    )


def _permissions(captured, source):
    _, admission, grant, fleet, policy, _ = captured
    return fleet_root_permissions(source, admission, grant, fleet=fleet, policy=policy, now=NOW)


async def test_a_memory_root_carrying_the_callers_private_carry_runs_a_fleet(captured):
    """The caller's carry is excluded (declared `files`); the root's memory, again its
    only resource, is still shared with workers read-only."""
    source = captured[0]
    assert source.memory_store_id is not None
    source = _with(source, ResourceBinding(id=CALLER_RESOURCES_ID, kind="native"))
    assert fleet_root_obstacles(source) == ()
    permissions = _permissions(captured, source)
    assert "files" in permissions.spec.declared_drops
    assert "credentials" not in permissions.spec.declared_drops
    assert not any(r.id == CALLER_RESOURCES_ID for r in permissions.resources)
    (mount,) = [
        r for r in permissions.resources if r.resource and r.resource.kind == "memory_store"
    ]
    assert mount.native.value["resource"]["access"] == "read_only"


async def test_a_sole_read_only_store_is_still_shared_with_workers(captured):
    """Where sharing works it is unchanged: the store stays mounted read-only."""
    source = captured[0]
    assert source.memory_read_only is True and not source.resource_requirements
    assert fleet_root_obstacle(source) is None
    permissions = _permissions(captured, source)
    assert "memory" not in permissions.spec.declared_drops
    (mount,) = [
        r for r in permissions.resources if r.resource and r.resource.kind == "memory_store"
    ]
    assert mount.native.value["resource"]["access"] == "read_only"


async def test_no_caller_vault_login_ever_reaches_a_worker(captured):
    """The hold variant (`caller-mcp:...:hold:<key>`) included: excluded, not merely
    declared dropped, so no worker resource carries the caller's login."""
    source = captured[0]
    base = f"{CALLER_VAULT_PREFIX}{source.tenant_id}:{source.resource_scope.account_id}:{source.agent_id}"
    for reference in (f"{base}:hold:held-key", f"{base}:slack-turn:not-a-uuid"):
        # As the projector builds a held caller vault: its login plus the caller's carry.
        root = _with(
            source,
            ResourceBinding(id=reference, kind="credential", credential_ref=reference),
            ResourceBinding(id=CALLER_RESOURCES_ID, kind="native"),
        )
        permissions = _permissions(captured, root)
        assert not any(
            (r.id or "").startswith(CALLER_VAULT_PREFIX)
            or (r.credential_ref or "").startswith(CALLER_VAULT_PREFIX)
            for r in permissions.resources
        )
        assert "credentials" not in permissions.spec.declared_drops


async def test_a_github_app_session_vault_never_reaches_a_worker(captured):
    source = captured[0]
    vault = f"{APP_SESSION_VAULT_PREFIX}{source.tenant_id}:{source.agent_id}"
    source = _with(source, ResourceBinding(id=vault, kind="native"))
    assert fleet_root_obstacle(source) is None
    permissions = _permissions(captured, source)
    assert "repository" in permissions.spec.declared_drops
    assert not any(r.id == vault for r in permissions.resources)


async def test_what_blocks_a_fleet_is_named_not_hidden_behind_memory(captured):
    source = _with(captured[0], ResourceBinding(id="opaque-carry", kind="native"))
    assert fleet_root_obstacles(source) == ("source_definition",)
    assert fleet_root_obstacle(source) == "source_definition"
