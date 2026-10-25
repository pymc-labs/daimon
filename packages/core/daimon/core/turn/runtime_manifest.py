"""Credential-reference validation without credential discovery or SDK imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, Self, cast, get_type_hints
from uuid import UUID

import structlog
from daimon.core.errors import DaimonError
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.admission import Admission
from daimon.core.turn.ceiling import remaining_s, turn_deadline
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ThreadMode
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, ResourceRef, SkillRef
from mux.contracts.profile import Capability
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ResourceBinding, ToolSpec
from mux.errors import InvalidConfig, MuxError, UnsupportedCapability
from mux.profiles import backend_selection, get_profile, native_agent_tools
from mux.state.operations import request_digest
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

if TYPE_CHECKING:
    from daimon.core.neutral_resources import NeutralResourceHost
    from daimon.core.stores.domain import (
        BackendAgent,
        SourceRepresentation,
        _BackendDropConsent,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.stores.neutral_catalog import AgentRevision
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.contracts.ports import ManagedAgents

_RESERVED = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")
_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576
_WILDCARD = "*"
#: Built-in web tools an agent template never carries off Anthropic: they are named
#: losses (OpenAI's web_search is unpriced and uncounted; Gemini's grounding is off).
_WEB_TOOLS = frozenset({"web_search", "web_fetch"})
"""Reserved sentinel for 'every value' on `platform`, `channel_id` and
`account_id` only. Never accepted for `tenant_id` or `profile`: a manifest
row always authorizes resources for exactly one tenant, so the one hard
scope boundary (`tenant_id`) and the chosen native profile stay exact-match
always. This lets one row authorize "this tenant's OpenAI resources for
every channel/account", the deployment-wide default self-host operators
need, without ever letting a row serve a different tenant."""
_WILDCARDABLE_FIELDS = ("platform", "channel_id", "account_id")
_ADMISSION_FACTS = {
    name: TypeAdapter(annotation)
    for name, annotation in get_type_hints(Admission).items()
    if name
    not in ("agent", "environment", "observation", "backend_counterpart", "backend_agent_revision")
}

#: Admission facts a root retained before they existed read as their default.
_LEGACY_ADMISSION_FACTS = frozenset({"daimon_rule_tools", "daimon_rules_need_driver"})

log = structlog.get_logger(__name__)


class ProviderRuntimeConfigurationError(RuntimeError):
    """Static, typed operator errors; never include manifest content or paths."""

    def __init__(
        self,
        reason: Literal["reserved_key_reference", "invalid_manifest", "wildcard_tenant_or_profile"],
    ) -> None:
        self.reason = reason
        if reason == "reserved_key_reference":
            message = (
                "Provider runtime api_key_env uses a reserved provider prefix "
                "(OPENAI_, ANTHROPIC_, GOOGLE_, GEMINI_). Use a host-scoped name such as "
                "DAIMON_CHANNEL_OPENAI_KEY or DAIMON_CHANNEL_GEMINI_KEY."
            )
        elif reason == "wildcard_tenant_or_profile":
            message = (
                'Provider runtime manifest rows may use "*" on platform, channel_id or '
                'account_id only. "*" is never valid for tenant_id or profile.'
            )
        else:
            message = (
                "Provider runtime manifest must be a readable JSON list of objects, at most 1 MiB."
            )
        super().__init__(message)


def validate_api_key_env(value: str) -> str:
    """Refuse SDK ambient-variable namespaces before looking up any secret."""
    if value.upper().startswith(_RESERVED):
        raise ProviderRuntimeConfigurationError("reserved_key_reference")
    return value


ApiKeyEnvironment = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    AfterValidator(validate_api_key_env),
]


def validate_configured_runtime_manifest() -> None:
    """Check an explicitly configured manifest's references, without constructing plans.

    Settings loaders call this only with mux and channel backends enabled. Missing
    manifests retain the existing selected-turn refusal; no discovery happens here.
    Scope/digest, full provider policy and key availability remain admission checks.
    """
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not path:
        return
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ProviderRuntimeConfigurationError("invalid_manifest")
        rows = _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise ProviderRuntimeConfigurationError("invalid_manifest") from None
    for row in rows:
        reference = row.get("api_key_env")
        if isinstance(reference, str):
            validate_api_key_env(reference)
        if row.get("tenant_id") == _WILDCARD or row.get("profile") == _WILDCARD:
            raise ProviderRuntimeConfigurationError("wildcard_tenant_or_profile")


class ConfigurationGrant(BaseModel):
    """Finite operator-authorized resolved fields; no wildcard defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profiles: tuple[str, ...] = Field(min_length=1)
    models: tuple[str | None, ...] = Field(min_length=1)
    thread_modes: tuple[ThreadMode, ...] = Field(min_length=1)
    requires: dict[Capability, CapabilityRequirement]


class SourceSkillMapping(BaseModel):
    """A concrete source version and an independently deployed full-byte target."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SkillRef
    target: SkillRef
    archive: Path

    @model_validator(mode="after")
    def _concrete(self) -> Self:
        if (
            not self.archive.is_absolute()
            or self.source.version in (None, "latest")
            or self.target.version in (None, "latest")
            or self.target.digest is None
        ):
            raise ValueError("skill representation requires a concrete version and full archive")
        return self


class SourceCapabilities(BaseModel):
    """Operator-selected capability inputs; source capture must verify every byte."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_pin: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: ModelRef
    skills: tuple[SourceSkillMapping, ...]
    mcp_tools: tuple[str, ...] = Field(min_length=1)
    environment: EnvironmentSpec | None = None

    @model_validator(mode="after")
    def _unique(self) -> Self:
        profile = get_profile(backend_selection(self.model.provider).profile)
        if (
            len({item.source.id for item in self.skills}) != len(self.skills)
            or len({item.target.id for item in self.skills}) != len(self.skills)
            or len(set(self.mcp_tools)) != len(self.mcp_tools)
            or self.model.options
            or (
                profile.skill_digest == "version"
                and any(item.target.digest != item.target.version for item in self.skills)
            )
        ):
            raise ValueError("source capability representation must be complete and unambiguous")
        return self


@dataclass(frozen=True)
class _TemplateInputs:
    """Capture inputs a template row implies: only Daimon MCP, from the live catalogue."""

    source_pin: str
    model: ModelRef
    mcp_tools: tuple[str, ...] = ()
    skills: tuple[SourceSkillMapping, ...] = ()
    environment: EnvironmentSpec | None = None


def _grant_models(row: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The models a template row's configuration grant names; none for a malformed grant."""
    grant = row.get("configuration_grant")
    models = grant.get("models") if isinstance(grant, dict) else None
    return tuple(m for m in models if isinstance(m, str)) if isinstance(models, list) else ()


def configuration_matches(
    revision: ConfigRevision, *, digest: str | None, grant: ConfigurationGrant | None
) -> bool:
    """Legacy exact grants remain exact; bounded grants authorize only declared fields."""
    if grant is None:
        return digest == revision.digest
    return (
        revision.profile in grant.profiles
        and revision.model in grant.models
        and revision.thread_mode in grant.thread_modes
        and dict(revision.requires) == grant.requires
    )


def manifest_configuration_matches(row: Mapping[str, JsonValue], revision: ConfigRevision) -> bool:
    """Match before any credential lookup; invalid grants refuse at the loader boundary."""
    raw = row.get("configuration_grant")
    grant = ConfigurationGrant.model_validate(raw) if raw is not None else None
    digest = row.get("config_digest")
    return configuration_matches(
        revision, digest=digest if isinstance(digest, str) else None, grant=grant
    )


def fixed_plan_origin_allowed(origin: str | None) -> bool:
    """Only channel-origin plans bypass the complete authorized-source proof."""
    return origin == "channel"


async def capture_preparation_source(
    request: ProviderPreparationRequest, *, fresh: bool = False, capability_preview: bool = False
) -> BackendAgent:
    """Capture source facts; the existing proof and materializer validate them."""
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
    from daimon.core.sessions import project_session_resources
    from mux.errors import ScopeViolation

    if request.admission.hold is not None:
        # The source records the agent-wide memory store; a held turn mounts its
        # channel's store only, so it never has a source to capture.
        raise ScopeViolation("source-preparation", "a held turn cannot capture an unheld source")
    if fresh:
        from daimon.core.mux_compat import retrieve_agent, retrieve_environment

        actual = await retrieve_agent(
            request.deps.anthropic, request.admission.agent.id, scope=request.scope
        )
        environment = await retrieve_environment(
            request.deps.anthropic, request.admission.environment.id, scope=request.scope
        )
        request = replace(
            request,
            source=None,
            admission=replace(request.admission, agent=actual, environment=environment),
        )
    source = request.source
    if source is None:
        source = await load_backend_agent(
            request.deps.anthropic,
            request.deps.sessionmaker,
            tenant_id=request.tenant_id,
            agent_name=(request.admission.agent.metadata or {}).get(
                MA_METADATA_KEY_NAME, request.admission.config.agent_name
            )
            or "",
            actual_agent=request.admission.agent,
        )
    captured = await project_session_resources(
        source,
        agent=request.admission.agent,
        environment=request.admission.environment,
        session_factory=request.deps.sessionmaker,
        tenant_id=request.tenant_id,
        agent_uuid=source.agent_id,
        account_id=(
            None if request.admission.shared_owner is not None else request.admission.account_id
        ),
        resource_scope=request.scope,
        memory_read_only=request.admission.memory_read_only,
        fernet=request.deps.fernet,
        channel_skills=request.admission.channel_skills,
        tool_safety=request.deps.tool_safety,
        asks_before_publishing=request.admission.asks_before_publishing,
        mcp_settings=request.deps.mcp,
        agent_github_app=request.deps.agent_github_app,
        hide_personal_for=request.admission.shared_owner,
        slack_turn_context_id=request.admission.slack_turn_context_id,
        hold=None if request.admission.shared_owner is not None else request.admission.hold,
        private_dm_id=request.admission.private_dm_id,
        origin_seal_ids=request.admission.origin_seal_ids,
    )
    return await _capture_source_capabilities(
        request, captured, capability_preview=capability_preview
    )


#: A connector name a declared `mcp_server:<name>` loss can spell (mux contracts).
_CONNECTOR_NAME = re.compile(r"[A-Za-z0-9_.-]+")


async def _capture_source_capabilities(
    request: ProviderPreparationRequest, source: BackendAgent, *, capability_preview: bool = False
) -> BackendAgent:
    """Verify configured representations at the common preview/preparation capture edge.

    No configured representation means no archive, catalogue or provider reads.
    Native target ownership/readback remains a separate mandatory preparation gate.
    """
    from anthropic import AnthropicError
    from daimon.core.defaults.ma_index import download_skill_version
    from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti, validate_agent_mcp_token
    from daimon.core.mcp_oauth.discovery import list_mcp_tools
    from daimon.core.mux_backend import managed_agents
    from daimon.core.mux_compat import legacy_call
    from daimon.core.skills.ingest import retained_archive_files
    from daimon.core.stores.domain import SourceRepresentation
    from daimon.core.stores.mcp_tokens import revoke_mcp_token
    from daimon.core.turn.runtime_defaults import capture_template_rows
    from mux.contracts.resources import MCPConnection, SkillUpload, SkillUploadFile
    from mux.drivers.anthropic import (
        AGENT_TOOLS_NAMESPACE,
        AgentToolsConfig,
        MCPToolset,
        agent_tools,
        mcp_permissions,
    )
    from mux.profiles import represented_execution_tools
    from sqlalchemy.exc import SQLAlchemyError

    source = source.model_copy(update={"representations": ()})
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    rows: list[dict[str, JsonValue]] = []
    if path is not None:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError("manifest too large")
            rows = _ROWS.validate_json(raw)
        except (OSError, ValueError):
            # This optional capability evidence cannot break an ordinary source
            # capture. A full deployment still refuses without the representation,
            # and its closed runtime-manifest loader independently rejects bad input.
            return source
    # A code-default agent template counts only where the operator configured no
    # row for this tenant's profile at all, matching selection's last resort.
    tenant = str(source.tenant_id)
    operator_profiles = {str(row.get("profile")) for row in rows if row.get("tenant_id") == tenant}
    rows = [
        *rows,
        *(
            row
            for row in capture_template_rows(tenant)
            if str(row.get("profile")) not in operator_profiles
        ),
    ]
    revision = request.admission.backend_revision
    if (
        (request.admission.private_dm_id is not None)
        or request.admission.slack_turn_context_id is not None
        or request.admission.origin_channel_id is None
        or not capability_preview
        and (revision is None or request.admission.backend_revision_origin != "agent")
    ):
        return source
    try:
        selected = [
            row
            for row in rows
            if row.get("source_capabilities") is not None
            and row.get("platform") == request.platform
            and row.get("channel_id") == request.admission.origin_channel_id
            and (
                capability_preview
                or revision is not None
                and row.get("channel_id") == revision.channel.channel_id
                and row.get("profile") == revision.profile
            )
            and row.get("tenant_id") == str(source.tenant_id)
            and row.get("account_id") == request.scope.account_id
            and row.get("source_agent_id") == str(source.agent_id)
            and source.source is not None
            and row.get("source_agent_version") == source.source.revision.local
        ]
        templates = [
            (row, model)
            for row in rows
            if row.get("root_materialization") is True
            and row.get("template_capabilities") == "mcp"
            and row.get("tenant_id") == str(source.tenant_id)
            and row.get("platform") in (request.platform, _WILDCARD)
            and row.get("channel_id") in (request.admission.origin_channel_id, _WILDCARD)
            and row.get("account_id") in (request.scope.account_id, _WILDCARD)
            and (
                capability_preview
                or revision is not None
                and row.get("profile") == revision.profile
            )
            for model in _grant_models(row)
            if capability_preview or revision is not None and model == revision.model
        ]
        if not selected and not templates:
            return source
        spec, scope = source.effective_spec, source.resource_scope
        if spec is None or scope is None or scope != request.scope:
            raise ValueError("missing captured source")
        pin = source_preservation_pin(source)
        represented: list[SourceRepresentation] = []
        candidates: list[tuple[dict[str, JsonValue], SourceCapabilities | _TemplateInputs]] = []
        for row in selected:
            candidates.append((row, SourceCapabilities.model_validate(row["source_capabilities"])))
        for row, model in templates:
            candidates.append(
                (
                    row,
                    _TemplateInputs(
                        source_pin=pin,
                        model=ModelRef(
                            provider=get_profile(str(row.get("profile"))).provider, id=model
                        ),
                    ),
                )
            )
        for row, inputs in candidates:
            template = isinstance(inputs, _TemplateInputs)
            profile_id = row.get("profile")
            if not isinstance(profile_id, str):
                raise ValueError("missing representation profile")
            profile = get_profile(profile_id)
            if (
                inputs.source_pin != pin
                or profile.provider != inputs.model.provider
                or profile.skill_digest not in ("version", "content")
                or not template
                and row.get("platform") != request.platform
                or not template
                and row.get("channel_id") != request.admission.origin_channel_id
                and row.get("channel_id")
                != (
                    request.admission.backend_revision.channel.channel_id
                    if request.admission.backend_revision
                    else None
                )
            ):
                raise ValueError("representation differs from captured source")
            if inputs.environment is not None and (
                not profile.implicit_execution
                or source.environment is None
                or inputs.environment != source.environment.spec
                or inputs.environment.native_config is not None
                or inputs.environment.execution != "hosted"
                or inputs.environment.network is None
                or inputs.environment.packages is None
            ):
                raise ValueError("environment representation differs from captured policy")
            if request.deps.mcp.public_url is None or request.deps.mcp.jwt_secret is None:
                raise ValueError("MCP catalogue requires the captured caller")
            # Use the runtime's registered chat-execution authority, never the
            # restricted external-agent key (which sees a different catalogue).
            # This preview credential is revoked before returning and never
            # exposed to a provider; the ACK owner mints the runtime credential.
            now = request.now()
            secret = request.deps.mcp.jwt_secret.get_secret_value().encode()
            async with request.deps.sessionmaker.begin() as db:
                catalogue_token = await mint_agent_mcp_token(
                    db,
                    account_id=request.admission.account_id,
                    tenant_id=source.tenant_id,
                    agent_id=source.agent_id,
                    label="source-catalogue",
                    secret=secret,
                    now=now,
                    ttl_days=900 / 86400,  # pyright: ignore[reportArgumentType]
                    chat_execution=True,
                    surface="provider_turn",
                )
            try:
                await validate_agent_mcp_token(
                    request.deps.sessionmaker,
                    token=catalogue_token,
                    secret=secret,
                    scope=scope,
                    agent_id=source.agent_id,
                    now=now,
                    chat_execution=True,
                )
                catalogue = await list_mcp_tools(str(request.deps.mcp.public_url), catalogue_token)
            finally:
                async with request.deps.sessionmaker.begin() as db:
                    await revoke_mcp_token(db, jti=token_jti(catalogue_token), now=request.now())
            allowed = next(
                (
                    server.tool_policy.get("allowed_tools")
                    for server in spec.mcp_servers or ()
                    if server.name == DAIMON_SERVER_NAME
                ),
                None,
            )
            if allowed is not None:
                if not isinstance(allowed, list) or any(
                    not isinstance(name, str) for name in allowed
                ):
                    raise ValueError("invalid source MCP allowlist")
                if frozenset(allowed) != catalogue:
                    raise ValueError("source MCP allowlist differs from complete server catalogue")
                catalogue = frozenset(cast(list[str], allowed))
            if template:
                # A template carries no operator list: the live catalogue is the source's.
                inputs = _TemplateInputs(
                    source_pin=inputs.source_pin,
                    model=inputs.model,
                    mcp_tools=tuple(sorted(catalogue)),
                )
            if frozenset(inputs.mcp_tools) != catalogue:
                raise ValueError("MCP representation must preserve the exact source catalogue")
            # A user's own connectors can't come along: each is a named loss, and
            # the Daimon MCP server is represented without them.
            connectors = frozenset(
                server.name
                for server in spec.mcp_servers or ()
                if server.name != DAIMON_SERVER_NAME
            )
            if any(_CONNECTOR_NAME.fullmatch(name) is None for name in connectors):
                # No named loss can spell it: represent nothing, so MCP drops whole.
                raise ValueError("unrepresentable connector name")
            connector_losses = tuple(f"mcp_server:{name}" for name in sorted(connectors))
            tools = tuple(
                tool
                for tool in spec.tools or ()
                if not (tool.kind == "mcp_toolset" and tool.name in connectors)
            )
            permissions = {name: "auto" for name in catalogue}
            for extension in spec.extensions or ():
                if extension.namespace == AGENT_TOOLS_NAMESPACE:
                    native = agent_tools(extension)
                    mcps = [
                        tool
                        for tool in native.tools
                        if isinstance(tool, MCPToolset) and tool.mcp_server_name not in connectors
                    ]
                    if len(mcps) != 1 or mcps[0].mcp_server_name != DAIMON_SERVER_NAME:
                        raise ValueError("unrepresented MCP toolset")
                    permissions = mcp_permissions(mcps[0], catalogue)
                    tools = (
                        *AgentToolsConfig(
                            tools=[
                                tool for tool in native.tools if not isinstance(tool, MCPToolset)
                            ]
                        ).intent(exact=True),
                        ToolSpec(name=DAIMON_SERVER_NAME, kind="mcp_toolset"),
                    )
            if not any(
                tool.kind == "mcp_toolset"
                and tool.name == DAIMON_SERVER_NAME
                and tool.permission == "auto"
                for tool in tools
            ):
                raise ValueError("source has no enabled daimon-mcp toolset")
            # No OpenAI/Gemini turn has a confirmation card, pinned or template:
            # every confirmation-gated or publishing tool stays unavailable (and
            # the server refuses it for their provider-turn credentials too),
            # except those whose card Daimon posts itself.
            from daimon.core.tool_safety import confirmation_gated_tools, self_carded_tools

            gated, carded = confirmation_gated_tools(), self_carded_tools()
            permissions = {
                # A self-carded tool runs (Daimon posts its card) unless the agent
                # turned it off, which stays a named loss as before.
                name: (
                    "auto"
                    if name in carded and permission != "disabled"
                    else "ask"
                    if name in gated
                    else permission
                )
                for name, permission in permissions.items()
            }
            if not any(permission == "auto" for permission in permissions.values()):
                raise ValueError("source has no automatically executable MCP tools")
            # URL fetching has no native hosted-Agents tool. Keep the exact
            # source capability visible as a consented loss instead of silently
            # treating shell HTTP execution as the provider's fetch tool.
            unavailable_builtin_tools = (
                ("builtin_tool:web_fetch",)
                if profile.provider == "openai"
                and any(tool.kind == "builtin" and tool.name == "web_fetch" for tool in tools)
                else ()
            )
            own_tools = tuple(tool for tool in tools if tool.kind != "mcp_toolset")
            template_losses: tuple[str, ...] = ()
            if template:
                # A template maps the source's execution tools to the provider's own
                # (OpenAI bash in its hosted environment, Gemini code_execution): both
                # are billed as tokens and container time already measured, never per
                # call. Web tools stay named losses: OpenAI's web_search is unpriced
                # and uncounted, and Gemini's grounding is off. Custom skills are a
                # whole loss its consent must name.
                web = tuple(
                    tool for tool in own_tools if tool.kind == "builtin" and tool.name in _WEB_TOOLS
                )
                unavailable_builtin_tools = tuple(
                    f"builtin_tool:{tool.name}" for tool in sorted(web, key=lambda t: t.name)
                )
                try:
                    mapped_tools = (
                        represented_execution_tools(
                            profile, tuple(tool for tool in own_tools if tool not in web)
                        )
                        if own_tools
                        else ()
                    )
                except InvalidConfig:
                    # Tools it can't map (custom tools, a partial toolset) go whole.
                    mapped_tools, unavailable_builtin_tools = (), ()
                    template_losses = ("tools",)
                if any(s.source in (None, "custom") for s in spec.skills or ()):
                    template_losses = ("skills", *template_losses)
            else:
                mapped_tools = represented_execution_tools(
                    profile,
                    tuple(
                        tool
                        for tool in own_tools
                        if not (
                            unavailable_builtin_tools
                            and tool.kind == "builtin"
                            and tool.name == "web_fetch"
                        )
                    ),
                )
            if any(
                tool.kind == "mcp_toolset"
                and (tool.name != DAIMON_SERVER_NAME or tool.permission != "auto")
                for tool in tools
            ):
                raise ValueError("unrepresented MCP toolset")
            reference = f"caller-mcp:{source.tenant_id}:{scope.account_id}:{source.agent_id}"
            servers = tuple(s for s in spec.mcp_servers or () if s.name not in connectors)
            if not servers:
                # The composer supplies this caller-bound capability even when
                # it is absent from the persistent source agent definition.
                servers = (
                    MCPConnection(name=DAIMON_SERVER_NAME, url=str(request.deps.mcp.public_url)),
                )
            if (
                len(servers) != 1
                or servers[0].name != DAIMON_SERVER_NAME
                or servers[0].transport != "streamable_http"
                or servers[0].url.rstrip("/") != str(request.deps.mcp.public_url).rstrip("/")
                or not any(
                    binding.credential_ref == reference
                    and _mcp_credential_servers(source, binding) == frozenset({DAIMON_SERVER_NAME})
                    for binding in source.resource_requirements
                )
                or servers[0].credential_ref not in (None, reference)
            ):
                raise ValueError("MCP representation requires its original caller authority")
            if set(servers[0].tool_policy) - {"allowed_tools"}:
                raise ValueError("unrepresented MCP tool policy")
            allowed = servers[0].tool_policy.get("allowed_tools")
            if allowed is not None and (
                not isinstance(allowed, list)
                or len(allowed) != len(inputs.mcp_tools)
                or set(inputs.mcp_tools) != set(allowed)
            ):
                raise ValueError("MCP representation expands the source allowlist")
            # Hosted Agents MCP currently has no approval request/response
            # protocol (unlike Responses). Keep confirmation-gated tools out
            # of its executable allowlist and require explicit per-tool loss
            # consent. Never convert `ask` into `auto` or drop sibling tools.
            unavailable_mcp_tools = tuple(
                f"mcp_tool:{DAIMON_SERVER_NAME}:{name}"
                for name, permission in sorted(permissions.items())
                if permission == "ask"
            )
            connection = servers[0].model_copy(
                update={
                    "credential_ref": reference,
                    "tool_policy": {
                        "allowed_tools": [
                            name for name in inputs.mcp_tools if permissions[name] == "auto"
                        ]
                    },
                }
            )
            # The provider's built-in skills (docx, pptx, ...) have no bytes to
            # carry and run only there: another backend goes without them.
            portable = tuple(
                s for s in spec.skills or () if s.source in (None, "custom") and not template
            )
            if {item.source.id for item in inputs.skills} != {s.id for s in portable}:
                raise ValueError("skill representation is incomplete")
            unresolved = tuple(s for s in portable if s.version in (None, "latest"))
            latest: dict[str, str] = {}
            if unresolved:
                # Grant reads only for ids captured from this actual source.
                # A scoped catalogue walk deliberately hides custom skills.
                backend = managed_agents(
                    request.deps.anthropic,
                    scope=scope,
                    resources=frozenset(("skill", s.id) for s in unresolved),
                )

                async def resolve_latest(
                    attached: SkillRef, selected: ManagedAgents = backend
                ) -> tuple[str, str]:
                    record = await legacy_call(selected.skills.retrieve(scope, attached.id))
                    pin = record.latest_version
                    if (
                        record.id != attached.id
                        or record.source != attached.source
                        or pin is None
                        or pin.id != attached.id
                        or pin.version in (None, "latest")
                    ):
                        raise ValueError("source skill has no concrete current version")
                    assert pin.version is not None
                    return attached.id, pin.version

                latest = dict(await asyncio.gather(*(resolve_latest(s) for s in unresolved)))

            async def verify_skill(
                item: SourceSkillMapping, versions: Mapping[str, str] = latest
            ) -> None:
                attached = next(s for s in spec.skills or () if s.id == item.source.id)
                version = (
                    versions.get(attached.id)
                    if attached.version in (None, "latest")
                    else attached.version
                )
                if (
                    not isinstance(version, str)
                    or version != item.source.version
                    or item.source.source != attached.source
                ):
                    raise ValueError("source skill version changed")
                original_files = retained_archive_files(
                    await download_skill_version(
                        request.deps.anthropic,
                        skill_id=item.source.id,
                        version=version,
                        scope=scope,
                    )
                )
                retained = retained_archive_files(item.archive.read_bytes())
                if original_files != retained:
                    raise ValueError("represented skill bytes changed")
                canonical = SkillUpload(
                    files=tuple(
                        SkillUploadFile(path=path, content=body)
                        for path, body in sorted(retained.items())
                    )
                )
                if request_digest(canonical.model_dump(mode="json")) != item.target.digest:
                    raise ValueError("target skill digest differs from source bytes")

            await asyncio.gather(*(verify_skill(item) for item in inputs.skills))
            target = AgentSpec(
                name=spec.name,
                description=spec.description,
                model=inputs.model,
                system=spec.system,
                metadata=spec.metadata,
                tools=mapped_tools,
                skills=tuple(item.target for item in inputs.skills),
                mcp_servers=(connection,),
            )
            represented.append(
                SourceRepresentation(
                    source_pin=pin,
                    target=target,
                    credential_refs=(reference,),
                    unavailable_mcp_tools=unavailable_mcp_tools,
                    unavailable_builtin_tools=unavailable_builtin_tools,
                    environment=inputs.environment,
                    unavailable_capabilities=(*template_losses, *connector_losses),
                )
            )
        if len({item.target.model.model_dump_json() for item in represented}) != len(represented):
            raise ValueError("ambiguous source representation")
        return source.model_copy(update={"representations": tuple(represented)})
    except (OSError, ValueError, DaimonError, MuxError, AnthropicError, SQLAlchemyError) as error:
        # Representations are optional capture evidence, never a new failure
        # mode for native callers. Full deployments require
        # this evidence independently and refuse before any provider exposure.
        # Said once here, though: without it the agent silently loses its MCP
        # on the new provider (J2's Argon preview). Only our own fixed
        # sentences are logged, never a provider's or a validator's text.
        log.warning(
            "source_capture.representation_skipped",
            error=type(error).__name__,
            reason=_own_sentence(error),
            tenant_id=str(source.tenant_id),
            agent_id=str(source.agent_id),
            profile=revision.profile if revision is not None else None,
            model=revision.model if revision is not None else None,
            capability_preview=capability_preview,
        )
        return source


def _own_sentence(error: BaseException) -> str | None:
    """The fixed sentence of a ValueError raised by the capture itself, else None.

    Anything raised deeper (a catalogue read, a validator) may carry content.
    """
    frame = error.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    if (
        type(error) is not ValueError
        or frame is None
        or frame.tb_frame.f_code is not _capture_source_capabilities.__code__
    ):
        return None
    return str(error)


def source_instructions_preserved(required: str | None, system: str | None) -> bool:
    """Retained source text is an exact prefix, with a paragraph boundary."""
    return (
        required is None
        or system is not None
        and (system == required or system.startswith(required + "\n\n"))
    )


def compose_source_instructions(
    required: str | None, system: str | None, *, deduplicate_prefix: bool = False
) -> str:
    """Preserve the source prefix once, retaining the target's remaining paragraphs."""
    if source_instructions_preserved(required, system):
        return system or ""
    assert required is not None
    if not deduplicate_prefix:
        return required + ("\n\n" + system if system else "")
    suffix = (system or "").split("\n\n") if system else []
    common = 0
    for left, right in zip(required.split("\n\n"), suffix, strict=False):
        if left != right:
            break
        common += 1
    rest = "\n\n".join(suffix[common:])
    return required + ("\n\n" + rest if rest else "")


def _source_extension_obligation(
    extension: ExtensionConfig,
    *,
    native_target: AgentSpec | None = None,
    foreign_target: bool = False,
) -> Literal["model_controls", "source_definition"] | None:
    if extension.version != 1:
        return "source_definition"
    if extension.namespace == "anthropic.model_config":
        from mux.drivers.anthropic import AgentModelConfig

        try:
            native = AgentModelConfig.model_validate(dict(extension.value))
        except ValueError:
            return "source_definition"
        if native_target is not None and extension in (native_target.extensions or ()):
            model_id = native.model if isinstance(native.model, str) else native.model.id
            if model_id != native_target.model.id:
                return "source_definition"
            profile = get_profile(backend_selection(native_target.model.provider).profile)
            if any(
                offered.namespace == extension.namespace and offered.version == extension.version
                for offered in profile.extensions
            ):
                # The owning native compiler emits the complete unchanged model object.
                return None
        model = extension.value.get("model")
        if isinstance(model, dict):
            if set(model) <= {"id", "speed"} and model.get("speed") is None:
                return None
            if foreign_target:
                return "model_controls"
        return "source_definition"
    if (
        extension.namespace == "anthropic.multiagent"
        and extension.value == {"clear": True}
        or extension.namespace == "anthropic.agent_create_nulls"
    ):
        return None
    return "source_definition"


def _mcp_credential_servers(source: BackendAgent, binding: ResourceBinding) -> frozenset[str]:
    """Pair only owner-projected credentials with their MCP capability."""
    if (
        binding.kind != "credential"
        or binding.credential_ref is None
        or binding.native is not None
        or binding.resource is not None
        or binding.target_path is not None
    ):
        return frozenset()
    paired: set[str] = (
        {
            server.name
            for server in source.effective_spec.mcp_servers or ()
            if server.credential_ref == binding.credential_ref
        }
        if source.effective_spec is not None
        else set()
    )
    if source.resource_scope is not None and binding.id == binding.credential_ref:
        caller = (
            f"caller-mcp:{source.tenant_id}:{source.resource_scope.account_id}:{source.agent_id}"
        )
        if binding.id == caller:
            paired.add(DAIMON_SERVER_NAME)
        elif binding.id.startswith(caller + ":slack-turn:"):
            try:
                UUID(binding.id.removeprefix(caller + ":slack-turn:"))
            except ValueError:
                pass
            else:
                paired.add(DAIMON_SERVER_NAME)
    return frozenset(paired)


def memory_mount_retained(source: BackendAgent, resources: tuple[ResourceBinding, ...]) -> bool:
    """Whether `resources` carry the agent's memory store exactly as its sessions mount
    it: the store is the only resource, in the agent's own resource scope, with a
    settled read/write posture. Only then can that store go where the resources go."""
    from daimon.core.memory_resource import memory_mount

    if len(resources) != 1 or source.memory_store_id is None:
        return False
    native, ref = resources[0].native, resources[0].resource
    return (
        source.source is not None
        and source.resource_scope is not None
        and source.memory_read_only is not None
        and ref is not None
        and ref.provider == source.source.ref.provider
        and ref.kind == "memory_store"
        and ref.id == source.memory_store_id
        and (ref.tenant_id, ref.account_id)
        == (source.resource_scope.tenant_id, source.resource_scope.account_id)
        and native is not None
        and (native.namespace, native.version) == ("anthropic.session_resource_create", 1)
        and native.value
        in (
            {"resource": memory_mount(source.memory_store_id, read_only=True)},
            {"resource": memory_mount(source.memory_store_id, read_only=source.memory_read_only)},
        )
    )


def classify_source_obligations(
    source: BackendAgent,
    target: AgentSpec | None,
    *,
    environment: EnvironmentSpec | None,
    resources: tuple[ResourceBinding, ...] = (),
    asks_before_publishing: bool = False,
    retained_agent: ResourceRef | None = None,
) -> frozenset[str]:
    """One source-preservation rule for pre-allocation normalization and preparation."""
    spec = source.effective_spec
    if spec is None:
        return frozenset({"source_definition"})
    drops = target.declared_drops if target is not None else ()
    represented = represented_source(source, target)
    # Existing native sessions retain their agent and composer inputs;
    # this proof never authorizes a newly compiled counterpart.
    native_retained = (
        (retained_agent is not None)
        and source.source is not None
        and retained_agent == source.source.ref
        and retained_agent.kind == "agent"
        and source.source.native is not None
        and source.resource_scope is not None
        and source.source.ref.tenant_id == source.resource_scope.tenant_id
        and str(source.tenant_id) == source.resource_scope.tenant_id
        and source.source.lifecycle == "active"
        and source.source.archived_at is None
        and target == spec
        and retained_agent.provider == spec.model.provider
        and not drops
        and source.environment is not None
        and source.environment.lifecycle == "active"
        and source.environment.archived_at is None
        and environment == source.environment.spec
        and resources == source.resource_requirements
    )
    missing: set[str] = set()
    if represented is not None:
        missing.update(set(represented.unavailable_mcp_tools) - set(drops))
        missing.update(set(represented.unavailable_builtin_tools) - set(drops))
        missing.update(set(represented.unavailable_capabilities) - set(drops))
    if not source_instructions_preserved(spec.system, target.system if target else None):
        missing.add("instructions")
    source_tools = tuple(spec.tools or ())
    from mux.drivers.anthropic import AGENT_TOOLS_NAMESPACE, agent_tools

    for extension in spec.extensions or ():
        if extension.version != 1:
            missing.add("source_definition")
        elif extension.namespace == AGENT_TOOLS_NAMESPACE:
            try:
                native = agent_tools(extension)
            except ValueError:
                missing.update(("source_definition", "tools"))
            else:
                if target is not None and "tools" not in drops:
                    source_tools = native.intent()
                    if extension in (target.extensions or ()):
                        # The native compiler preserves this exact tool payload,
                        # overriding the target's neutral tools.
                        source_tools = tuple(target.tools or ())
                    elif not represented and native.has_unrepresented_toolset():
                        missing.add("tools")
        else:
            foreign_target = target is not None and target.model.provider != spec.model.provider
            obligation = _source_extension_obligation(
                extension,
                native_target=target if not foreign_target else None,
                foreign_target=foreign_target,
            )
            if obligation is not None and not (
                obligation == "model_controls" and obligation in drops
            ):
                missing.add(obligation)
    profile = get_profile(backend_selection(spec.model.provider).profile)
    tools_required = bool(
        spec.tools or any(item.value.get("tools") for item in spec.extensions or ())
    )
    if asks_before_publishing or (
        tools_required
        and "tools" not in drops
        and (
            target is None
            or not represented
            and (
                native_agent_tools(profile, spec.model_copy(update={"tools": source_tools}))
                != native_agent_tools(profile, target)
            )
        )
    ):
        missing.add("tools")
    if (
        spec.skills
        and "skills" not in drops
        and (target is None or not represented and target.skills != spec.skills)
    ):
        missing.add("skills")
    if (
        spec.mcp_servers
        and "mcp" not in drops
        and (target is None or not represented and target.mcp_servers != spec.mcp_servers)
    ):
        # `mcp` is its own obligation: declaring `tools` alone must never
        # silently discharge it — an excluded mcp toolset requires an
        # explicit `mcp` (and/or `tools`) declaration, never implied.
        missing.add("mcp")
    if source.repository_sources and "repository" not in drops and not native_retained:
        missing.add("repository")
    # Connectors the representation names as lost go with their credentials.
    lost_servers: set[str] = (
        {
            item.removeprefix("mcp_server:")
            for item in represented.unavailable_capabilities
            if item.startswith("mcp_server:")
        }
        if represented is not None
        else set()
    )
    retained_servers = (target.mcp_servers or ()) if target is not None else ()
    retained_mcp = {server.name for server in retained_servers}
    retained_credentials = {binding.credential_ref for binding in resources} | {
        server.credential_ref for server in retained_servers
    }
    retained_credentials.discard(None)
    for server in spec.mcp_servers or ():
        if server.credential_ref and not native_retained:
            if represented and server.credential_ref in represented.credential_refs:
                continue
            if server.name in lost_servers:
                continue
            if server.name in retained_mcp or server.credential_ref in retained_credentials:
                missing.add("credentials")
            elif "mcp" not in drops:
                missing.add("mcp")
    if (
        not native_retained
        and "credentials" not in drops
        and any(repository.credential_ref for repository in source.repository_sources)
    ):
        missing.add("credentials")
    for resource in source.resource_requirements:
        if (
            native_retained
            and resource.kind == "credential"
            and resource.credential_ref is not None
            and resource.native is None
            and resource.resource is None
        ):
            continue
        paired = _mcp_credential_servers(source, resource)
        if paired:
            if represented and resource.credential_ref in represented.credential_refs:
                continue
            if paired <= lost_servers:
                continue
            if paired & retained_mcp or resource.credential_ref in retained_credentials:
                missing.add("credentials")
            elif "mcp" in drops:
                continue
            else:
                missing.add("mcp")
                continue
        if resource.target_path is not None and "files" not in drops:
            missing.add("files")
        if (
            resource.kind == "artifact"
            and resource.resource is not None
            and resource.resource.kind == "file"
            and source.resource_scope is not None
            and (resource.resource.tenant_id, resource.resource.account_id)
            == (source.resource_scope.tenant_id, source.resource_scope.account_id)
            and resource.native is None
            and resource.credential_ref is None
            and resource.target_path is not None
        ):
            continue
        # No installed materializer proves credentials or opaque private resources.
        # A declared `credentials` drop discharges a bare credential: the target runs
        # without it, and nothing carries it there.
        if resource.kind != "credential":
            missing.add("source_definition")
        elif "credentials" not in drops:
            missing.add("credentials")
    if (
        (source.memory_required is not False or source.memory_store_id is not None)
        and "memory" not in drops
        and not native_retained
        and not memory_mount_retained(source, resources)
    ):
        missing.add("memory")
    if source.environment is not None and source.environment.spec.native_config is not None:
        from mux.drivers.anthropic import EnvironmentConfig

        try:
            EnvironmentConfig.model_validate(dict(source.environment.spec.native_config.value))
        except ValueError:
            missing.add("source_definition")
    # Removing execution is explicit; retaining a source environment requires
    # its complete canonical policy, which native readback must also verify.
    if target is None or not (
        native_retained
        or ("sandbox" in drops and environment is None and not resources)
        or (
            source.environment is not None
            and environment is not None
            and environment == source.environment.spec
            and environment.native_config is None
        )
    ):
        missing.add("environment")
    if spec.model.options:
        missing.add("source_definition")
    return frozenset(missing)


def preview_source_drops(source: BackendAgent, model: ModelRef) -> AgentSpec:
    """Derive explicit losses using the same complete source-preservation rule."""
    spec = source.effective_spec
    profile = backend_selection(model.provider).profile
    if (
        spec is None
        or source.source is None
        or source.environment is None
        or source.resource_scope is None
        or source.memory_required is None
        or source.memory_read_only is None
        or source.asks_before_publishing is None
        or model.options
    ):
        raise UnsupportedCapability(("source_context",), profile)
    if profile == backend_selection(spec.model.provider).profile:
        target = spec.model_copy(update={"model": model})
        missing = classify_source_obligations(
            source,
            target,
            environment=source.environment.spec,
            resources=source.resource_requirements,
            retained_agent=source.source.ref,
            asks_before_publishing=source.asks_before_publishing,
        )
        if missing:
            raise UnsupportedCapability(tuple(sorted(missing)), profile)
        return target
    target = AgentSpec(
        name=spec.name,
        description=spec.description,
        model=model,
        system=spec.system,
        tools=(),
        skills=(),
        mcp_servers=(),
        metadata=spec.metadata,
    )
    representations = [item for item in source.representations if item.target.model == model]
    if representations:
        if len(representations) != 1 or representations[0].source_pin != source_preservation_pin(
            source
        ):
            raise UnsupportedCapability(("source_representation",), profile)
        target = representations[0].target
    represented = represented_source(source, target)
    environment = represented.environment if represented is not None else None
    missing = classify_source_obligations(
        source,
        target,
        environment=environment,
        asks_before_publishing=source.asks_before_publishing,
    )
    try:
        target = AgentSpec.model_validate(
            {
                **target.model_dump(),
                "declared_drops": tuple(
                    sorted("sandbox" if item == "environment" else item for item in missing)
                ),
            }
        )
    except ValueError:
        raise UnsupportedCapability(tuple(sorted(missing)), profile) from None
    missing = classify_source_obligations(
        source,
        target,
        environment=environment,
        asks_before_publishing=source.asks_before_publishing,
    )
    if missing:
        raise UnsupportedCapability(tuple(sorted(missing)), profile)
    if target.declared_drops:
        target = target.model_copy(
            update={
                "system": compose_source_instructions(
                    spec.system,
                    "Unavailable capabilities: " + ", ".join(target.declared_drops) + ".",
                )
            }
        )
    return target


#: The template drop a provider turn's consent named before Daimon posted the
#: switch card itself (`tool_safety.self_carded_tools`).
_SWITCH_TOOL_DROP: Final[str] = f"mcp_tool:{DAIMON_SERVER_NAME}:request_agent_backend_switch"


def _consented(target: AgentSpec | None, consent: _BackendDropConsent) -> bool:
    return (
        target is not None
        and target.declared_drops == consent.declared_drops
        and request_digest(target.model_dump(mode="json")) == consent.target_digest
    )


def _with_switch_tool_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before the switch tool was offered names it.

    That consent declared the tool a loss: not allowed on Daimon's MCP, one more
    drop, and the drops line in the instructions. Only that one difference is
    rebuilt; anything else still differs and still needs a fresh confirmation.
    """
    if _SWITCH_TOOL_DROP in target.declared_drops:
        return None
    tool = _SWITCH_TOOL_DROP.rsplit(":", 1)[1]
    from mux.contracts.resources import MCPConnection

    servers: list[MCPConnection] = []
    found = False
    for server in target.mcp_servers or ():
        allowed = server.tool_policy.get("allowed_tools") if server.tool_policy else None
        if server.name == DAIMON_SERVER_NAME and isinstance(allowed, list) and tool in allowed:
            found = True
            server = server.model_copy(
                update={
                    "tool_policy": {
                        **server.tool_policy,
                        "allowed_tools": [name for name in allowed if name != tool],
                    }
                }
            )
        servers.append(server)
    if not found:
        return None
    drops = tuple(sorted((*target.declared_drops, _SWITCH_TOOL_DROP)))
    return target.model_copy(
        update={
            "mcp_servers": tuple(servers),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _with_tools_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before a template kept the source's tools names it.

    That consent (before #1483) declared `tools` a whole loss: no tools on the
    target, `tools` among its drops, and neither of today's granular web losses
    (an OpenAI consent already named web fetch). Only that difference is rebuilt;
    the copy then runs with its tools from now on.
    """
    if "tools" in target.declared_drops or not target.tools:
        return None
    from mux.profiles.openai import PERSISTENT_WORKSPACE

    granular: set[str] = {"builtin_tool:web_search"}
    if target.model.provider != PERSISTENT_WORKSPACE.provider:
        granular.add("builtin_tool:web_fetch")
    drops = tuple(sorted({*target.declared_drops, "tools"} - granular))
    return target.model_copy(
        update={
            "tools": (),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _consented_before(
    target: AgentSpec, source_system: str | None, consent: _BackendDropConsent
) -> bool:
    """A consent recorded before a template kept the source's tools (#1483) or
    offered the switch tool (#1438).

    Either difference alone, or both, is rebuilt; anything else still needs a
    fresh confirmation.
    """
    without_tools = _with_tools_dropped(target, source_system)
    candidates = (
        _with_switch_tool_dropped(target, source_system),
        without_tools,
        _with_switch_tool_dropped(without_tools, source_system) if without_tools else None,
    )
    return any(_consented(candidate, consent) for candidate in candidates)


def source_preservation_pin(source: BackendAgent) -> str:
    """Pin complete owner-captured intent independently of the current caller address."""
    updates: dict[str, object] = {"resource_scope": None}
    for name in ("source", "environment"):
        resource = getattr(source, name)
        if resource is not None:
            updates[name] = resource.model_copy(
                update={"ref": resource.ref.model_copy(update={"account_id": None})}
            )
    captured = source.model_copy(update=updates)
    # Direct facts include the intentionally non-serialized resource projection.
    facts = dict(captured)
    # Derived representation evidence must not change old consent pins, nor
    # recursively become part of the source it attests.
    facts.pop("representations", None)
    return request_digest(cast(JsonValue, TypeAdapter(object).dump_python(facts, mode="json")))


def represented_source(
    source: BackendAgent, target: AgentSpec | None
) -> SourceRepresentation | None:
    if target is None:
        return None
    matches = [
        item
        for item in source.representations
        if item.source_pin == source_preservation_pin(source)
        and item.target.model == target.model
        and item.target.tools == target.tools
        and item.target.skills == target.skills
        and item.target.mcp_servers == target.mcp_servers
        and item.target.metadata == target.metadata
        and item.target.name == target.name
        and item.target.description == target.description
        and item.target.extensions == target.extensions
        and source_instructions_preserved(item.target.system, target.system)
    ]
    return matches[0] if len(matches) == 1 else None


async def prepare_source(request: ProviderPreparationRequest) -> ProviderPreparationRequest:
    """Prepare scoped source intent; native output verification remains mandatory."""
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.ma_identity import derive_agent_uuid

    revision = request.admission.backend_revision
    if (
        request.admission.backend_revision_origin != "agent"
        or not request.deps.agent_backends
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or revision is None
        or request.scope.tenant_id != str(request.tenant_id)
        or request.scope.account_id != str(request.session_account_id)
        or request.scope.account_id != str(request.admission.account_id)
        or request.scope.is_platform
        or request.scope.is_legacy_host_authorized
    ):
        raise AdmissionDenied(reason="backend_unsupported")

    request = replace(
        request,
        deadline=turn_deadline(
            now=request.now(),
            deadline=request.deadline,
            authority_deadline=None,
        ),
    )

    agent_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    setting = None
    from daimon.core.stores import agent_backends, mux_state

    async with asyncio.timeout(
        remaining_s(request.deadline, now=request.now(), refuse_expired=True)
    ):
        async with request.deps.sessionmaker() as session:
            with session.no_autoflush:
                setting = await mux_state.get_config_agent_revision(
                    session,
                    revision,
                    agent_id=agent_id,
                )
                provisional = request.admission.backend_agent_revision
                if provisional is not None:
                    if (
                        await mux_state.get_config_revision(
                            session, revision.channel, revision.local
                        )
                        is None
                    ):
                        setting = await agent_backends.current(
                            session, request.tenant_id, agent_id, persisted=True
                        )
                    if setting != provisional or setting is None or setting.drop_consent is None:
                        raise AgentBackendRefused("re_confirm")
    timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
    source = await asyncio.wait_for(
        capture_preparation_source(request, fresh=True)
        if setting is not None
        else capture_preparation_source(request),
        timeout=timeout,
    )
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != agent_id
        or source.resource_scope != request.scope
        or source.source is None
        or source.source.ref.kind != "agent"
        or source.source.ref.tenant_id != request.scope.tenant_id
        or source.source.lifecycle != "active"
        or source.source.ref.id != request.admission.agent.id
        or source.source.revision.local != request.admission.agent.version
    ):
        if setting is not None:
            raise AgentBackendRefused("re_confirm")
        raise UnsupportedCapability(("source_context",), revision.profile)
    # Capture is evidence of source intent, not evidence that a target preserved it.
    spec = source.effective_spec
    if spec is None:
        raise UnsupportedCapability(("source_definition",), revision.profile)
    target = None
    if setting is not None:
        consent = setting.drop_consent
        if consent is None or setting.backend != revision.backend:
            raise AgentBackendRefused("re_confirm")
        if revision.model is None and revision.backend != spec.model.provider:
            raise AgentBackendRefused("re_confirm")
        if source_preservation_pin(source) != consent.source_pin:
            raise AgentBackendRefused("re_confirm")
        target = preview_source_drops(
            source, ModelRef(provider=revision.backend, id=revision.model or spec.model.id)
        )
        if not _consented(target, consent) and not _consented_before(target, spec.system, consent):
            raise AgentBackendRefused("re_confirm")
    retain_native = (
        (setting is not None)
        and not setting.declared_drops
        and revision.profile == backend_selection(spec.model.provider).profile
    )
    missing = classify_source_obligations(
        source,
        target,
        retained_agent=source.source.ref if retain_native else None,
        environment=source.environment.spec
        if retain_native and source.environment
        else represented.environment
        if (represented := represented_source(source, target)) is not None
        else None,
        resources=source.resource_requirements if retain_native else (),
        asks_before_publishing=request.admission.asks_before_publishing,
    )
    if missing:
        raise UnsupportedCapability(tuple(sorted(missing)), revision.profile)
    return replace(request, source=source, target_spec=target)


async def read_preparation_counterpart(
    request: ProviderPreparationRequest,
) -> ProviderPreparationRequest:
    """Verify the prepared source's owned native counterpart before handoff."""

    from daimon.core.channel_backend import AgentBackendRefused

    try:
        selected = await prepare_source(request)
        request = replace(request, deadline=selected.deadline)
        source, revision = selected.source, selected.admission.backend_revision
        if source is None or revision is None:
            raise AdmissionDenied(reason="backend_unsupported")
        target = selected.target_spec
        from daimon.core.turn.runtimes import ScopedTurnRuntimes, prepare_channel_runtime

        # Provider setup this turn's runtime needs first (e.g. the tenant's GPT
        # environment, created once). Only a real turn's preparation reaches here.
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        selected = await asyncio.wait_for(prepare_channel_runtime(selected), timeout=timeout)
        if isinstance(selected.deps.turn_runtimes, ScopedTurnRuntimes):
            selected.deps.turn_runtimes.bind_preparation(selected)
        runtime = selected.deps.turn_runtimes.get(revision.profile)
        if runtime is None or runtime.read_agent_counterpart is None:
            raise UnsupportedCapability(("native_counterpart",), revision.profile)
        if runtime.materialize_root is not None:
            timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
            allocated = await asyncio.wait_for(runtime.materialize_root(selected), timeout=timeout)
            selected = replace(selected, counterpart=allocated)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        counterpart = await asyncio.wait_for(
            runtime.read_agent_counterpart(selected), timeout=timeout
        )
        if selected.counterpart is not None and counterpart != selected.counterpart:
            raise UnsupportedCapability(("native_counterpart",), revision.profile)
        timeout = remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        current_source = await asyncio.wait_for(
            capture_preparation_source(request, fresh=True)
            if (target is not None)
            else capture_preparation_source(request),
            timeout=timeout,
        )
        remaining_s(request.deadline, now=request.now(), refuse_expired=True)
        if (target is not None) and current_source != source:
            raise AgentBackendRefused("re_confirm")
        if target is None or counterpart.agent.spec != target or current_source != source:
            raise UnsupportedCapability(("source_drift",), revision.profile)
        return replace(selected, counterpart=counterpart, verified_counterpart=counterpart)
    except AdmissionDenied as error:
        # A refusal that names its own cause (e.g. GPT still being set up for this
        # tenant) reaches the person as itself; anything else stays "unsupported".
        if error.reason != "backend_unsupported":
            raise
        raise AdmissionDenied(reason="backend_unsupported") from error
    except AgentBackendRefused as error:
        # A stale consent is not missing provider setup: name the fresh preview
        # the person needs. Any other backend refusal stays unsupported (J6g).
        if error.reason == "re_confirm":
            raise AdmissionDenied(reason="backend_unsupported", cause="re_confirm") from error
        raise AdmissionDenied(reason="backend_unsupported") from error
    except UnsupportedCapability as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    except Exception as error:
        from daimon.core.channel_backend import backend_refusal_causes, transient_failure

        # Anything else (a timeout included) is not proven missing setup. Name it
        # with fixed labels only (class, our frame, SQLSTATE; never its text) so
        # its real cause is never lost (smoke dce44708 J2: four inner causes logged
        # as "redacted"), and say busy when a retry can clear it.
        busy = transient_failure(error)
        log.warning(
            "turn.preparation_counterpart_error",
            causes=backend_refusal_causes(error),
            transient=busy,
            profile=(
                request.admission.backend_revision.profile
                if request.admission.backend_revision is not None
                else None
            ),
        )
        raise AdmissionDenied(
            reason="backend_unsupported",
            cause="preparation_busy" if busy else "preparation_failed",
        ) from error


class CounterpartPlan(Protocol):
    """Read-only operator pins shared by native deployment plans."""

    @property
    def profile(self) -> str: ...
    @property
    def tenant_id(self) -> str: ...
    @property
    def account_id(self) -> str: ...
    @property
    def platform(self) -> str: ...
    @property
    def channel_id(self) -> str: ...
    @property
    def project(self) -> str: ...
    @property
    def agent_id(self) -> str | None: ...
    @property
    def skill_ids(self) -> tuple[str, ...]: ...
    @property
    def source_agent_id(self) -> UUID | None: ...
    @property
    def source_agent_version(self) -> int | None: ...
    @property
    def counterpart_catalog_id(self) -> str | None: ...
    @property
    def counterpart_catalog_revision(self) -> int | None: ...
    @property
    def root_materialization(self) -> bool: ...
    @classmethod
    def model_validate(cls, obj: object) -> Self: ...


def validate_counterpart_pins(
    plan: CounterpartPlan,
) -> None:
    """Legacy rows have no counterpart; partial source/catalog pins never authorize one."""
    if plan.root_materialization:
        # A tenant-wide agent template: identity comes from the admitted agent,
        # never from the row, so the row may pin none of it.
        if (
            (plan.source_agent_id is not None)
            or plan.source_agent_version is not None
            or plan.counterpart_catalog_id is not None
            or plan.counterpart_catalog_revision is not None
            or plan.agent_id is not None
        ):
            raise ValueError("root materialization takes its identity from the admitted agent")
        return
    pins = (
        plan.source_agent_id,
        plan.source_agent_version,
        plan.counterpart_catalog_id,
        plan.counterpart_catalog_revision,
    )
    if any(pin is not None for pin in pins) and any(pin is None for pin in pins):
        raise ValueError("a counterpart requires complete source and catalog revision pins")
    if all(pin is not None for pin in pins) and plan.agent_id is None:
        raise ValueError("a counterpart requires a native agent identity")


def _matching_rows(
    rows: list[dict[str, JsonValue]],
    revision: ConfigRevision,
    request: ProviderPreparationRequest,
    *,
    allow_wildcard: bool,
) -> list[dict[str, JsonValue]]:
    """`profile` and `tenant_id` are always exact. The other scope fields are
    exact-matched first; `allow_wildcard` additionally accepts `_WILDCARD` rows
    on those fields only — the caller tries exact-only before falling back."""
    from daimon.core.ma_identity import derive_agent_uuid

    if request.admission.backend_revision_origin != "agent":
        kind: TurnKind = "channel"
        source_agent_id, source_agent_version = None, None
    else:
        agent = request.admission.agent
        kind = "root"
        source_agent_id = str(derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=agent.id))
        source_agent_version = agent.version
    return _rows_for(
        rows,
        revision,
        tenant_id=request.scope.tenant_id,
        account_id=request.scope.account_id,
        allow_wildcard=allow_wildcard,
        kind=kind,
        source_agent_id=source_agent_id,
        source_agent_version=source_agent_version,
    )


type TurnKind = Literal["channel", "root"]


def _same_agent(pinned: object, actual: str | None) -> bool:
    """Source pins are UUIDs: compare them as UUIDs, not by spelling."""
    try:
        return actual is not None and UUID(str(pinned)) == UUID(actual)
    except ValueError:
        return False


def _pinned_source(row: dict[str, JsonValue]) -> bool:
    return row.get("source_agent_id") not in (None, _WILDCARD)


def _rows_for(
    rows: list[dict[str, JsonValue]],
    revision: ConfigRevision,
    *,
    tenant_id: str,
    account_id: str,
    allow_wildcard: bool,
    kind: TurnKind,
    source_agent_id: str | None,
    source_agent_version: int | None,
) -> list[dict[str, JsonValue]]:

    def field_matches(row: dict[str, JsonValue], field: str, actual: object) -> bool:
        value = row.get(field)
        if value == actual:
            return True
        return allow_wildcard and value == _WILDCARD

    def origin_eligible(row: dict[str, JsonValue]) -> bool:
        # A channel turn uses a fixed plan; an agent turn uses a template or
        # a counterpart pinned to its source.
        root = row.get("root_materialization") is True
        pinned = _pinned_source(row)
        if kind == "channel":
            return not (root or pinned)
        if pinned and (
            not _same_agent(row.get("source_agent_id"), source_agent_id)
            or (
                source_agent_version is not None
                and row.get("source_agent_version") != source_agent_version
            )
        ):
            # Another agent's (or version's) pinned row: fall through to the template.
            return False
        return root or pinned

    return [
        row
        for row in rows
        if origin_eligible(row)
        and row.get("profile") == revision.profile
        and row.get("tenant_id") == tenant_id
        and field_matches(row, "platform", revision.channel.platform)
        and field_matches(row, "channel_id", revision.channel.channel_id)
        and field_matches(row, "account_id", account_id)
        and manifest_configuration_matches(row, revision)
    ]


def _most_specific(rows: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """The one matching row strictly narrower than every other, else all of them.

    A row is narrower when the scope fields it names exactly are a strict
    superset of another's: a channel's own row with `"*"` accounts beats a
    deployment-wide default. Rows naming different fields (a channel's row
    and an account's row) are incomparable and all remain, so they refuse
    as ambiguous instead of one silently shadowing the other's credentials.
    """

    def exact(row: dict[str, JsonValue]) -> frozenset[str]:
        return frozenset(field for field in _WILDCARDABLE_FIELDS if row.get(field) != _WILDCARD)

    for candidate in rows:
        if all(exact(other) < exact(candidate) for other in rows if other is not candidate):
            return [candidate]
    return rows


def _operator_configured(
    rows: list[dict[str, JsonValue]], tenant_id: str, revision: ConfigRevision
) -> bool:
    """Whether the operator's manifest has any row for this tenant's profile."""
    return any(
        row.get("tenant_id") == tenant_id and row.get("profile") == revision.profile for row in rows
    )


def _configured_rows() -> list[dict[str, JsonValue]]:
    """The operator manifest's rows; none when no manifest is configured.

    A configured manifest that can't be read refuses (`AdmissionDenied`) rather than
    falling back to the code defaults, so a broken operator file never widens access.
    """
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        return []
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise AdmissionDenied(reason="backend_unsupported")
        return _ROWS.validate_json(raw)
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def select_runtime_manifest[Plan: CounterpartPlan](
    request: ProviderPreparationRequest, plan_type: type[Plan]
) -> Plan:
    """Select exactly one installed scope/configuration grant before credential discovery.

    A deployment-wide default is one row with `_WILDCARD` ("*") on `platform`,
    `channel_id` and/or `account_id` — never on `tenant_id` or `profile`. An
    exact match always wins over a wildcard row (computed first, tried first);
    wildcard rows are only consulted when no exact row matches. Among those,
    a row whose exactly named scope fields strictly include every other
    candidate's wins (a channel's own row, even with `"*"` accounts, beats a
    deployment-wide default); otherwise the existing "exactly one candidate
    or refuse" rule refuses them as ambiguous.
    The resolved plan always carries the request's real platform/channel_id/
    account_id — a wildcard row is a template, never a literal "*" identity.
    """

    from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
    from daimon.core.turn.runtime_defaults import default_rows

    revision = request.admission.backend_revision
    if revision is None:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        rows = _configured_rows()
        selected = _matching_rows(rows, revision, request, allow_wildcard=False)
        if not selected:
            selected = _most_specific(_matching_rows(rows, revision, request, allow_wildcard=True))
        if not selected and not _operator_configured(rows, request.scope.tenant_id, revision):
            # The operator configured nothing for this tenant's profile: the
            # deployment's code defaults serve it. Where the operator configured
            # the profile, its rows alone decide, so a narrower scope never widens.
            provider = get_profile(revision.profile).provider
            if provider in DEFAULT_KEY_ENV and not provider_key_present(provider):
                # The operator's step, named only in the log: set DEFAULT_KEY_ENV[provider].
                log.warning(
                    "provider_runtime.not_configured",
                    provider=provider,
                    key_env=DEFAULT_KEY_ENV[provider],
                )
                raise AdmissionDenied(
                    reason="backend_unsupported",
                    cause="provider_not_configured",
                    provider=provider,
                )
            defaults = default_rows(request.scope.tenant_id, request)
            selected = _most_specific(
                _matching_rows(defaults, revision, request, allow_wildcard=True)
            )
        if len(selected) != 1:
            raise AdmissionDenied(reason="backend_unsupported")
        resolved = dict(selected[0])
        for field, actual in (
            ("platform", revision.channel.platform),
            ("channel_id", revision.channel.channel_id),
            ("account_id", request.scope.account_id),
        ):
            if resolved.get(field) == _WILDCARD:
                resolved[field] = actual
        return plan_type.model_validate(resolved)
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def manifest_has_row(request: ProviderPreparationRequest) -> bool:
    """Whether the operator's runtime file has any row for this tenant's profile.

    The same rule selection uses before it consults the code defaults
    (`_operator_configured`): where the operator configured the profile at all,
    its rows alone decide, so nothing is provisioned for a turn the defaults will
    never serve. This reads only the file, never the code defaults. A configured
    file that can't be read counts as having a row: never provision around a
    configuration the operator meant to install.
    """
    revision = request.admission.backend_revision
    if revision is None:
        return False
    try:
        rows = _configured_rows()
    except AdmissionDenied:
        return True
    return _operator_configured(rows, request.scope.tenant_id, revision)


def root_counterpart_catalog_id(request: ProviderPreparationRequest) -> str:
    """The one catalog entry a template row's counterpart lives at for this agent version.

    Keyed by the admitted agent's scoped identity, its MA version, the admitted
    profile and model and the consented target spec, so an agent edit, a model
    change or a different derivation gets a new entry, and an entry can never
    serve another agent, version, model or spec.
    """
    from daimon.core.ma_identity import derive_agent_uuid

    revision, target = request.admission.backend_revision, request.target_spec
    if revision is None or revision.model is None or target is None:
        raise AdmissionDenied(reason="backend_unsupported")
    agent_id = derive_agent_uuid(
        tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
    )
    # The consented target's digest: a different derived target never reuses an entry.
    spec = request_digest(target.model_dump(mode="json"))[:16]
    return (
        f"root:{agent_id}:{request.admission.agent.version}:{revision.profile}"
        f":{revision.model}:{spec}"
    )


def validate_counterpart_request(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
) -> ConfigRevision:
    """Validate the same immutable source/configuration pins before native allocation."""
    from daimon.core.ma_identity import derive_agent_uuid

    try:
        validate_counterpart_pins(
            plan,
        )
    except ValueError:
        raise AdmissionDenied(reason="backend_unsupported") from None
    revision, scope = request.admission.backend_revision, request.scope
    if plan.root_materialization:
        if (
            request.admission.backend_revision_origin != "agent"
            or revision is None
            or not request.deps.channel_backends
            or not request.deps.agent_backends
            or request.deps.turn_path != "mux"
            or scope.tenant_id != str(request.tenant_id)
            or scope.account_id != str(request.admission.account_id)
            or scope.account_id != str(request.session_account_id)
            or (plan.tenant_id, plan.account_id) != (scope.tenant_id, scope.account_id)
            or (revision.channel.tenant_id, revision.channel.platform, revision.channel.channel_id)
            != (plan.tenant_id, plan.platform, plan.channel_id)
            or request.platform != plan.platform
            or revision.profile != plan.profile
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        if select_runtime_manifest(request, type(plan)) != plan:
            raise AdmissionDenied(reason="backend_unsupported")
        return revision
    if (
        request.admission.backend_revision_origin != "agent"
        or revision is None
        or not request.deps.channel_backends
        or request.deps.turn_path != "mux"
        or scope.tenant_id != str(request.tenant_id)
        or scope.account_id != str(request.admission.account_id)
        or scope.account_id != str(request.session_account_id)
        or (plan.tenant_id, plan.account_id) != (scope.tenant_id, scope.account_id)
        or (revision.channel.tenant_id, revision.channel.platform, revision.channel.channel_id)
        != (plan.tenant_id, plan.platform, plan.channel_id)
        or request.platform != plan.platform
        or revision.profile != plan.profile
        or (
            plan.source_agent_id
            != derive_agent_uuid(
                tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
            )
            or plan.source_agent_version != request.admission.agent.version
        )
    ) or (
        plan.counterpart_catalog_id is None
        or plan.counterpart_catalog_revision is None
        or plan.agent_id is None
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if select_runtime_manifest(request, type(plan)) != plan:
        raise AdmissionDenied(reason="backend_unsupported")
    return revision


async def materialize_root_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    resources: NeutralResourceHost,
) -> AgentRevision:
    """Publish this agent version's counterpart from its consented target, once.

    Runs after source preparation proved the drop consent, so the target is the
    exact spec the admin confirmed. A concurrent first turn loses the catalog
    compare-and-set and reads the winner's entry; the native key is the catalog
    id, so native creation is idempotent too. `read_counterpart` then verifies
    the entry like any operator-installed counterpart.
    """
    from daimon.core.skills.ingest import SkillIngestError
    from daimon.core.stores.neutral_catalog import CatalogConflict
    from sqlalchemy import text

    validate_counterpart_request(plan, request)
    source, target = request.source, request.target_spec
    if (
        (not plan.root_materialization)
        or target is None
        or source is None
        or source.source is None
        or source.resource_scope != request.scope
        or resources.scope != request.scope
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    catalog_id = root_counterpart_catalog_id(request)
    try:
        return await resources.entry(catalog_id)
    except SkillIngestError:
        pass
    # Concurrent first turns publish one native agent: the loser waits, then reads
    # the winner's entry. Catalog entries (and so this key) are per caller account.
    body = json.dumps([request.scope.tenant_id, request.scope.account_id, plan.project, catalog_id])
    key = int.from_bytes(hashlib.sha256(body.encode()).digest()[:8], signed=True)
    async with request.deps.sessionmaker() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        try:
            return await resources.entry(catalog_id)
        except SkillIngestError:
            pass
        existing = await resources.catalog.get_agent(resources.scope, catalog_id)
        if existing is not None:
            # An archived entry is never republished over; this agent version stays refused.
            log.warning(
                "turn.root_counterpart_archived",
                catalog_id=catalog_id,
                lifecycle=existing.agent.lifecycle,
                tenant_id=request.scope.tenant_id,
            )
            raise AdmissionDenied(reason="backend_unsupported")
        try:
            return await resources.publish(catalog_id, target, expected_revision=0, key=catalog_id)
        except CatalogConflict:
            return await resources.entry(catalog_id)


async def read_counterpart(
    plan: CounterpartPlan,
    request: ProviderPreparationRequest,
    *,
    source: BackendAgent,
    resources: NeutralResourceHost,
    verify_native: Callable[[AgentRevision], Awaitable[None]],
) -> AgentRevision:
    """Verify an installed native catalog component, without authorizing execution.

    Complete source preservation is a separate owner's proof. Names and source
    digests cannot substitute for identity, version or operator configuration.
    Readback detects drift; it does not create a native CAS guarantee.
    """
    from daimon.core.skills.ingest import SkillIngestError
    from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
    from mux.profiles import get_profile

    revision = validate_counterpart_request(
        plan,
        request,
    )
    scope = request.scope

    root = plan.root_materialization
    catalog_id = (
        root_counterpart_catalog_id(request) if root else cast(str, plan.counterpart_catalog_id)
    )
    from daimon.core.ma_identity import derive_agent_uuid

    # Templates bind to this turn's admitted source agent.
    expected_source = (
        derive_agent_uuid(tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id)
        if root
        else plan.source_agent_id
    )
    if (
        source.tenant_id != request.tenant_id
        or source.agent_id != expected_source
        or resources.scope != scope
        or resources.workspace != plan.project
        or resources.backend.capabilities().profile_id != plan.profile
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        row = await resources.entry(catalog_id)
        if (
            (
                (not root)
                and (
                    row.local_revision != plan.counterpart_catalog_revision
                    or row.agent.ref.id != plan.agent_id
                )
            )
            or row.agent.spec.model.id != revision.model
            or (
                request.admission.backend_counterpart is not None
                and row != request.admission.backend_counterpart
            )
            or row.agent.spec.model.provider != get_profile(plan.profile).provider
            or tuple(pin.id for pin in row.agent.spec.skills or ()) != plan.skill_ids
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        checked = await resources.read(catalog_id)
        await verify_native(checked)
        if (
            checked != row
            or select_runtime_manifest(request, type(plan)) != plan
            or await resources.entry(catalog_id) != row
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        return checked
    except (
        ValueError,
        KeyError,
        SkillIngestError,
        ProviderError,
        ScopeViolation,
        UnsupportedCapability,
    ):
        raise AdmissionDenied(reason="backend_unsupported") from None
