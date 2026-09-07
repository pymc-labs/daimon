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

from daimon.core.specs import AgentSpec, SkillRef

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
