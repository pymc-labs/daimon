"""Deterministic UUID derivation for MA-prefixed agent ids and platform tenant identity.

MA returns ids like `agent_017vXa...` which are NOT UUIDs. Several local
tables key on a UUID per `(tenant, agent)` pair (e.g. agent_repo_binding).
The MCP token broker also computes the same UUID from the JWT's `agent_id`
claim. Both call sites MUST agree — hence a single helper.

This module also owns the frozen tenant namespace UUID and `derive_tenant_uuid`,
which keys on `(platform, workspace_id)` to produce a deterministic tenant UUID
stable across DB resets and processes.

The namespace UUIDs below are FROZEN. Do not change them once shipped — any
change invalidates every row keyed by uuid5 under the old namespace.
If a re-key is genuinely needed, that's a migration, not a code change.
"""

from __future__ import annotations

import uuid

from daimon.core.stores.domain import Platform

# Frozen namespace UUID. Generated once via `uuid.uuid4()` on 2026-05-12.
# DO NOT CHANGE — see module docstring. A re-key invalidates every agent_repo_binding
# row keyed by uuid5 under the old namespace (that's a migration, not a code change).
_DAIMON_AGENT_NS = uuid.UUID("51339389-531c-48b2-a356-c63b5e1f3787")

# Frozen namespace UUID. Generated once via `uuid.uuid4()` on 2026-05-29.
# DO NOT CHANGE — see module docstring. A re-key invalidates every tenants row
# keyed by uuid5 under the old namespace (that's a migration, not a code change).
_DAIMON_TENANT_NS = uuid.UUID("ca6a5c2f-55da-44ae-81ec-3a1703b5b67b")


def derive_agent_uuid(*, tenant_id: uuid.UUID, ma_agent_id: str) -> uuid.UUID:
    """UUID5 derived from (tenant, ma_agent_id).

    Deterministic — same inputs always produce the same UUID.
    Tenant-scoped — different tenants with the same MA id derive different UUIDs.
    Collision-resistant — uuid5 inherits SHA-1's collision properties.

    Used by:
      - RepoAuthModal.on_submit (writes agent_repo_binding keyed by this UUID)
      - (future) MCP token broker (mints JWT.agent_id from this UUID)
    Both call sites MUST go through this helper. Do not re-derive ad hoc.
    """
    return uuid.uuid5(_DAIMON_AGENT_NS, f"{tenant_id}/{ma_agent_id}")


def derive_tenant_uuid(*, platform: Platform, workspace_id: str) -> uuid.UUID:
    """UUID5 derived from (platform, workspace_id) — the deterministic tenant anchor.

    Deterministic — same inputs always produce the same tenant UUID across DB
    resets and processes. Platform-generic so a future Slack tenant derives
    without a guild-shaped hack. CLI/no-guild sentinel = (platform="cli",
    workspace_id="local").

    Frozen under _DAIMON_TENANT_NS, separate from _DAIMON_AGENT_NS so the two
    identity domains re-key independently.

    NOTE: defined here, NOT wired into the live bootstrap path.
    """
    return uuid.uuid5(_DAIMON_TENANT_NS, f"{platform}:{workspace_id}")
