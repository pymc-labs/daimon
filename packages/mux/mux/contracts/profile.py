"""Capabilities and profiles: what a backend can do, declared up front.

A profile is one way of running a provider (Anthropic Managed Agents, an
OpenAI hosted workspace, ...). It declares a `Support` level for each
capability. Anything it does not declare is `unknown`, and admission treats
`unknown` exactly like `unsupported`: no evidence, no promise.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.errors import ExtensionVersionError, UnsupportedCapability
from mux.contracts.extensions import ExtensionRef
from mux.contracts.ids import Provider

Support = Literal["native", "emulated", "unsupported", "unknown"]

SUPPORTED: frozenset[Support] = frozenset({"native", "emulated"})
"""The support levels admission accepts."""

Capability = Literal[
    # Core: a profile missing any of these is not core-conforming.
    "thread_workspace_persistence",
    "turn_lifecycle",
    "cancel",
    "tool_loop",
    "required_actions",
    "skills_bundle",
    "artifacts",
    "usage_observations",
    "reconcile",
    # Negotiable: a channel may require or optionally want these.
    "steer",
    "tool_confirmation",
    "native_event_replay",
    "event_previews",
    "vaults",
    "memory_stores",
    "session_resources",
    "skills_versions",
    "multiagent",
    "environments_fork",
    "native_schedules",
    "model_request_usage",
    "model_request_limit",
    "input_token_limit",
    "output_token_limit",
    "session_spend_control",
    "workspace_export_import",
    "neutral_catalog",
    "agent_delete",
]

CORE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        "thread_workspace_persistence",
        "turn_lifecycle",
        "cancel",
        "tool_loop",
        "required_actions",
        "skills_bundle",
        "artifacts",
        "usage_observations",
        "reconcile",
    }
)


class AccountingMode(StrEnum):
    """The host settlement protocol for a profile's measured observations.

    Native root totals describe already decoded disjoint child usage, not
    permission to provision delegation or a claim that a roster port exists.
    Native requests settle incremental model-request observations through the
    same native protocol; `usage_grain` is the grain a driver must emit.
    """

    REQUEST_SPANS = "request_spans"
    NATIVE_TOTALS = "native_totals"
    NATIVE_REQUESTS = "native_requests"
    NATIVE_ROOT_TOTALS = "native_root_totals"

    @property
    def settles_natively(self) -> bool:
        return self is not AccountingMode.REQUEST_SPANS

    @property
    def supports_root_usage(self) -> bool:
        return self is AccountingMode.NATIVE_ROOT_TOTALS

    @property
    def usage_grain(self) -> Literal["model_request", "turn"]:
        """The grain of the observations this mode settles."""
        if self in (AccountingMode.REQUEST_SPANS, AccountingMode.NATIVE_REQUESTS):
            return "model_request"
        return "turn"


class Profile(Contract):
    """One provider profile and what it supports.

    `core` is declared, and checked: a profile may only call itself core
    when it supports every core capability. A non-core profile is admitted
    only for a channel whose configuration names it explicitly.
    """

    provider: Provider
    profile_id: str
    schema_version: str
    sdk_pin: str
    core: bool
    support: FrozenMap[Capability, Support]
    accounting_mode: AccountingMode = AccountingMode.REQUEST_SPANS
    extensions: tuple[ExtensionRef, ...] = ()
    execution_tool: Literal["bash", "code_execution"] = "code_execution"
    implicit_execution: bool = False
    mcp_policy_keys: tuple[Literal["allowed_tools", "required"], ...] = ()
    skill_digest: Literal["version", "content"] | None = None
    tool_readback: Literal["authored", "compiled"] = "authored"
    catalog_readback: Literal["retrieve", "verify"] = "retrieve"
    emits_legacy_events: bool = False
    """The host receives SDK events alongside normalized events for rendering."""
    meters_cache_writes: bool = True
    """The usage meter counts cache-write input. False where the provider bills cache
    writes but its usage has no such count: the host then prices none, an estimate."""

    @model_validator(mode="after")
    def _check(self) -> Profile:
        if not self.profile_id.startswith(f"{self.provider}."):
            raise ValueError(f"profile id {self.profile_id!r} must start with {self.provider!r}.")
        if self.core:
            gaps = sorted(
                cap for cap in CORE_CAPABILITIES if self.support_for(cap) not in SUPPORTED
            )
            if gaps:
                raise ValueError(f"core profile {self.profile_id!r} lacks {', '.join(gaps)}")
        for ext in self.extensions:
            if not ext.namespace.startswith(f"{self.provider}."):
                raise ValueError(f"extension {ext.namespace!r} is not a {self.provider} namespace")
        return self

    def support_for(self, capability: Capability) -> Support:
        return self.support.get(capability, "unknown")

    @property
    def execution_tools(self) -> frozenset[str]:
        """Builtin workspace actions removed when no sandbox is delegated."""
        return frozenset(
            (
                self.execution_tool,
                "bash",
                "code_execution",
                "computer",
                "apply_patch",
                "edit",
                "read",
                "write",
                "glob",
                "grep",
            )
        )

    def missing_core(self) -> tuple[Capability, ...]:
        """Core capabilities this profile does not support, sorted."""
        return tuple(sorted(c for c in CORE_CAPABILITIES if self.support_for(c) not in SUPPORTED))

    def offered_extension(self, namespace: str, version: int) -> ExtensionRef:
        """The offered extension, or the error that says why it is not.

        A namespace the profile does not offer at all is an unsupported
        capability; an offered namespace at another version is a version
        error, so the caller can tell "never" from "not this one".
        """
        versions = tuple(sorted(e.version for e in self.extensions if e.namespace == namespace))
        if not versions:
            raise UnsupportedCapability((namespace,), self.profile_id)
        if version not in versions:
            raise ExtensionVersionError(namespace, version, versions)
        return ExtensionRef(namespace=namespace, version=version)


__all__ = [
    "AccountingMode",
    "CORE_CAPABILITIES",
    "Capability",
    "Profile",
    "SUPPORTED",
    "Support",
]
