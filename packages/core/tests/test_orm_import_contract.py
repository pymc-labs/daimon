"""``daimon.core._models`` (the ORM) is private to stores and defaults.

import-linter cannot say "every ``daimon.core`` child except ``_models``": a
wildcard that matches ``_models`` itself makes grimp reject the self-pair. The
rule is checked here instead, on the grimp graph ``lint-imports`` uses, so a
new core module needs no list entry and cannot silently escape the check.
"""

from __future__ import annotations

import grimp

_CORE = "daimon.core"
_MODELS = f"{_CORE}._models"
# Stores are the sanctioned layer between adapters and the ORM, and defaults seeds
# through it; every other path to _models is a violation.
_SANCTIONED = (f"{_CORE}.stores", f"{_CORE}.defaults")


def orm_violations(graph: grimp.ImportGraph) -> list[str]:
    """Modules with an import chain to the ORM that bypasses stores and defaults.

    Removes the sanctioned edges from ``graph``, so pass a graph you own.
    """
    for package in _SANCTIONED:
        if package not in graph.modules:
            continue
        for module in (package, *graph.find_descendants(package)):
            if graph.direct_import_exists(importer=module, imported=_MODELS):
                graph.remove_import(importer=module, imported=_MODELS)
    sources = ["daimon.adapters"] + sorted(
        child
        for child in graph.find_children(_CORE)
        if child != _MODELS and child not in _SANCTIONED
    )
    return [
        source
        for source in sources
        if source in graph.modules and graph.chain_exists(source, _MODELS, as_packages=True)
    ]


def test_no_core_module_or_adapter_reaches_the_orm_outside_stores() -> None:
    violations = orm_violations(grimp.build_graph("daimon"))  # pyright: ignore[reportUnknownMemberType]
    assert not violations, f"import daimon.core._models only from stores and defaults: {violations}"


def test_orm_rule_flags_a_new_core_module_and_allows_stores() -> None:
    graph = grimp.ImportGraph()
    for module in (
        "daimon.adapters",
        "daimon.adapters.web",
        _CORE,
        _MODELS,
        f"{_CORE}.stores",
        f"{_CORE}.stores.agents",
        f"{_CORE}.defaults",
        f"{_CORE}.fresh",
        f"{_CORE}.clean",
    ):
        graph.add_module(module)
    graph.add_import(importer=f"{_CORE}.stores.agents", imported=_MODELS)
    graph.add_import(importer="daimon.adapters.web", imported=f"{_CORE}.stores.agents")
    graph.add_import(importer=f"{_CORE}.clean", imported=f"{_CORE}.stores")
    assert orm_violations(graph) == [], "stores reaching the ORM is the sanctioned path"

    graph.add_import(importer=f"{_CORE}.fresh", imported=_MODELS)
    graph.add_import(importer="daimon.adapters.web", imported=f"{_CORE}.fresh")
    assert orm_violations(graph) == ["daimon.adapters", f"{_CORE}.fresh"], (
        "a new core module importing the ORM, and what imports it, must be flagged"
    )
