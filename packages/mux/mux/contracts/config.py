"""The channel's backend configuration and its default resolution.

A channel that configures nothing runs on Anthropic Managed Agents with
per-caller threads, exactly as before mux existed. Every other choice is
opt-in: another backend, a non-core profile, or shared threads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import Field, model_validator

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.errors import InvalidConfig
from mux.contracts.ids import ChannelRef, Provider
from mux.contracts.profile import Capability

ThreadMode = Literal["per_caller", "shared"]
"""`per_caller` (the default) keeps one thread per caller; `shared` is opt-in."""

DEFAULT_BACKEND: Provider = "anthropic"

DEFAULT_PROFILES: Mapping[Provider, str] = {
    "anthropic": "anthropic.managed_agents",
    "openai": "openai.persistent_workspace",
}
"""The core profile a backend gets when the config names none.

Gemini has no entry: its only profile is non-core, so a channel must name
`gemini.inline_reuse` to use it.
"""


class CapabilityRequirement(Contract):
    """How much a channel needs one capability.

    `optional` must declare the fallback the host will use instead, so a
    missing feature degrades the way the channel said it may, visibly.
    """

    level: Literal["required", "optional"]
    fallback: str | None = None

    @model_validator(mode="after")
    def _fallback_matches_level(self) -> CapabilityRequirement:
        if self.level == "optional" and not self.fallback:
            raise ValueError("an optional capability needs a declared fallback")
        if self.level == "required" and self.fallback is not None:
            raise ValueError("a required capability has no fallback")
        return self


class BackendConfig(Contract):
    """What a channel stores. Every field may be unset; unset means the default."""

    backend: Provider | None = None
    profile: str | None = None
    model: str | None = None
    requires: FrozenMap[Capability, CapabilityRequirement] = Field(
        default_factory=dict[Capability, CapabilityRequirement]
    )
    thread_mode: ThreadMode = "per_caller"


class ResolvedBackend(Contract):
    """A channel configuration with every default filled in.

    `model` is `None` only on the default backend, where the agent's own
    model applies as it always has.
    """

    backend: Provider
    profile: str
    model: str | None
    requires: FrozenMap[Capability, CapabilityRequirement] = Field(
        default_factory=dict[Capability, CapabilityRequirement]
    )
    thread_mode: ThreadMode = "per_caller"

    @model_validator(mode="after")
    def _consistent(self) -> ResolvedBackend:
        check_selection(self.backend, self.profile, self.model)
        return self

    def content_digest(self) -> str:
        """A content digest: equal configurations hash equal."""
        body = self.model_dump(mode="json", include=set(ResolvedBackend.model_fields))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class ConfigRevision(ResolvedBackend):
    """One immutable revision of a channel's resolved configuration."""

    channel: ChannelRef
    local: int = Field(ge=0)
    digest: str

    @model_validator(mode="after")
    def _digest_matches(self) -> ConfigRevision:
        if self.digest != self.content_digest():
            raise ValueError("config revision digest does not match its content")
        return self

    @classmethod
    def create(cls, channel: ChannelRef, local: int, resolved: ResolvedBackend) -> ConfigRevision:
        return cls(
            channel=channel,
            local=local,
            digest=resolved.content_digest(),
            **resolved.model_dump(include=set(ResolvedBackend.model_fields)),
        )


def check_selection(backend: Provider, profile: str, model: str | None) -> None:
    """Raise `InvalidConfig` unless backend, profile and model agree.

    The profile must belong to the backend, and a model, when named, must
    not be blank. Only the default backend may leave the model unset.
    """
    if not profile.startswith(f"{backend}."):
        raise InvalidConfig(f"profile {profile!r} does not belong to backend {backend!r}")
    if model is not None and not model.strip():
        raise InvalidConfig("model must not be blank")
    if backend != DEFAULT_BACKEND and model is None:
        raise InvalidConfig(f"backend {backend!r} needs an explicit model")


def resolve_default(config: BackendConfig | None) -> ResolvedBackend:
    """Fill in defaults. Pure: no I/O, no clock.

    No configuration at all (a legacy channel) resolves to Anthropic Managed
    Agents with per-caller threads. A non-default backend must name its model.
    """
    config = config or BackendConfig()
    backend = config.backend
    if backend is None:
        if config.profile is not None and not config.profile.startswith(f"{DEFAULT_BACKEND}."):
            raise InvalidConfig(f"profile {config.profile!r} needs its backend set")
        backend = DEFAULT_BACKEND
    profile = config.profile or DEFAULT_PROFILES.get(backend)
    if profile is None:
        raise InvalidConfig(f"backend {backend!r} has no default profile; name one explicitly")
    check_selection(backend, profile, config.model)
    from mux.profiles import PROFILES  # deferred: profiles are built from these contracts

    if profile not in PROFILES:
        raise InvalidConfig(f"unknown profile {profile!r}")
    return ResolvedBackend(
        backend=backend,
        profile=profile,
        model=config.model,
        requires=config.requires,
        thread_mode=config.thread_mode,
    )
