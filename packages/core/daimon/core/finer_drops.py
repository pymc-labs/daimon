"""Losses finer than a whole capability, and what a provider's sandbox keeps.

A switch names the part of a capability it loses when it can ("its network rules"),
and falls back to the whole one ("its sandbox") only when no finer word fits. A
consent that accepted the whole capability still covers every part of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mux.contracts.resources import NetworkPolicy
from mux.profiles import carried_environment, get_profile, network_effect

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mux.contracts.resources import EnvironmentSpec

#: Each finer drop, with the whole capability it is a part of.
FINER_DROPS: Final[Mapping[str, str]] = {
    "installed_packages": "sandbox",
    "network_rules": "sandbox",
    "workspace_files": "sandbox",
    "effort_setting": "model_controls",
}


def finer_parts(whole: str, drops: frozenset[str] | set[str]) -> frozenset[str]:
    """The parts of `whole` that `drops` names."""
    return frozenset(d for d in drops if FINER_DROPS.get(d) == whole)


def kept_environment(profile_id: str, environment: EnvironmentSpec) -> EnvironmentSpec | None:
    """What `profile_id`'s sandbox recreates of `environment`; None for its default one."""
    kept = carried_environment(get_profile(profile_id), environment)
    if not kept.packages and network_effect(kept.network) == NetworkPolicy():
        return None
    return kept


def sandbox_losses(
    environment: EnvironmentSpec | None, kept: EnvironmentSpec | None = None
) -> tuple[str, ...]:
    """What a provider's sandbox, set up as `kept` (None: its default), leaves out of `environment`.

    Each setting `kept` lacks is its own loss (packages, network, workspace files);
    `("sandbox",)` when the environment is one no finer word describes (self-hosted,
    none, or a native config this can't read). Empty when there is no environment.
    """
    if environment is None:
        return ()
    if environment.execution != "hosted" or environment.native_config is not None:
        return ("sandbox",)
    lost: list[str] = []
    if _packages(environment) != _packages(kept):
        lost.append("installed_packages")
    if network_effect(environment.network) != network_effect(kept.network if kept else None):
        lost.append("network_rules")
    if (environment.sources or ()) != ((kept.sources or ()) if kept else ()):
        lost.append("workspace_files")
    return tuple(sorted(lost))


def _packages(environment: EnvironmentSpec | None) -> dict[str, tuple[str, ...]]:
    packages = environment.packages if environment is not None else None
    return {manager: names for manager, names in (packages or {}).items() if names}
