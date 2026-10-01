"""Derive a read-only reader variant of a publisher's agent.

SPEC D-04/§1.7: a published report is answered by a *variant* of whatever
agent the publisher names, not a fixed generic `report-reader` agent. The
variant keeps the source's prompt, model and skills (so it answers in the
voice the client already knows) and loses every MCP server and every
`mcp_toolset` tool (so a report reader can never reach the source's
integrations — see threat T-21-03-A).

Split in two, functional-core/imperative-shell:

- `derive_reader_spec` is the pure `AgentSpec -> AgentSpec` transform. No I/O,
  fully unit-testable, and the only place the isolation guarantees are
  decided.
- `ensure_reader_variant` is the thin shell: resolve the source agent on MA,
  derive its spec, then find-or-create-or-update the variant, keyed so that
  publishing twice from an unchanged source reuses one variant instead of
  creating a second (threat T-21-03-D).
"""

from __future__ import annotations

import uuid
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSkillParams
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, find_agents_by_daimon_tag
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_SPEC_HASH,
    build_metadata,
    compute_spec_fingerprint,
)
from daimon.core.defaults.skills import resolve_refs
from daimon.core.errors import DaimonError
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.specs import AgentSpec, SkillRef, dump_agent_spec

# Skill seeded from the defaults tree (its SKILL.md lands in a later plan)
# that teaches the model how to unpack and read the mounted report bundle.
READER_SKILL_NAME = "report-reader"

# Appended to the source agent's system prompt. Prose the model reads, not a
# spec citation — carries the five clauses SPEC §1.7 names for a reader
# variant's behaviour.
READER_BLOCK = """

## Answering questions about this report

You are now a reader for one published report, not the general-purpose \
agent you were before. Answer only from the bundle mounted in this session \
— never fall back on outside knowledge or on what you remember from other \
conversations. For every number you state, cite the file and the column it \
came from. If the bundle genuinely cannot answer a question, say so plainly \
instead of guessing or extrapolating a plausible-sounding figure — never \
invent data to fill a gap. Only rebuild or refit a model when the reader \
explicitly asks for it, and when you do, say up front that a refit takes a \
few minutes so they know to wait.
"""


def derive_reader_spec(source: AgentSpec) -> AgentSpec:
    """Pure transform: the source agent's spec, stripped to a report reader.

    - `name` gains a `-reader` suffix.
    - `isolated=True` and `mcp_servers=None` — a reader variant is created
      via `create_isolated_session` (no vault, no env mount), so it has
      nothing to authenticate an MCP server with.
    - `tools` drops every `mcp_toolset` entry, collapsing to `None` when
      nothing is left (an empty list and `None` are not equivalent to
      `dump_agent_spec`'s downstream merge).
    - `mcp_servers` and every `mcp_toolset` tool are dropped *together*:
      `AgentSpec._require_mcp_toolset_when_mcp_servers_set` rejects a spec
      that declares one without the other, so dropping only one half would
      raise at validation instead of producing a clean variant.
    - `skills` gains exactly one `report-reader` ref, added only when no
      existing ref already carries that `skill_id` — deriving from an
      already-derived spec must not duplicate it.
    - `system` gains `READER_BLOCK`, appended only when not already present,
      for the same idempotence reason.
    - `model`, `description`, `multiagent` and `skill_repos` carry through
      unchanged.

    `source` is never mutated — `model_copy` returns a new instance.
    """
    non_mcp_tools = [tool for tool in (source.tools or []) if tool.get("type") != "mcp_toolset"]
    skills = list(source.skills)
    if not any(ref.skill_id == READER_SKILL_NAME for ref in skills):
        skills.append(SkillRef(type="custom", skill_id=READER_SKILL_NAME))
    system = source.system or ""
    if READER_BLOCK not in system:
        system += READER_BLOCK
    return source.model_copy(
        update={
            "name": f"{source.name}-reader",
            "isolated": True,
            "mcp_servers": None,
            "tools": non_mcp_tools or None,
            "skills": skills,
            "system": system,
        }
    )


async def ensure_reader_variant(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    source_name: str,
) -> BetaManagedAgentsAgent:
    """Find, create or update the reader variant of `source_name`, on MA only.

    Deliberate deviation from SPEC §1.7's literal signature: the spec writes
    `ensure_reader_variant(anthropic, session_factory, *, ...)`, but nothing
    in this function's work touches the database — resolving the source,
    deriving the spec, resolving skill refs and creating or updating the
    agent are all Managed Agents calls. `guideline:architecture` forbids
    taking a collaborator you do not use, so `session_factory` is omitted.

    Reuse is keyed on `daimon_reader_of`, a fingerprint of the *source's*
    shape: `daimon_spec_hash` off the source's own metadata when it carries
    one (every defaults-reconciled agent does), else a fingerprint of the
    source's `(id, version)` pair. Both change exactly when the source
    changes — a reconciled agent's spec hash moves on edit, and MA bumps
    `version` on every update regardless of how the agent got created — so
    either branch is a stable "has the source changed" signal.

    Raises `DaimonError` when `source_name` does not resolve to an agent on
    MA — fails loudly rather than silently falling back to a different
    agent.
    """
    source_ma = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=source_name)
    if source_ma is None:
        raise DaimonError(f"agent {source_name!r} not found")

    reader_of = source_ma.metadata.get(MA_METADATA_KEY_SPEC_HASH) or compute_spec_fingerprint(
        {"agent_id": source_ma.id, "version": source_ma.version}
    )

    # The source's skills are already resolved MA skill params (real skill
    # ids, not authoring names) — reconstructed as SkillRef only so
    # `derive_reader_spec` can run its dedup check against them. They are
    # never re-resolved through `resolve_refs` (see below): a custom skill's
    # already-resolved id would not match any tenant display_title.
    source_skills = [
        SkillRef(type=skill.type, skill_id=skill.skill_id) for skill in source_ma.skills
    ]
    source_spec = AgentSpec(
        name=source_ma.name,
        model=source_ma.model.id,
        description=source_ma.description,
        system=source_ma.system,
        # A reader carries only the base agent toolset, which `dump_agent_spec`
        # injects (with always-allow) on every create/update. The source's
        # toolset is deliberately not round-tripped: the live agent response
        # carries per-tool fields (a `type` on each config entry) that the
        # create-params shape rejects, and a reader has no use for the
        # source's toolset overrides anyway.
        tools=None,
        # Likewise the source's MCP servers are not carried over: a reader has
        # none by construction, and a spec that names servers without their
        # paired toolset (which is exactly what the source looks like once its
        # tools are dropped) fails the spec's own validator.
        mcp_servers=None,
        skills=source_skills,
    )
    reader_spec = derive_reader_spec(source_spec)

    # Only the newly-derived `report-reader` ref is an authoring name that
    # needs resolving; every other entry in `reader_spec.skills` came
    # straight off `source_ma.skills` above and is already a resolved MA
    # skill param — pass it through unchanged instead of routing it through
    # `resolve_refs`'s display_title lookup (see `source_skills` comment).
    resolved_skills: list[BetaManagedAgentsSkillParams] = []
    new_refs: list[SkillRef] = []
    for ref in reader_spec.skills:
        if ref.skill_id == READER_SKILL_NAME:
            new_refs.append(ref)
        else:
            resolved_skills.append(
                cast("BetaManagedAgentsSkillParams", {"type": ref.type, "skill_id": ref.skill_id})
            )
    resolved_skills.extend(await resolve_refs(anthropic, refs=new_refs, tenant_id=tenant_id))

    reader_spec_dump = dump_agent_spec(reader_spec, mode="json")
    reader_spec_hash = compute_spec_fingerprint(
        {"spec": reader_spec_dump, "skills": resolved_skills}
    )
    # managed=False is load-bearing: a variant stamped managed would be
    # archived by the next `defaults apply` sweep because it is not in the
    # seeded spec list (threat T-21-03-C).
    metadata = build_metadata(
        tenant_id=tenant_id,
        name=reader_spec.name,
        account_id=account_id,
        managed=False,
        spec_hash=reader_spec_hash,
        isolated=True,
    )
    metadata[MA_METADATA_KEY_READER_OF] = reader_of

    matches = await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=reader_spec.name)
    match = matches[0] if matches else None

    if match is None:
        return await anthropic.beta.agents.create(
            **dump_agent_spec(reader_spec),
            skills=resolved_skills,
            metadata=metadata,
        )

    if (
        match.metadata.get(MA_METADATA_KEY_READER_OF) == reader_of
        and match.metadata.get(MA_METADATA_KEY_SPEC_HASH) == reader_spec_hash
    ):
        # Reuse across publishes: neither the source nor the derived shape
        # changed since the variant was last written. No MA write.
        return match

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await anthropic.beta.agents.update(
            fresh.id,
            version=fresh.version,
            **dump_agent_spec(reader_spec),
            skills=resolved_skills,
            metadata=cast("dict[str, str | None]", metadata),
        )

    return await update_agent_with_version_retry(anthropic, match.id, _apply)
