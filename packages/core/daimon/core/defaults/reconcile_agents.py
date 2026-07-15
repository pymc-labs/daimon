"""Per-resource reconciliation for agents.

Two-state decision tree: find on MA by daimon tag → update (found) or create
(not found). The spec hash and DB row are gone; MA is the source of truth for
whether the resource exists.
"""

from __future__ import annotations

import uuid
from typing import cast

import structlog
from anthropic import AsyncAnthropic
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.mcp_merge import (
    is_corrupted_daimon_mcp_entry,
    merge_default_mcp_server,
    merge_default_mcp_toolset,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_SPEC_HASH,
    build_metadata,
    compute_spec_fingerprint,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.skills import resolve_refs
from daimon.core.defaults.spec_merge import (
    merge_mcp_servers_with_ma,
    merge_skills_with_ma,
    merge_tools_with_ma,
)
from daimon.core.specs import AgentSpec, dump_agent_spec

_log = structlog.get_logger(__name__)


async def reconcile_agent(
    client: AsyncAnthropic,
    spec: AgentSpec,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
    account_id: uuid.UUID | None = None,
    public_url: str | None = None,
    managed: bool = True,
) -> ResourceOutcome:
    """Reconcile a single agent against MA.

    `managed=True` (default) stamps `daimon_managed=true` in the agent's
    metadata so the sweep (defaults apply) can find and archive resources
    that have dropped out of the seeded spec. This is correct for callers
    in the defaults pipeline.

    `managed=False` is for editing user-owned agents (e.g. Discord's panel
    `call_reconcile_for_panel` updating a user fork). Without this guard,
    every panel edit re-stamps the user fork as managed → next `defaults
    apply` sweeps it because it isn't in the seeded spec list.
    """
    # Prepend the credential-guidance block so every agent knows where its
    # secrets live (mounted env file) vs MCP vault-bound auth, instead of
    # hallucinating "no key" / hunting for non-existent MCP keys. Applied
    # before the spec-hash computation so the block participates in the hash:
    # existing agents update exactly once to gain it, then stabilise (the
    # block is idempotent under re-application).
    spec = spec.model_copy(update={"system": apply_credential_guidance(spec.system or "")})

    merged_mcp = merge_default_mcp_server(spec.mcp_servers, public_url)
    merged_tools = merge_default_mcp_toolset(spec.tools, public_url)
    update: dict[str, object] = {}
    if merged_mcp is not spec.mcp_servers:
        update["mcp_servers"] = merged_mcp
    if merged_tools is not spec.tools:
        update["tools"] = merged_tools
    if update:
        spec = spec.model_copy(update=update)

    resolved_skills = await resolve_refs(client, refs=spec.skills, tenant_id=tenant_id)
    matches = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name=spec.name)
    ma_match = matches[0] if matches else None
    # Dedup: when MA holds multiple agents with the same daimon_name (the
    # documented R5 multi_match condition), keep the canonical one (max
    # created_at, returned first by find_agents_by_daimon_tag) and archive
    # the rest. Without this the duplicates accumulate forever.
    duplicates = matches[1:] if len(matches) > 1 else []
    if duplicates and not dry_run:
        canonical_account = ma_match.metadata.get(MA_METADATA_KEY_ACCOUNT) if ma_match else None
        for dup in duplicates:
            dup_account = dup.metadata.get(MA_METADATA_KEY_ACCOUNT)
            if dup_account != canonical_account:
                # Defense in depth (D-72-01): never archive an agent whose
                # daimon_account differs from the canonical's — that would be
                # cross-owner data loss. Warn and skip; a human must investigate.
                _log.warning(
                    "reconcile.skip_cross_account_duplicate",
                    kind="agent",
                    name=spec.name,
                    canonical_id=ma_match.id if ma_match else None,
                    duplicate_id=dup.id,
                    canonical_account=canonical_account,
                    duplicate_account=dup_account,
                )
                continue
            _log.info(
                "reconcile.archive_duplicate",
                kind="agent",
                name=spec.name,
                canonical_id=ma_match.id if ma_match else None,
                duplicate_id=dup.id,
            )
            await client.beta.agents.archive(dup.id)
    spec_dump = dump_agent_spec(spec, mode="json")
    spec_hash = compute_spec_fingerprint(
        {
            "spec": spec_dump,
            "skills": resolved_skills,
            "account_id": str(account_id) if account_id else None,
        }
    )
    metadata = build_metadata(
        tenant_id=tenant_id,
        name=spec.name,
        account_id=account_id,
        managed=managed,
        spec_hash=spec_hash,
    )

    if ma_match is not None:
        existing_hash = ma_match.metadata.get(MA_METADATA_KEY_SPEC_HASH)
        # D-06: check if MA carries a corrupted daimon-mcp entry (name==daimon-mcp,
        # foreign URL). A chat-clobber (pre-guard) never updated daimon_spec_hash, so
        # the hash short-circuit below would skip the repair indefinitely. Bypass it
        # when corruption is present so merge_mcp_servers_with_ma (spec wins on name)
        # can land the canonical entry. After repair, MA stores the healed hash and
        # subsequent reconciles take SKIPPED normally — no perpetual churn.
        ma_has_corruption = public_url is not None and any(
            is_corrupted_daimon_mcp_entry(name=entry.name, url=entry.url, public_url=public_url)
            for entry in ma_match.mcp_servers
        )
        if existing_hash == spec_hash and not ma_has_corruption:
            # L13 idempotency: nothing changed since last reconcile. Skip the
            # MA update so agent.version stops climbing on every scheduler boot.
            return ResourceOutcome(
                kind="agent", name=spec.name, action=Action.SKIPPED, anthropic_id=ma_match.id
            )
        if dry_run:
            return ResourceOutcome(kind="agent", name=spec.name, action=Action.UPDATED)
        # Merge spec entries with MA's current state so user-attached MCPs and
        # skills (e.g. a context7 server pinned to the daimon agent via SDK)
        # survive `defaults apply`. Spec wins on name/skill_id collision.
        merged_servers = merge_mcp_servers_with_ma(spec.mcp_servers, ma_match)
        spec_mcp_names = {s.get("name") for s in (spec.mcp_servers or [])}
        preserved_mcp_names = {
            entry.name for entry in ma_match.mcp_servers if entry.name not in spec_mcp_names
        }
        merged_tools = merge_tools_with_ma(
            spec.tools, ma_match, preserved_mcp_names=preserved_mcp_names
        )
        if merged_servers is not spec.mcp_servers or merged_tools is not spec.tools:
            spec = spec.model_copy(update={"mcp_servers": merged_servers, "tools": merged_tools})
        merged_skills = merge_skills_with_ma(resolved_skills, ma_match)
        updated = await client.beta.agents.update(
            ma_match.id,
            version=ma_match.version,
            **dump_agent_spec(spec),
            skills=merged_skills,
            metadata=cast("dict[str, str | None]", metadata),
        )
        return ResourceOutcome(
            kind="agent", name=spec.name, action=Action.UPDATED, anthropic_id=updated.id
        )

    if dry_run:
        return ResourceOutcome(kind="agent", name=spec.name, action=Action.CREATED)
    created = await client.beta.agents.create(
        **dump_agent_spec(spec),
        skills=resolved_skills,
        metadata=metadata,
    )
    return ResourceOutcome(
        kind="agent", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )
