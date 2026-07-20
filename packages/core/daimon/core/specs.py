"""Authoring-time YAML schemas for `defaults/` and user-authored spec files.

Principle: thin passthrough. Field names mirror the `anthropic` SDK's
`*CreateParams` TypedDicts so that YAML authors write what the SDK sees.
No daimon-private aliases, no runtime translation layer inside these models —
the seed converter (separate module) is the one place that maps a `*Spec` to
the SDK's kwargs shape.

Entry points:
- `AgentSpec` — parsed from `defaults/agents/<name>.yaml` or user CRUD input.
- `EnvironmentSpec` — parsed from `defaults/environments/<name>.yaml`.
- `SkillSpec` — parsed from `SKILL.md`'s YAML frontmatter (caller splits the
  markdown body before passing the frontmatter dict here).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from anthropic.types.beta.beta_managed_agents_model_param import BetaManagedAgentsModelParam
from anthropic.types.beta.beta_managed_agents_multiagent_params import (
    BetaManagedAgentsMultiagentParams,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from anthropic.types.beta.beta_packages_params import BetaPackagesParams
from daimon.core.errors import SpecError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class SkillRef(BaseModel):
    """Authoring-time reference to a skill in an AgentSpec.

    `type` discriminates between custom (user-uploaded) and anthropic-built-in
    skills. `skill_id` is the name used to look up the skill at upload time
    (resolved to a MA skill ID by `resolve_refs`).

    `extra="forbid"` enforces the STRIDE T-07-01 threat mitigation: unknown
    fields from YAML are rejected at parse time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["custom", "anthropic"]
    skill_id: str


class SkillRepo(BaseModel):
    """Authoring-time reference to a GitHub skill repository.

    Used by the `sync_agent_skills` orchestrator. Excluded from
    `AgentSpec.model_dump()` because skill_repos do not serialize to MA —
    the sync pipeline turns them into uploaded `BetaManagedAgentsSkillParams`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    branch: str = "main"
    path: str = ""
    # split=True discovers each SKILL.md as its own skill and ships its full
    # subtree. Defaults True so callers that don't think about it (chat
    # create_agent/update_agent, webhook resync) get every skill in a
    # multi-skill repo. split=False bundles the whole repo as one skill and
    # excludes nested SKILL.md subtrees — opt into it only for a single-skill
    # repo you want merged whole.
    split: bool = True


class AgentSpec(BaseModel):
    """Authoring shape for an agent YAML file.

    Mirrors `anthropic.types.beta.agent_create_params.AgentCreateParams` with
    `extra="forbid"`. Uses the SDK's `Tool` discriminated union,
    `BetaManagedAgentsURLMCPServerParams`, and `BetaManagedAgentsModelParam`
    verbatim — any SDK rename or discriminator change is inherited without
    re-declaration.

    Two fields diverge from the SDK:

    - `metadata` is omitted entirely. The upload call synthesizes
      `{daimon_account, daimon_name}` at the SDK boundary; operators cannot
      write it (extra="forbid").
    - `skills: list[SkillRef]` is the identity-reference exception. Authoring
      refs are resolved to `BetaManagedAgentsSkillParams` entries at upload
      time (via `daimon.core.defaults.skills.resolve_refs`) because MA skill
      IDs don't exist until skills have uploaded. `Field(exclude=True)` keeps
      it out of `model_dump()`; the resolved list is passed as a separate
      kwarg at the call site.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    model: BetaManagedAgentsModelParam
    description: str | None = None
    system: str | None = None
    tools: list[Annotated[Tool, Field(discriminator="type")]] | None = None
    mcp_servers: list[BetaManagedAgentsURLMCPServerParams] | None = None
    multiagent: BetaManagedAgentsMultiagentParams | None = None
    skills: list[SkillRef] = Field(default_factory=list[SkillRef], exclude=True)
    skill_repos: list[SkillRepo] = Field(default_factory=list[SkillRepo], exclude=True)

    @model_validator(mode="after")
    def _require_mcp_toolset_when_mcp_servers_set(self) -> AgentSpec:
        """Reject authoring shapes that declare `mcp_servers` without a matching
        `mcp_toolset` tool — MA's create/update endpoint returns a 400 in this
        case, and the failure surfaces deep in the SDK instead of at parse time.

        The defaults `reconcile_agents` pipeline always injects both halves
        together (via `merge_default_mcp_server` + `merge_default_mcp_toolset`),
        so this only fires for hand-authored specs passed to `daimon agents
        create/update`.
        """
        if not self.mcp_servers:
            return self
        tools = self.tools or []
        has_mcp_toolset = any(t.get("type") == "mcp_toolset" for t in tools)
        if not has_mcp_toolset:
            raise ValueError(
                "agent declares `mcp_servers` but no matching `mcp_toolset` "
                "entry in `tools`. Add an entry like "
                "`{type: mcp_toolset, mcp_server_name: <name>}` so the model "
                "can actually call into the MCP server."
            )
        return self

    @model_validator(mode="after")
    def _materialize_tool_iterables(self) -> AgentSpec:
        """Force `configs` / other Iterable fields inside tool dicts to concrete lists.

        The SDK's Tool TypedDicts annotate sub-collections as `Iterable[...]`.
        Pydantic's TypedDict validator stores these as a lazy `ValidatorIterator`
        that `model_dump` consumes on first walk; subsequent dumps would then
        produce empty lists. We normalize after validation so every dump is
        idempotent.
        """
        if self.tools is None:
            return self
        for tool in self.tools:
            for key, val in list(tool.items()):
                if (
                    key != "type"
                    and not isinstance(val, (str, bytes, dict))
                    and hasattr(val, "__iter__")
                ):
                    tool[key] = list(val)  # type: ignore[literal-required]  # Dynamic key iteration over TypedDict is inherently unsound; refactoring to cast(dict[str, object], tool) or a dict comprehension trades one suppression for two casts with no safety gain.
        return self


class EnvironmentSpec(BaseModel):
    """Authoring shape for an environment YAML file.

    Mirrors `anthropic.types.beta.environment_create_params.EnvironmentCreateParams`
    with `extra="forbid"` so authoring typos fail before any MA write. The SDK's
    `BetaCloudConfigParams` TypedDict is the nested type for `config` — no
    re-declaration here. `metadata` is intentionally omitted: the upload call
    synthesizes `{daimon_account, daimon_name}` at the SDK boundary.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    config: BetaCloudConfigParams | None = None
    description: str | None = None
    scope: Literal["organization", "account"] | None = None

    @model_validator(mode="after")
    def _normalize_packages_to_replace_semantics(self) -> EnvironmentSpec:
        """Make the YAML authoritative for environment packages (replace semantics).

        MA's environment update is a per-field merge: an absent `packages` key
        preserves whatever packages already exist on the environment, so reverting
        a YAML back to "no packages" can never clear them. Normalizing an absent or
        partial `packages` to explicit empty arrays for every ecosystem means
        `model_dump(exclude_none=True)` always emits `packages`, so both create and
        update send it and MA replaces rather than merges. Author-provided lists are
        preserved; the unspecified ecosystems fill empty.

        Only applies when `config` is present — an environment that declares no
        config at all keeps `config=None` so nothing is forced onto it.
        """
        if self.config is None:
            return self
        existing: BetaPackagesParams = self.config.get("packages") or {}
        self.config["packages"] = {
            "type": "packages",
            "apt": list(existing.get("apt") or []),
            "cargo": list(existing.get("cargo") or []),
            "gem": list(existing.get("gem") or []),
            "go": list(existing.get("go") or []),
            "npm": list(existing.get("npm") or []),
            "pip": list(existing.get("pip") or []),
        }
        return self


class SkillSpec(BaseModel):
    """Authoring shape for `SKILL.md`'s YAML frontmatter.

    Per skills-design §5.2:
    - `name` and `description` are required.
    - Additional frontmatter keys are preserved (via `extra='allow'`) but
      daimon does not interpret them. Providers (e.g. `obra/superpowers`)
      can set their own keys without daimon rejecting them.

    Callers split `SKILL.md` into (frontmatter_dict, body_markdown) and pass
    the dict here; body handling lives in the skills packaging pipeline.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    description: str


class SystemConfigSpec(BaseModel):
    """Authoring shape for `defaults/config.yaml`.

    Parsed at startup by `parse_deployment_default` into a `DeploymentDefault`
    and injected into `resolve()` as the bottom tier of the config cascade.
    Both fields are optional — a deployment that wants `--agent` and
    `--environment` to be mandatory can omit the file entirely or leave
    both null.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str | None = None
    environment_name: str | None = None


_TOOLSET_TYPES = frozenset({"agent_toolset_20260401", "mcp_toolset"})

_BASE_AGENT_TOOL_NAMES = ("bash", "read", "edit", "grep", "glob", "write")


def merge_default_agent_toolset(existing: list[Tool] | None) -> list[Tool]:
    """Return `existing` with the base 6-tool agent_toolset appended iff missing.

    MA rejects session creation on any agent that has skills attached but no
    usable `read` tool on its agent_toolset ("skills require the read tool to
    be usable"). Every daimon agent therefore carries the base toolset; an
    authored `agent_toolset_20260401` entry is preserved verbatim (same object
    returned, no churn), everything else gains the default.

    Does not mutate `existing`.
    """
    current = list(existing) if existing is not None else []
    for tool in current:
        if tool.get("type") == "agent_toolset_20260401":
            return existing if existing is not None else current
    base_toolset: Tool = {
        "type": "agent_toolset_20260401",
        "configs": [{"name": name} for name in _BASE_AGENT_TOOL_NAMES],
    }
    current.append(base_toolset)
    return current


def dump_agent_spec(
    spec: AgentSpec,
    *,
    mode: Literal["python", "json"] = "python",
    exclude_none: bool = True,
) -> dict[str, Any]:
    """Dump an `AgentSpec` suppressing the cosmetic `configs`-is-not-a-generator warning.

    The SDK's `Tool` TypedDicts type sub-collections as `Iterable[...]`, so
    pydantic emits `PydanticSerializationUnexpectedValue` on every dump even
    though `_materialize_tool_iterables` has normalized the stored value to a
    list. The dumped dict is correct — only the stderr warning is noise.

    Injects `permission_policy={"type": "always_allow"}` into every toolset's
    `default_config` (both `agent_toolset_20260401` and `mcp_toolset`). This
    enforces always-allow at the single SDK boundary; operators cannot override
    to a weaker policy.

    Also ensures the base agent_toolset is present (`merge_default_agent_toolset`)
    so no spec-shaped create or update can produce an agent whose attached
    skills are unusable. Fork paths copy raw MA state and bypass this function;
    they apply the merge at their own call sites.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Pydantic serializer warnings",
            category=UserWarning,
        )
        dumped = spec.model_dump(mode=mode, exclude_none=exclude_none)

    tools = cast(
        "list[dict[str, Any]]",
        list(merge_default_agent_toolset(cast("list[Tool] | None", dumped.get("tools")))),
    )
    for tool in tools:
        if tool.get("type") in _TOOLSET_TYPES:
            tool.setdefault("default_config", {})["permission_policy"] = {"type": "always_allow"}
    dumped["tools"] = tools

    return dumped


def load_agent_spec(path: Path) -> AgentSpec:
    """Read, parse, and validate an `AgentSpec` YAML file.

    Raises `SpecError` (preserving the underlying cause via `__cause__`) when
    the file is missing, contains malformed YAML, or fails `AgentSpec`
    validation. Never returns on failure.
    """
    try:
        text = path.read_text()
    except OSError as err:
        raise SpecError(f"cannot read agent spec at {path}: {err.strerror}") from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise SpecError(f"agent spec at {path} is not valid YAML: {err}") from err
    try:
        return AgentSpec.model_validate(data)
    except ValidationError as err:
        raise SpecError(f"agent spec at {path} failed validation: {err}") from err


def load_environment_spec(path: Path) -> EnvironmentSpec:
    """Read, parse, and validate an `EnvironmentSpec` YAML file.

    Raises `SpecError` (preserving the underlying cause via `__cause__`) when
    the file is missing, contains malformed YAML, or fails `EnvironmentSpec`
    validation. Never returns on failure.
    """
    try:
        text = path.read_text()
    except OSError as err:
        raise SpecError(f"cannot read environment spec at {path}: {err.strerror}") from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise SpecError(f"environment spec at {path} is not valid YAML: {err}") from err
    try:
        return EnvironmentSpec.model_validate(data)
    except ValidationError as err:
        raise SpecError(f"environment spec at {path} failed validation: {err}") from err
