"""The public surface is declared with `__all__` and each name has one path."""

from __future__ import annotations

import importlib
import pkgutil
from collections import defaultdict
from collections.abc import Iterator
from types import FunctionType, ModuleType

import mux.contracts
import mux.state
import pytest

# Packages and modules that declare a public surface. Anything else under
# `mux` (driver submodules, `_private` modules) is implementation detail until
# it declares `__all__` and is listed here.
PUBLIC_PACKAGES = ("mux.contracts", "mux.state")
PUBLIC_MODULES = (
    "mux.errors",
    "mux.profiles",
    "mux.drivers",
    "mux.drivers.anthropic",
    "mux.drivers.openai",
    "mux.drivers.gemini",
    "mux.drivers.selfhosted",
    # The daimon-harness/0 wire format, shared by the host, harness and conformance kit.
    "mux.drivers.selfhosted.wire",
)

# Intentional second import paths: a name listed here may be reachable from
# exactly these modules and no others.
DUAL_PATHS: dict[str, frozenset[str]] = {
    # `mux.errors` is the stable path for the taxonomy that
    # `mux.contracts.errors` defines.
    "mux.errors": frozenset({"mux.errors", "mux.contracts.errors"}),
    # `RequiredAction` is defined in `events` and re-exported by `actions`,
    # whose docstring documents the second path.
    "mux.contracts.actions": frozenset({"mux.contracts.events", "mux.contracts.actions"}),
}


def public_module_names() -> list[str]:
    names = list(PUBLIC_MODULES)
    for package in (mux.contracts, mux.state):
        names.append(package.__name__)
        names += [
            info.name
            for info in pkgutil.iter_modules(package.__path__, f"{package.__name__}.")
            if not info.name.rsplit(".", 1)[1].startswith("_")
        ]
    return sorted(set(names))


def declared(module_name: str) -> list[str]:
    module = importlib.import_module(module_name)
    assert hasattr(module, "__all__"), f"{module_name} declares no __all__"
    return list(module.__all__)


def identity(module: ModuleType, name: str) -> tuple[str, ...]:
    obj = getattr(module, name)
    if isinstance(obj, (type, FunctionType)):
        return (obj.__module__, obj.__qualname__)
    return (name, str(id(obj)))


def public_names() -> Iterator[tuple[str, str]]:
    for module_name in public_module_names():
        for name in declared(module_name):
            yield module_name, name


def test_public_packages_are_listed() -> None:
    assert all(name in public_module_names() for name in PUBLIC_PACKAGES)
    assert {"mux.contracts.ports", "mux.state.store"} <= set(public_module_names())


@pytest.mark.parametrize("module_name", public_module_names())
def test_all_names_exist_and_are_unique(module_name: str) -> None:
    module = importlib.import_module(module_name)
    names = declared(module_name)
    assert len(names) == len(set(names)), f"{module_name} repeats a name in __all__"
    missing = [name for name in names if not hasattr(module, name)]
    assert not missing, f"{module_name}.__all__ names missing attributes: {missing}"


def test_each_public_name_has_one_public_path() -> None:
    paths: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for module_name, name in public_names():
        module = importlib.import_module(module_name)
        paths[identity(module, name)].add(module_name)
    allowed = {modules for modules in DUAL_PATHS.values()}
    duplicated = {key: mods for key, mods in paths.items() if len(mods) > 1 and mods not in allowed}
    assert not duplicated, f"names reachable from several public modules: {duplicated}"


def test_declared_dual_paths_are_still_dual() -> None:
    for module_names in DUAL_PATHS.values():
        shared = set.intersection(*(set(declared(name)) for name in module_names))
        assert shared, f"{sorted(module_names)} no longer share a public name; drop the entry"
