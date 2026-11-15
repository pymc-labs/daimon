"""Losses finer than a whole capability, and what a provider's default sandbox keeps.

A switch names the part of a capability it loses when it can ("its network rules"),
and falls back to the whole one ("its sandbox") only when no finer word fits. A
consent that accepted the whole capability still covers every part of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mux.contracts.resources import NetworkPolicy

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


def sandbox_losses(environment: EnvironmentSpec | None) -> tuple[str, ...]:
    """What a provider's default sandbox leaves out of `environment`, as drop names.

    Empty only for a plain default environment (hosted, no packages, no workspace
    sources, default network): the sandbox then keeps everything the source had. Each
    other setting is its own loss; `("sandbox",)` when the environment is one no finer
    word describes (self-hosted, none, or a native config this can't read).
    """
    if environment is None:
        return ()
    if environment.execution != "hosted" or environment.native_config is not None:
        return ("sandbox",)
    lost: list[str] = []
    if any(environment.packages.values() if environment.packages else ()):
        lost.append("installed_packages")
    if environment.network is not None and environment.network != NetworkPolicy():
        lost.append("network_rules")
    if environment.sources:
        lost.append("workspace_files")
    return tuple(sorted(lost))
