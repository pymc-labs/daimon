"""Admission: can this profile run this channel's configuration?

`admit` is pure and runs before any provider mutation. It refuses with
`UnsupportedCapability` listing every unmet requirement, and otherwise
returns an `Admission` that says what was emulated, which optional features
fell back, and which core capabilities an explicitly chosen non-core profile
goes without, so the host can surface each of them.
"""

from __future__ import annotations

from mux.contracts._base import Contract
from mux.contracts.config import ConfigRevision, ThreadMode, check_selection
from mux.contracts.errors import InvalidConfig, UnsupportedCapability
from mux.contracts.ids import Provider
from mux.contracts.profile import CORE_CAPABILITIES, SUPPORTED, Capability, Profile, Support

UNWAIVABLE: frozenset[Capability] = frozenset({"usage_observations"})
"""Required on every profile, core or not: a turn that cannot be metered is refused."""


class FallbackApplied(Contract):
    capability: Capability
    support: Support
    fallback: str


class Admission(Contract):
    provider: Provider
    profile_id: str
    model: str | None
    thread_mode: ThreadMode
    config_local: int
    config_digest: str
    satisfied: tuple[Capability, ...]
    emulated: tuple[Capability, ...] = ()
    fallbacks: tuple[FallbackApplied, ...] = ()
    waived_core: tuple[Capability, ...] = ()


def admit(config: ConfigRevision, profile: Profile) -> Admission:
    """Admit `config` on `profile`, or raise.

    Core capabilities are mandatory on a core profile. A non-core profile is
    only ever selected by naming it in the config, which waives the core
    capabilities it lacks, except `usage_observations`, which no profile may
    go without; any the config lists as required still refuse.
    `unknown` support counts as unsupported.
    """
    if profile.profile_id != config.profile or profile.provider != config.backend:
        raise InvalidConfig(
            f"config selects {config.backend}/{config.profile}, not {profile.profile_id!r}"
        )
    check_selection(config.backend, config.profile, config.model)
    if config.digest != config.content_digest():
        raise InvalidConfig("config revision digest does not match its content")

    required: set[Capability] = {
        cap for cap, req in config.requires.items() if req.level == "required"
    }
    required |= UNWAIVABLE
    waived: tuple[Capability, ...] = ()
    if profile.core:
        required |= CORE_CAPABILITIES
    else:
        waived = tuple(c for c in profile.missing_core() if c not in required)

    missing = sorted(c for c in required if profile.support_for(c) not in SUPPORTED)
    if missing:
        raise UnsupportedCapability(tuple(missing), profile.profile_id)

    fallbacks = tuple(
        FallbackApplied(capability=cap, support=profile.support_for(cap), fallback=req.fallback)
        for cap, req in sorted(config.requires.items(), key=lambda item: item[0])
        if req.level == "optional"
        and req.fallback is not None
        and profile.support_for(cap) not in SUPPORTED
    )
    optional: set[Capability] = {
        cap for cap, req in config.requires.items() if req.level == "optional"
    }
    wanted = required | optional
    ordered: list[Capability] = sorted(wanted)
    satisfied: tuple[Capability, ...] = tuple(
        c for c in ordered if profile.support_for(c) in SUPPORTED
    )
    emulated: tuple[Capability, ...] = tuple(
        c for c in satisfied if profile.support_for(c) == "emulated"
    )
    return Admission(
        provider=profile.provider,
        profile_id=profile.profile_id,
        model=config.model,
        thread_mode=config.thread_mode,
        config_local=config.local,
        config_digest=config.digest,
        satisfied=satisfied,
        emulated=emulated,
        fallbacks=fallbacks,
        waived_core=waived,
    )
