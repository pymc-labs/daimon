"""The declared provider profiles and a lookup by id.

Support levels are what the provider documents today. Anything without
evidence is left undeclared, which reads as `unknown` and is refused at
admission; the conformance suite is what moves a capability out of
`unknown`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mux.contracts.config import BackendConfig, ResolvedBackend, ThreadMode, resolve_default
from mux.contracts.errors import InvalidConfig
from mux.contracts.ids import Provider
from mux.contracts.profile import SUPPORTED, AccountingMode, Profile
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.profiles.anthropic import MANAGED_AGENTS
from mux.profiles.gemini import ARGON_MODEL, INLINE_REUSE, WORKER_MODEL
from mux.profiles.openai import ASTRA_MODEL, CONVERSATION_ONLY, LUNA_MODEL, PERSISTENT_WORKSPACE
from mux.profiles.selfhosted import LOOP

PROFILES: Mapping[str, Profile] = {
    p.profile_id: p
    for p in (MANAGED_AGENTS, PERSISTENT_WORKSPACE, CONVERSATION_ONLY, INLINE_REUSE, LOOP)
}


def get_profile(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise InvalidConfig(f"unknown profile {profile_id!r}") from None


def worker_token_limits_supported(
    profile: Profile,
    *,
    max_input_tokens: int | None,
    max_output_tokens: int | None,
    max_turns: int,
) -> bool:
    """Profile-specific envelope shape; capability admission stays with the caller."""
    if profile.profile_id != INLINE_REUSE.profile_id or (
        max_input_tokens is None and max_output_tokens is None
    ):
        return True
    return (
        max_input_tokens is not None
        and max_output_tokens is not None
        and max_turns == 1
        and max_output_tokens <= 65_536
    )


def emits_legacy_events_for(provider: Provider) -> bool:
    """Read rendering policy for an event or resource without a profile id.

    All registered profiles for its provider must agree, so a missing or
    ambiguous declaration cannot silently select another rendering path.
    """
    declarations = {
        profile.emits_legacy_events for profile in PROFILES.values() if profile.provider == provider
    }
    if len(declarations) != 1:
        raise InvalidConfig("events require an unambiguous declared profile rendering policy")
    return declarations.pop()


def neutral_catalog_profile_for(provider: str, *, profile_id: str | None = None) -> Profile | None:
    """Select catalog support, requiring opt-in for explicitly named extra profiles.

    Provider-only callers keep their existing unambiguous resource profile;
    a host with a selected profile must retain that exact profile instead.
    """
    matches = tuple(
        profile
        for profile in PROFILES.values()
        if profile.provider == provider
        and (
            profile.profile_id == profile_id
            if profile_id is not None
            else profile.profile_id
            not in {
                explicit
                for selection in _BACKEND_SELECTIONS.values()
                for explicit in selection.profiles
            }
        )
        and profile.support_for("neutral_catalog") in SUPPORTED
    )
    if len(matches) > 1:
        raise InvalidConfig("provider has ambiguous neutral catalog profiles")
    return matches[0] if matches else None


def represented_execution_tools(
    profile: Profile, tools: tuple[ToolSpec, ...]
) -> tuple[ToolSpec, ...]:
    """Map supported filesystem capabilities through the profile's execution tool.

    MCP remains a separate capability. Unknown tools and permission prompts
    cannot acquire an execution mapping by resembling a known tool name.
    """
    if profile.profile_id not in (INLINE_REUSE.profile_id, PERSISTENT_WORKSPACE.profile_id):
        raise InvalidConfig("profile has no source execution mapping")
    names = {"bash", "read", "edit", "grep", "glob", "write"}
    web_tools = (
        {"web_fetch": "url_context", "web_search": "google_search"}
        if profile.profile_id == INLINE_REUSE.profile_id
        else {"web_search": "web_search"}
    )
    actual = {tool.name for tool in tools}
    if (
        len(tools) != len(actual)
        or not names.issubset(actual)
        or not actual.issubset(names | web_tools.keys())
        or any(tool.kind != "builtin" or tool.permission != "auto" for tool in tools)
    ):
        raise InvalidConfig("source execution tools are not representable")
    return (
        ToolSpec(name=profile.execution_tool, kind="builtin"),
        *(
            ToolSpec(name=target, kind="builtin")
            for source, target in web_tools.items()
            if source in actual
        ),
    )


def native_agent_tools(profile: Profile, spec: AgentSpec) -> tuple[ToolSpec, ...]:
    """Compare native readback while retaining the authored tool intent."""
    if profile.tool_readback == "authored":
        return tuple(spec.tools or ())
    return tuple(
        ToolSpec(
            name=tool.name,
            kind=tool.kind,
            description=(tool.description or None) if tool.kind == "custom" else None,
            input_schema=dict(tool.input_schema or {}) if tool.kind == "custom" else None,
            permission=tool.permission,
        )
        for tool in spec.tools or ()
        if tool.kind != "mcp_toolset"
        and not (tool.kind == "builtin" and tool.name == profile.execution_tool)
    )


def accounting_mode_for(provider: Provider) -> AccountingMode:
    """Read declared accounting policy for a DTO without a profile reference.

    All profiles of that provider must agree. An ambiguous declaration cannot
    choose an arbitrary default and silently change the observation's billing.
    No runtime/provider table, database read or SDK discovery is involved.
    """
    modes = {
        profile.accounting_mode for profile in PROFILES.values() if profile.provider == provider
    }
    if len(modes) != 1:
        raise InvalidConfig("usage requires an unambiguous declared profile accounting mode")
    return modes.pop()


__all__ = [
    "CONVERSATION_ONLY",
    "PROVIDER_ALIASES",
    "INLINE_REUSE",
    "MANAGED_AGENTS",
    "LOOP",
    "PERSISTENT_WORKSPACE",
    "PROFILES",
    "get_profile",
    "worker_token_limits_supported",
    "emits_legacy_events_for",
    "native_agent_tools",
    "neutral_catalog_profile_for",
    "accounting_mode_for",
]


@dataclass(frozen=True)
class BackendSelection:
    config: BackendConfig
    thread_modes: tuple[ThreadMode, ...]
    allowed_models: tuple[str, ...] | None = None
    profiles: tuple[str, ...] = ()
    # Allowed models that only a channel may select, never a fleet worker.
    channel_only_models: tuple[str, ...] = ()
    # Allowed models admitted only while the host holds a current dated tariff.
    tariff_gated_models: tuple[str, ...] = ()
    # Allowed models only a bounded fleet worker runs; an agent or channel never picks them.
    worker_only_models: tuple[str, ...] = ()


#: Everyday names for a provider beyond its id, display name and model family.
PROVIDER_ALIASES: Mapping[str, Provider] = {"chatgpt": "openai", "google": "gemini"}

_BACKEND_SELECTIONS: Mapping[Provider, BackendSelection] = {
    "anthropic": BackendSelection(BackendConfig(), ("per_caller", "shared")),
    "openai": BackendSelection(
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model=LUNA_MODEL),
        ("per_caller",),
        allowed_models=(ASTRA_MODEL,),
        profiles=(CONVERSATION_ONLY.profile_id,),
    ),
    "selfhosted": BackendSelection(
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6.1-sol"),
        ("per_caller",),
    ),
    "gemini": BackendSelection(
        BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"),
        ("per_caller",),
        allowed_models=(WORKER_MODEL, ARGON_MODEL),
        # Argon runs fleet workers only on the bounded token envelope
        # (`FleetOrchestration._bounded_gemini`); the host refuses it unbounded.
        tariff_gated_models=(ARGON_MODEL,),
        # The host runs Flash-Lite only token-capped (`turn.gemini._runtime_model`).
        worker_only_models=(WORKER_MODEL,),
    ),
}
RUNNABLE_PROFILES: frozenset[str] = frozenset(
    profile
    for selection in _BACKEND_SELECTIONS.values()
    for profile in (resolve_default(selection.config).profile, *selection.profiles)
)


def backend_selection(backend: Provider) -> ResolvedBackend:
    """The supported admin selection; provider choices stay in the profile seam."""
    try:
        selection = _BACKEND_SELECTIONS[backend]
    except KeyError:
        raise InvalidConfig("unknown backend selection") from None
    return resolve_default(selection.config)


def selectable_models(backend: Provider) -> tuple[str, ...]:
    """The models an admin may select for an agent on `backend`, its default first.

    Worker-only models (Flash-Lite) are left out: the host runs them only for a
    token-capped fleet worker, never for an agent's or channel's own turn.
    """
    declared = _BACKEND_SELECTIONS[backend]
    default = resolve_default(declared.config).model
    return tuple(
        dict.fromkeys(
            model
            for model in (default, *(declared.allowed_models or ()))
            if model is not None and model not in declared.worker_only_models
        )
    )


def worker_models(backend: Provider) -> tuple[str, ...]:
    """The models a fleet worker on `backend` may run, its default first.

    Worker-only models are included; channel-only models are left out.
    """
    declared = _BACKEND_SELECTIONS[backend]
    default = resolve_default(declared.config).model
    return tuple(
        dict.fromkeys(
            model
            for model in (default, *(declared.allowed_models or ()))
            if model is not None and model not in declared.channel_only_models
        )
    )


def shared_threads_available(backend: Provider) -> bool:
    """Read the same admitted thread modes used by selection and runnable checks."""
    return "shared" in _BACKEND_SELECTIONS[backend].thread_modes


def runnable_selection(selection: ResolvedBackend) -> bool:
    """One release constraint table for profile, model and thread mode."""
    declared = _BACKEND_SELECTIONS[selection.backend]
    resolved = resolve_default(declared.config)
    return (
        selection.profile in (resolved.profile, *declared.profiles)
        and (
            selection.model == resolved.model
            or selection.model in (declared.allowed_models or ())
            or (selection.backend == "selfhosted" and selection.model is not None)
        )
        and selection.thread_mode in declared.thread_modes
    )


def worker_selection_allowed(selection: ResolvedBackend) -> bool:
    """Whether a fleet worker may run this selection; channel-only models refuse."""
    declared = _BACKEND_SELECTIONS[selection.backend]
    return selection.model not in declared.channel_only_models


def tariff_gated(selection: ResolvedBackend) -> bool:
    """Whether admitting this selection requires a current host tariff."""
    declared = _BACKEND_SELECTIONS[selection.backend]
    return selection.model in declared.tariff_gated_models
