"""Client-side LIST filters for finding daimon-tagged resources on MA.

Agents and environments are matched by metadata (daimon_tenant +
daimon_name). Skills are matched by display_title — MA skill list items
do not expose metadata.

On multi-match, the most recently created resource is adopted; the
others are left alone and a warning is logged.
"""

from __future__ import annotations

import uuid
from typing import Literal

import sentry_sdk
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, SkillListResponse
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import SkillsListTruncatedError

_log = structlog.get_logger(__name__)

# _SKILLS_PAGE_LIMIT is the absolute org-wide visibility window for skills.
# ``next_page`` is NEVER populated for skills at any page boundary (verified
# live, 2026-06-10); agents.list paginates correctly under identical
# conditions. A full page therefore means
# the org skill view is truncated, not simply "more pages to follow".
_SKILLS_PAGE_LIMIT = 100


async def find_agents_by_daimon_tag(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    include_archived: bool = False,
) -> list[BetaManagedAgentsAgent]:
    """Return all MA agents tagged `(tenant_id, name)`, canonical first.

    The first element (max `created_at`) is the canonical match the resolver
    and reconcile should adopt; remaining elements are duplicates that
    reconcile is responsible for archiving (per R5 dedup). Empty list = no
    match.

    `include_archived=True` lifts the archived filter — useful for `daimon
    agents get --include-archived` when an operator needs to inspect an
    archived row. Defaults to False so reconcile + the resolver never adopt
    archived agents.
    """
    matches: list[BetaManagedAgentsAgent] = []
    async for ag in client.beta.agents.list(include_archived=include_archived):
        if (
            ag.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)
            and ag.metadata.get(MA_METADATA_KEY_NAME) == name
        ):
            matches.append(ag)
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="agents", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def find_agent_by_daimon_tag(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    include_archived: bool = False,
) -> BetaManagedAgentsAgent | None:
    """Read-only adapter for the resolver: canonical match only, no side effects."""
    matches = await find_agents_by_daimon_tag(
        client, tenant_id=tenant_id, name=name, include_archived=include_archived
    )
    if len(matches) > 1:
        # Account-aware tripwire. The ambiguous state is degraded but not
        # an outage — adoption still proceeds with the newest match. The warning
        # makes the cross-account collision diagnosable without causing downtime.
        _log.warning(
            "ma_index.resolver_ambiguous_name",
            tenant_id=str(tenant_id),
            name=name,
            count=len(matches),
            adopted_id=matches[0].id,
            adopted_account=matches[0].metadata.get(MA_METADATA_KEY_ACCOUNT),
            duplicate_accounts=[m.metadata.get(MA_METADATA_KEY_ACCOUNT) for m in matches[1:]],
        )
    return matches[0] if matches else None


async def find_environments_by_daimon_tag(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str
) -> list[BetaEnvironment]:
    """Parity with `find_agents_by_daimon_tag` — canonical first, duplicates follow."""
    matches: list[BetaEnvironment] = []
    async for env in client.beta.environments.list(include_archived=False):
        if (
            env.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)
            and env.metadata.get(MA_METADATA_KEY_NAME) == name
        ):
            matches.append(env)
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="environments", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def find_environment_by_daimon_tag(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str
) -> BetaEnvironment | None:
    """Read-only adapter for the resolver: canonical match only, no side effects."""
    matches = await find_environments_by_daimon_tag(client, tenant_id=tenant_id, name=name)
    return matches[0] if matches else None


async def list_agents_by_tenant(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID
) -> list[BetaManagedAgentsAgent]:
    """Return all non-archived MA agents tagged with tenant_id."""
    results: list[BetaManagedAgentsAgent] = []
    async for ag in client.beta.agents.list(include_archived=False):
        if ag.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
            results.append(ag)
    return results


async def list_referenced_skill_ids(client: AsyncAnthropic) -> set[str]:
    """Return the skill ids pinned by any non-archived agent (org-wide).

    Skills are org-wide on MA and carry no metadata field, so the skill sweep
    cannot distinguish a defaults-created skill from a user-synced one the way
    the agent/env sweeps use the `daimon_managed` marker. Sparing any skill an
    agent still references is the available proxy for "in use, don't delete"
    and prevents the sweep from deleting a live agent's skill out from under it
    (smoke-matrix #19). Not tenant-scoped: skills are org-wide, so a reference
    from an agent in any tenant counts.
    """
    referenced: set[str] = set()
    async for ag in client.beta.agents.list(include_archived=False):
        for skill in ag.skills:
            referenced.add(skill.skill_id)
    return referenced


async def list_environments_by_tenant(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID
) -> list[BetaEnvironment]:
    """Return all non-archived MA environments tagged with tenant_id."""
    results: list[BetaEnvironment] = []
    async for env in client.beta.environments.list(include_archived=False):
        if env.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
            results.append(env)
    return results


async def _collect_skills_page(
    client: AsyncAnthropic,
) -> tuple[list[SkillListResponse], bool]:
    """Fetch the single skills page and return (rows, page_full).

    ``page_full`` is True when the number of returned rows equals
    _SKILLS_PAGE_LIMIT — because MA never populates ``next_page`` for skills,
    a full page means the org view is truncated.
    """
    rows: list[SkillListResponse] = []
    async for sk in client.beta.skills.list(limit=_SKILLS_PAGE_LIMIT):
        rows.append(sk)
    return rows, len(rows) >= _SKILLS_PAGE_LIMIT


async def list_skills_strict(client: AsyncAnthropic) -> list[SkillListResponse]:
    """Return all MA skills, raising SkillsListTruncatedError if the page is full.

    Use in write contexts (create, delete, dedup) where making decisions on a
    truncated view is unsafe.
    """
    rows, page_full = await _collect_skills_page(client)
    if page_full:
        raise SkillsListTruncatedError(
            f"skills.list returned a full page of {_SKILLS_PAGE_LIMIT} rows — "
            "MA never populates next_page for skills, so the org skill view is "
            "truncated; create/delete decisions on this view are unsafe"
        )
    return rows


async def list_skills_lenient(client: AsyncAnthropic) -> tuple[list[SkillListResponse], bool]:
    """Return (rows, truncated) for the MA skills page.

    When truncated: emits a structlog warning and a Sentry capture. Use in read
    contexts where degraded results are acceptable but should be observable.
    """
    rows, page_full = await _collect_skills_page(client)
    if page_full:
        _log.warning("ma_index.skills_list_truncated", limit=_SKILLS_PAGE_LIMIT)
        sentry_sdk.capture_message("skills list truncated at MA API page limit", level="warning")
    return rows, page_full


async def find_skills_by_display_title(
    client: AsyncAnthropic,
    display_title: str,
    *,
    on_truncation: Literal["raise", "degrade"],
) -> list[SkillListResponse]:
    """Return all custom MA skills with `display_title`, canonical first.

    Parity with `find_agents_by_daimon_tag` — the first element (max
    `created_at`) is the canonical match the resolver and reconcile should
    adopt; remaining elements are duplicates that reconcile is responsible for
    cleaning up. Empty list = no match.

    Filters out ``source="anthropic"`` so built-in skills can never collide with
    a user-defined display_title.

    ``on_truncation="raise"`` raises SkillsListTruncatedError when the page is
    full REGARDLESS of whether a match was found — a match on a truncated view
    can still hide duplicates (write mode). ``on_truncation="degrade"``
    logs and captures to Sentry but returns matches (read mode). Callers
    must choose explicitly: write/create/delete contexts use "raise"; read
    contexts use "degrade".
    """
    rows, page_full = await _collect_skills_page(client)
    if page_full:
        if on_truncation == "raise":
            raise SkillsListTruncatedError(
                f"skills.list returned a full page of {_SKILLS_PAGE_LIMIT} rows — "
                "MA never populates next_page for skills, so the org skill view is "
                "truncated; create/delete decisions on this view are unsafe"
            )
        _log.warning("ma_index.skills_list_ceiling_hit", limit=_SKILLS_PAGE_LIMIT)
        sentry_sdk.capture_message("skills list truncated at MA API page limit", level="warning")
    matches = [sk for sk in rows if sk.source == "custom" and sk.display_title == display_title]
    if len(matches) > 1:
        _log.warning("ma_index.multi_match", kind="skills", count=len(matches))
    matches.sort(key=lambda m: m.created_at, reverse=True)
    return matches


async def find_skill_by_display_title(
    client: AsyncAnthropic,
    display_title: str,
    *,
    on_truncation: Literal["raise", "degrade"],
) -> SkillListResponse | None:
    """Read-only adapter for callers that only need the canonical match."""
    matches = await find_skills_by_display_title(client, display_title, on_truncation=on_truncation)
    return matches[0] if matches else None
