"""OpenAI host cases; shared helpers belong to test_catalog_product_context."""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.provider_fixtures import load_provider_fixtures

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .test_anthropic_catalog import CATALOG, INDEX, REPO, replay, selected
from .test_catalog_admin import (
    CHANNEL,
    VALUES,
    require,
)
from .test_catalog_admin import (
    binding as binding,
)
from .test_catalog_product_context import (
    context as context,
)
from .test_catalog_product_context import pytest_plugins as pytest_plugins


@pytest.mark.parametrize("binding", ["openai"], indirect=True)
async def test_opt_in_without_pinned_preparation_never_executes_synthetic_turn(
    tmp_path: Path,
    binding: AdminBindings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context(binding, tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("product opt-in allocated a memory store")

    if binding.backend == "anthropic":
        from . import headless_executor as host

        monkeypatch.setattr(host, "MemoryStateStore", forbidden)
        from daimon.testing.anthropic_fixtures import load_anthropic_fixtures

        plan, recipe = selected(load_anthropic_fixtures(INDEX, source_root=REPO))
        # Bind every actual source slot explicitly, including a setup with no thread.
        ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
        ctx.threads = {
            (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
        }
        result = await execute_cell(
            plan,
            replay(recipe),
            native_recipe=recipe,
            admin=binding,
            admin_values=VALUES,
            product_context=ctx,
        )
    else:
        if binding.backend == "openai":
            from . import openai_catalog_replay as module
        else:
            from . import gemini_catalog_replay as module
        monkeypatch.setattr(module, "MemoryStateStore", forbidden)
        pack = load_provider_fixtures(INDEX.parent.parent / "index.json", catalog_root=CATALOG)
        row, fixture = pack.select("QA-NEW37-MARKDOWN-TABLE-READABLE", binding.backend)
        entry = next(e for e in load_catalog(CATALOG) if e.id == row.scenario_id).model_copy(
            update={"sha256": row.source.sha256}
        )
        plan = plan_scenario(entry, CATALOG, binding.backend, run_id="product-context")
        ctx.channels = {b.ref: CHANNEL for b in plan.bindings}
        ctx.threads = {
            (i.channel or "", i.thread or ""): "323456789012345678" for i in plan.invocations
        }
        result = await (
            execute_openai_cell if binding.backend == "openai" else execute_gemini_cell
        )(
            plan,
            row,
            fixture,
            source_root=CATALOG,
            repository_root=REPO,
            admin=binding,
            admin_values=VALUES,
            product_context=ctx,
        )
    require(result.reported_status == "PENDING", "unbound context acquired full-cell credit")
    require(
        not result.evidence.turns and result.request_count == 0,
        "pending context acquired native evidence",
    )
    require(
        any(g.code == "ADMIN_HOST_CONTEXT_UNBOUND" for g in result.gaps),
        "context barrier disappeared",
    )
    require(not binding.trace.requests, "unbound context called a provider")
