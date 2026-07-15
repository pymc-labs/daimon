"""Skill SkillRef -> SDK skill-param resolution.

THIS MODULE IS THE D-02 CHOKEPOINT. It is the ONLY place where bare authoring
names (``SkillRef.skill_id``, chat skill strings) become canonical
tenant-scoped display_titles. Callers MUST pass bare authoring names; they
MUST NOT pre-prefix. One rule: every custom-skill lookup goes through here.

Called by ``reconcile_agents.py`` (seed path), ``agent_setup/write.py``
(Discord panel), ``commands/agents.py`` (CLI admin), and ``tools/agents.py``
(MCP chat). All four callers pass ``tenant_id``; the prefix logic lives here
and nowhere else.
"""

from __future__ import annotations

import uuid
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSkillParams
from daimon.core.defaults.ma_index import find_skill_by_display_title, list_skills_lenient
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.errors import DefaultsError
from daimon.core.specs import SkillRef


async def resolve_refs(
    client: AsyncAnthropic,
    *,
    refs: list[SkillRef],
    tenant_id: uuid.UUID,
) -> list[BetaManagedAgentsSkillParams]:
    """Resolve authoring-time SkillRef list to MA skill parameters.

    Anthropic built-in skills (``ref.type == "anthropic"``) are passed through
    as-is -- built-ins never carry tenant prefixes.

    Custom skills are resolved via MA lookup using the canonical
    tenant-scoped display_title (built here from the bare ``ref.skill_id``
    authoring name). No bare-title fallback exists (D-05) -- strict
    prefixed-only lookup from day one.
    """
    resolved: list[BetaManagedAgentsSkillParams] = []
    for ref in refs:
        if ref.type == "anthropic":
            resolved.append({"type": "anthropic", "skill_id": ref.skill_id})
        else:
            canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=ref.skill_id)
            sk = await find_skill_by_display_title(client, canonical, on_truncation="degrade")
            if sk is None:
                raise DefaultsError(
                    f"custom skill {ref.skill_id!r} not found "
                    "in this tenant's skill namespace on MA"
                )
            resolved.append({"type": "custom", "skill_id": sk.id})
    return resolved


async def resolve_skill_names(
    client: AsyncAnthropic,
    entries: list[str | BetaManagedAgentsSkillParams],
    *,
    tenant_id: uuid.UUID,
) -> list[BetaManagedAgentsSkillParams]:
    """Resolve a mixed list of skill names and skill-param dicts.

    String entries are resolved to ``{"type": "custom", "skill_id": <MA id>}``
    via canonical-title lookup (bare authoring name -> tenant-prefixed title ->
    MA lookup), preserving input order.

    Dict entries with ``type == "anthropic"`` pass through unchanged
    (built-ins need no tenant prefix).

    Any dict entry where ``type != "anthropic"`` (e.g. raw MA ids like
    ``{"type": "custom", "skill_id": "skill_01XXX"}``) is rejected with a
    ``DefaultsError`` -- raw MA ids and non-anthropic skill params cannot be
    attached via this path; skills must be referenced by bare name within the
    caller's own namespace (D-09, closes RESEARCH Pitfall 6).

    Misses are aggregated into a single ``DefaultsError`` that names every
    unresolved skill and lists the CALLER'S OWN available bare names (no
    cross-tenant title leak). The available list is fetched only when at
    least one name failed to resolve.
    """
    resolved: list[BetaManagedAgentsSkillParams] = []
    unresolved: list[str] = []
    rejected_dicts: list[Any] = []

    for entry in entries:
        if isinstance(entry, str):
            canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=entry)
            sk = await find_skill_by_display_title(client, canonical, on_truncation="degrade")
            if sk is None:
                unresolved.append(entry)
                continue
            resolved.append({"type": "custom", "skill_id": sk.id})
        else:
            # Dict entry: only anthropic type passes through; everything else is rejected.
            if entry.get("type") == "anthropic":
                resolved.append(entry)
            else:
                rejected_dicts.append(entry)

    if rejected_dicts:
        raw = ", ".join(repr(d) for d in rejected_dicts)
        raise DefaultsError(
            f"raw skill ids and non-anthropic skill params cannot be attached: {raw}; "
            "skills must be referenced by bare name within the caller's own namespace"
        )

    if unresolved:
        rows, truncated = await list_skills_lenient(client)
        # Build the available list: only this tenant's custom skills (bare names)
        # plus anthropic built-in names. Cross-tenant titles are never shown.
        available: list[str] = []
        for sk in rows:
            if sk.source == "custom" and sk.display_title is not None:
                bare = strip_tenant_prefix(tenant_id=tenant_id, display_title=sk.display_title)
                if bare is not None:
                    available.append(bare)
            elif sk.source == "anthropic" and sk.display_title is not None:
                available.append(sk.display_title)
        names = ", ".join(repr(name) for name in unresolved)
        avail = ", ".join(available) if available else "(none)"
        msg = f"skill {names} not found; available: {avail}"
        if truncated:
            msg += (
                " (note: the skill list is truncated at the MA API page limit; "
                "some skills may be missing)"
            )
        raise DefaultsError(msg)

    return resolved
