"""OpenAI host cases; shared helpers belong to test_catalog_admin."""

from __future__ import annotations

import pytest
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.anthropic_fixtures import load_anthropic_fixtures
from daimon.testing.provider_fixtures import load_provider_fixtures

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import (
    execute_cell,
    execute_gemini_cell,
    execute_openai_cell,
)
from .test_anthropic_catalog import INDEX, REPO, replay, selected
from .test_catalog_admin import (
    CATALOG as CATALOG,
)
from .test_catalog_admin import (
    VALUES as VALUES,
)
from .test_catalog_admin import (
    binding as binding,
)
from .test_catalog_admin import pytest_plugins as pytest_plugins
from .test_catalog_admin import (
    require as require,
)


@pytest.mark.parametrize("binding", ["openai"], indirect=True)
@pytest.mark.parametrize(
    "scenario_id",
    ["QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", "QA-D7-BIG-WORKSPACE-CHECKPOINT"],
)
async def test_native_skill_pair_reaches_public_admin_and_stops_at_missing_edge(
    binding: AdminBindings, scenario_id: str
) -> None:
    values = {**VALUES, "env.qa_library_skill": "qa-library"}
    if binding.backend == "anthropic":
        pack = load_anthropic_fixtures(INDEX, source_root=REPO)
        plan, recipe = selected(pack, scenario_id)
        result = await execute_cell(
            plan, replay(recipe), native_recipe=recipe, admin=binding, admin_values=values
        )
    else:
        providers = load_provider_fixtures(CATALOG.parent / "index.json", catalog_root=CATALOG)
        scenario, fixture = providers.select(scenario_id, binding.backend)
        entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id).model_copy(
            update={"sha256": scenario.source.sha256}
        )
        plan = plan_scenario(entry, CATALOG, binding.backend, run_id="admin-native")
        result = await (
            execute_openai_cell if binding.backend == "openai" else execute_gemini_cell
        )(
            plan,
            scenario,
            fixture,
            source_root=CATALOG,
            repository_root=REPO,
            admin=binding,
            admin_values=values,
        )
    admin_receipts = [r for r in result.step_receipts if r.kind == "admin"]
    require(len(admin_receipts) == 2, "original add/remove steps were dropped")
    first = admin_receipts[0]
    require(first.code == "ADMIN_RESOURCE_EDGE_UNBOUND", "public edge was not consulted")
    require(
        first.admin is not None and first.admin.backend == binding.backend, "public receipt lost"
    )
    require(admin_receipts[1].code == "DEPENDENCY_BLOCKED", "cleanup invented a predecessor")
    require(result.reported_status == "PENDING", "missing skill edge conferred cell credit")
    require(
        result.request_count == (6 if binding.backend == "openai" else 3),
        "later turn escaped admin barrier",
    )
    require([t.turn for t in result.evidence.turns] == [1], "post-admin turn executed")
    require(not binding.trace.requests, "unbound skill edge called a provider")
