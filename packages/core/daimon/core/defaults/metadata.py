"""MA metadata tagging + display_title construction for the defaults pipeline."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

MA_METADATA_KEY_TENANT = "daimon_tenant"
MA_METADATA_KEY_NAME = "daimon_name"
MA_METADATA_KEY_ACCOUNT = "daimon_account"
MA_METADATA_KEY_MANAGED = "daimon_managed"
MA_METADATA_KEY_SPEC_HASH = "daimon_spec_hash"

# Marks a whole MA workspace as a throwaway one that the test-only workspace
# nuke is allowed to empty. Stamped on a single sentinel agent, never on a
# tenant resource — see `daimon.core.ma.find_workspace_disposable_sentinel`.
MA_METADATA_KEY_WORKSPACE = "daimon_workspace"
MA_METADATA_VALUE_WORKSPACE_DISPOSABLE = "disposable"


def compute_spec_fingerprint(payload: dict[str, Any]) -> str:
    """Stable short digest of a JSON-serializable payload.

    Used by `reconcile_*` to decide whether MA's current shape equals the
    spec we'd write. Stamping this in metadata lets a subsequent reconcile
    skip the MA write entirely when nothing changed, instead of unconditionally
    bumping `agent.version` on every `defaults apply` (which runs on every
    scheduler boot — the L13 idempotency bug).

    The first 16 hex chars of sha256 over JSON with sort_keys=True. Collisions
    at 64 bits are negligible for this use case; false negatives (extra writes)
    are harmless, false positives (missed writes) would matter but are bounded
    by the birthday paradox at 2^32 inputs.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_metadata(
    *,
    tenant_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID | None = None,
    managed: bool = False,
    spec_hash: str | None = None,
) -> dict[str, str]:
    """Return the MA metadata tag for `(tenant_id, local_name)`.

    Used on every POST to `client.beta.agents.create` and
    `client.beta.environments.create` so LIST-by-client-filter can recover the
    daimon tenant and local name deterministically.

    When `account_id` is provided, stamp `daimon_account=str(account_id)` so the
    The `/agent-setup` panel can filter the roster to the invoking user.
    When `account_id` is `None`, the `daimon_account` key is omitted entirely —
    this preserves the "tenant-scoped / no account" semantics of the seeded
    default agent (everyone's agent).

    When `managed=True`, stamp `daimon_managed="true"` so the sweep pass can
    distinguish defaults-owned resources from user forks. Only the defaults
    reconcile callsites pass `managed=True`; CLI-create paths leave it unset
    so user forks are out of sweep scope.

    When `spec_hash` is provided, stamp `daimon_spec_hash=<hash>` so a
    subsequent reconcile can short-circuit to SKIPPED when MA's current
    metadata already carries the same hash (the L13 idempotency contract).
    """
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
    }
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    if spec_hash is not None:
        metadata[MA_METADATA_KEY_SPEC_HASH] = spec_hash
    return metadata


# MA hard-enforced limit on skill display_title length (probed 2026-05-09).
_DISPLAY_TITLE_MAX = 64


def tenant_scoped_display_title(
    *, tenant_id: uuid.UUID, name: str, agent_name: str | None = None
) -> str:
    """Return the canonical tenant-owned skill display_title. THIS IS THE ONLY
    function allowed to produce a tenant-owned skill display_title — use it
    at every create/lookup/dedup/recovery site.

    Prefix is ``f"{str(tenant_id)[:8]}-"`` (9 chars). Body is ``name`` when
    ``agent_name`` is None (seeded shape) or ``f"{agent_name}/{name}"`` (synced shape).

    If prefix + body fits within _DISPLAY_TITLE_MAX (64 chars), return as-is.
    Otherwise truncate: ``f"{prefix}{body[:keep]}~{digest}"`` where ``digest`` is
    the first 4 hex chars of sha256 over the FULL untruncated body, and
    ``keep = 64 - len(prefix) - 5`` (the "~" plus 4 hex chars = 5). The hash is
    over the full body so two long names sharing a long prefix still get distinct
    titles.
    """
    prefix = f"{str(tenant_id)[:8]}-"
    body = name if agent_name is None else f"{agent_name}/{name}"
    full = f"{prefix}{body}"
    if len(full) <= _DISPLAY_TITLE_MAX:
        return full
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:4]
    keep = _DISPLAY_TITLE_MAX - len(prefix) - 5  # 5 = "~" + 4 hex chars
    return f"{prefix}{body[:keep]}~{digest}"


def find_conflicting_skill_body(
    *, existing_bodies: list[str], name: str, agent_name: str | None
) -> str | None:
    """Return the first existing skill body whose MA mount name would collide
    with creating a skill called ``name`` under the given shape, else None.

    MA mounts skills by their internal name (the terminal segment of the
    display_title body), not the full display_title — two distinct skill
    resources with the same terminal name attached to one agent make session
    creation fail with a mount collision. Bodies are tenant-stripped
    display_title bodies (see :func:`strip_tenant_prefix`).

    Rules:
    - Creating a registry skill (``agent_name is None``): any agent-scoped body
      (``"agent/name"``) with the same terminal name conflicts — registry skills
      are attachable to any agent, including that one. An exact registry-shape
      match is NOT a conflict; that is the update-in-place path.
    - Creating an agent-scoped skill: only an exact registry-shape body
      conflicts. Other agents' scoped skills never mount alongside this one.
    """
    for body in existing_bodies:
        is_scoped = "/" in body
        terminal_name = body.rsplit("/", 1)[-1]
        if terminal_name != name:
            continue
        if agent_name is None and is_scoped:
            return body
        if agent_name is not None and not is_scoped:
            return body
    return None


def find_mount_collision(
    *, skill_ids: list[str], body_by_skill_id: dict[str, str]
) -> tuple[str, list[str]] | None:
    """Return ``(mount_name, bodies)`` for the first mount path that two or
    more of ``skill_ids`` would share, else None.

    ``body_by_skill_id`` maps a tenant's custom skill ids to their
    tenant-stripped display_title bodies; ids absent from the map (anthropic
    built-ins, foreign tenants) are skipped — their mounts cannot collide with
    tenant-owned names. The mount name is the body's terminal path segment.
    """
    bodies_by_mount: dict[str, list[str]] = {}
    for skill_id in skill_ids:
        body = body_by_skill_id.get(skill_id)
        if body is None:
            continue
        bodies_by_mount.setdefault(body.rsplit("/", 1)[-1], []).append(body)
    for mount_name, bodies in bodies_by_mount.items():
        if len(bodies) > 1:
            return mount_name, bodies
    return None


def strip_tenant_prefix(*, tenant_id: uuid.UUID, display_title: str) -> str | None:
    """Strip the tenant id-8 prefix from a display_title and return the body.

    Prefix-matches against the KNOWN ``tenant_id`` — never splits on "-" (names
    like ``cli-auth`` contain dashes). Returns the remainder on match, None
    otherwise. None means "not this tenant's skill — filter it out".
    """
    prefix = f"{str(tenant_id)[:8]}-"
    if display_title.startswith(prefix):
        return display_title[len(prefix) :]
    return None
