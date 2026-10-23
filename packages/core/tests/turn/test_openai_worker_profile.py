"""OpenAI fleet workers run conversation-only, the profile the code-default worker row serves."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.fleet_configuration import default_fleet_grant
from daimon.core.neutral_resources import agent_worker_backend
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.stores.domain import TenantBackendPolicy
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.runtime_defaults import default_rows
from daimon.core.turn.runtime_manifest import select_runtime_manifest
from mux.profiles import CONVERSATION_ONLY, as_worker, backend_selection, worker_selection

TENANT = uuid.uuid4()


@pytest.fixture(autouse=True)
def fresh_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """No operator rows; only the deployment's OpenAI key."""
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    for env in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "set")


def test_an_agent_keeps_its_workspace_but_its_workers_are_conversation_only() -> None:
    assert backend_selection("openai").profile == "openai.persistent_workspace"
    assert worker_selection("openai").profile == CONVERSATION_ONLY.profile_id
    assert as_worker(backend_selection("openai")) == worker_selection("openai")
    for provider in ("anthropic", "gemini"):
        assert worker_selection(provider) == backend_selection(provider), "unchanged"


def test_the_default_fleet_grants_openai_workers_conversation_only() -> None:
    grant = default_fleet_grant(
        TenantBackendPolicy(
            tenant_id=TENANT, revision="r", enabled_backends=("anthropic", "openai", "gemini")
        )
    )
    assert grant is not None
    openai = [backend for backend, model in grant.choices if model.provider == "openai"]
    assert openai and {backend.profile for backend in openai} == {CONVERSATION_ONLY.profile_id}


def test_an_openai_agent_worker_runs_conversation_only() -> None:
    selection, model = agent_worker_backend(cast(Any, None), "openai", "gpt-6-astra")
    assert selection.profile == CONVERSATION_ONLY.profile_id
    assert (selection.model, model.id) == ("gpt-6-astra", "gpt-6-astra")


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-astra"])
def test_a_fresh_tenants_openai_task_worker_finds_the_code_default_row(model: str) -> None:
    selection = worker_selection("openai").model_copy(update={"model": model})
    revision = SimpleNamespace(
        backend="openai",
        profile=selection.profile,
        model=model,
        thread_mode=selection.thread_mode,
        requires={},
        digest="unused",
        channel=SimpleNamespace(platform="discord", channel_id="never-set-up"),
    )
    request = SimpleNamespace(
        tenant_id=TENANT,
        admission=SimpleNamespace(
            backend_revision=revision,
            backend_revision_origin="agent",
            agent=SimpleNamespace(id="agent_ma_1", version=1),
        ),
        worker=SimpleNamespace(limits=SimpleNamespace(native_budget_usd=None)),
        native_budget_usd=None,
        openai_environment_id=None,
        scope=SimpleNamespace(tenant_id=str(TENANT), account_id="any"),
    )

    plan = select_runtime_manifest(cast(Any, request), OpenAIDeployment)

    assert plan.worker_materialization and plan.profile == CONVERSATION_ONLY.profile_id
    assert plan.spend_limit_usd_cents is not None, "Astra never runs uncapped"


def test_every_default_worker_row_serves_its_providers_worker_profile() -> None:
    rows = [row for row in default_rows(str(TENANT)) if row.get("worker_materialization")]
    assert {row["profile"] for row in rows} == {worker_selection("openai").profile}


@pytest.mark.parametrize("model", [None, "gpt-6-astra"], ids=["same-model", "astra"])
def test_a_gpt_root_spawns_gpt_task_workers_without_its_workspace(model: str | None) -> None:
    """J1 then J3: a root switched to GPT keeps its workspace; its GPT workers can't carry it."""
    from dataclasses import replace

    from daimon.core.neutral_resources import WorkerLimits, resolve_worker_spec, task_worker_spec
    from mux.contracts.ids import ModelRef

    from ..test_fleet_orchestration import NOW, POLICY, permissions

    root = permissions("openai")
    assert root.backend_grants[0].profile == "openai.persistent_workspace", "the root's own"
    models = (root.spec.model, ModelRef(provider="openai", id="gpt-6-astra"))
    # The default fleet grant: each OpenAI model on the worker profile.
    grants = tuple(worker_selection("openai").model_copy(update={"model": m.id}) for m in models)
    parent = replace(root, backend_grants=grants, models=models)

    spec = task_worker_spec(parent, role="researcher", task="Answer X.", model=model)
    assert {"mcp", "memory", "sandbox", "skills", "tools"} <= set(spec.declared_drops)

    resolved = resolve_worker_spec(
        parent,
        spec,
        policy=POLICY,
        now=NOW,
        limits=WorkerLimits(
            budget_usd=parent.limits.budget_usd / 2,
            deadline=parent.limits.deadline,
            max_turns=1,
        ),
    )
    assert resolved.backend.profile == CONVERSATION_ONLY.profile_id
    assert resolved.environment is None and resolved.resources == ()
