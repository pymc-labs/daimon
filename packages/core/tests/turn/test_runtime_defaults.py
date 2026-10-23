"""A never-configured tenant gets Gemini agent turns and Gemini/OpenAI workers from code."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.runtime_defaults import (
    DEFAULT_PROJECT,
    OPENAI_WORKER_SPEND_LIMIT_CENTS,
    default_rows,
)
from daimon.core.turn.runtime_manifest import manifest_hold_usd, select_runtime_manifest

TENANT = uuid.uuid4()
OTHER = uuid.uuid4()


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    for env in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)


def revision(profile: str, model: str) -> Any:
    return SimpleNamespace(
        backend=profile.partition(".")[0],
        profile=profile,
        model=model,
        thread_mode="per_caller",
        requires={},
        digest="unused",
        channel=SimpleNamespace(platform="discord", channel_id="never-set-up"),
    )


def request(
    profile: str, model: str, *, worker: bool = False, budget: Decimal | None = None
) -> Any:
    limits = SimpleNamespace(native_budget_usd=budget)
    return SimpleNamespace(
        tenant_id=TENANT,
        admission=SimpleNamespace(
            backend_revision=revision(profile, model),
            backend_revision_origin="agent",
            agent=SimpleNamespace(id="agent_ma_1", version=3),
        ),
        worker=SimpleNamespace(limits=limits) if worker else None,
        scope=SimpleNamespace(tenant_id=str(TENANT), account_id="any-account"),
    )


def test_an_unkeyed_deployment_has_no_defaults_and_refuses() -> None:
    assert default_rows(str(TENANT)) == []
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(request("gemini.inline_reuse", "gemini-3.8-flash"), GeminiPlan)


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-4-argon"])
def test_a_gemini_agent_turn_runs_from_the_code_template(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")

    plan = select_runtime_manifest(request("gemini.inline_reuse", model), GeminiPlan)

    assert plan.root_materialization and not plan.worker_materialization
    assert plan.tenant_id == str(TENANT) and plan.channel_id == "never-set-up"
    assert plan.template_capabilities == "mcp", "the agent keeps its Daimon tools"
    assert (plan.hold_usd, plan.cap_usd) == (Decimal("0.25"), Decimal("0.50"))


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-4-argon"])
def test_a_gemini_worker_runs_from_the_code_template(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")

    plan = select_runtime_manifest(request("gemini.inline_reuse", model, worker=True), GeminiPlan)

    assert plan.worker_materialization and not plan.root_materialization
    assert manifest_hold_usd(
        revision("gemini.inline_reuse", model), tenant_id=str(TENANT), account_id="any-account"
    ) == Decimal("0.49"), "the hold agrees with the row selection picks"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-astra"])
def test_an_openai_worker_is_capped_and_needs_no_project(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")

    plan = select_runtime_manifest(
        request("openai.conversation_only", model, worker=True), OpenAIDeployment
    )

    assert plan.worker_materialization
    assert plan.spend_limit_usd_cents == OPENAI_WORKER_SPEND_LIMIT_CENTS, "Astra is never uncapped"
    assert plan.project == DEFAULT_PROJECT and plan.project_header is False


def test_a_budgeted_openai_worker_is_capped_by_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")

    plan = select_runtime_manifest(
        request("openai.conversation_only", "gpt-6-astra", worker=True, budget=Decimal("0.20")),
        OpenAIDeployment,
    )

    assert plan.spend_limit_usd_cents is None, "the worker's native budget sets the cap"


def test_an_openai_agent_turn_has_no_code_default_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its own turn needs a hosted environment; a tenant gets one from its own row."""
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(
            request("openai.persistent_workspace", "gpt-6-luna"), OpenAIDeployment
        )


def test_a_pro_model_is_never_in_a_default_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    grants = [
        cast(dict[str, list[str]], row["configuration_grant"]) for row in default_rows(str(TENANT))
    ]
    models = {model for grant in grants for model in grant["models"]}
    assert not any("pro" in model for model in models)


def test_an_operator_row_always_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    own = {
        **default_rows(str(TENANT))[0],
        "project": "operator-project",
        "hold_usd": "0.10",
        "cap_usd": "0.20",
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([own]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))

    plan = select_runtime_manifest(request("gemini.inline_reuse", "gemini-3.8-flash"), GeminiPlan)

    assert plan.project == "operator-project" and plan.cap_usd == Decimal("0.20")


def test_an_unreadable_operator_file_refuses_instead_of_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    path = tmp_path / "runtime.json"
    path.write_text("not json")
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(request("gemini.inline_reuse", "gemini-3.8-flash"), GeminiPlan)


def test_one_tenants_row_never_serves_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code default is per tenant; another tenant's row neither blocks nor serves it."""
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    theirs = {**default_rows(str(OTHER))[0], "project": "other-tenant"}
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([theirs]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))

    plan = select_runtime_manifest(request("gemini.inline_reuse", "gemini-3.8-flash"), GeminiPlan)

    assert plan.tenant_id == str(TENANT) and plan.project == DEFAULT_PROJECT


def test_an_unconfigured_provider_is_refused_with_its_real_cause() -> None:
    with pytest.raises(AdmissionDenied) as refused:
        select_runtime_manifest(
            request("openai.conversation_only", "gpt-6-luna", worker=True), OpenAIDeployment
        )
    assert (refused.value.cause, refused.value.provider) == ("provider_not_configured", "openai")


def test_the_refusal_copy_names_no_setting() -> None:
    from daimon.core.turn.notices import RefusalNouns, admission_refusal_text

    text = admission_refusal_text(
        "backend_unsupported",
        RefusalNouns(scope="server", admin="a server admin", billing="`/billing`"),
        cause="provider_not_configured",
        provider="openai",
    )
    assert "OpenAI isn't set up on this Daimon install yet" in text
    assert "DAIMON_" not in text
