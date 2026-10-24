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
    OPENAI_SPEND_LIMIT_CENTS,
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
    profile: str,
    model: str,
    *,
    worker: bool = False,
    budget: Decimal | None = None,
    origin: str = "agent",
    environment: str | None = None,
) -> Any:
    limits = SimpleNamespace(native_budget_usd=budget)
    return SimpleNamespace(
        tenant_id=TENANT,
        admission=SimpleNamespace(
            backend_revision=revision(profile, model),
            backend_revision_origin=origin,
            agent=SimpleNamespace(id="agent_ma_1", version=3),
        ),
        worker=SimpleNamespace(limits=limits) if worker else None,
        native_budget_usd=None,
        openai_environment_id=environment,
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
    assert (plan.hold_usd, plan.cap_usd) == (Decimal("0.25"), Decimal("1.75"))


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
    assert plan.spend_limit_usd_cents == OPENAI_SPEND_LIMIT_CENTS, "Astra is never uncapped"
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


def test_an_openai_agent_turn_waits_for_its_tenants_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the tenant's provisioned hosted environment there is no row; with it, one."""
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(
            request("openai.persistent_workspace", "gpt-6-luna"), OpenAIDeployment
        )

    plan = select_runtime_manifest(
        request("openai.persistent_workspace", "gpt-6-astra", environment="envtmpl_1"),
        OpenAIDeployment,
    )

    assert plan.root_materialization and plan.environment_id == "envtmpl_1"
    assert plan.spend_limit_usd_cents == OPENAI_SPEND_LIMIT_CENTS
    assert plan.project_header is False


def test_a_channel_pinned_to_gemini_runs_from_the_code_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")

    plan = select_runtime_manifest(
        request("gemini.inline_reuse", "gemini-3.8-flash", origin="channel"), GeminiPlan
    )

    assert not plan.root_materialization and not plan.worker_materialization
    assert plan.tools == () and plan.template_capabilities is None


def test_an_operator_row_narrower_than_the_turn_never_widens_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers the operator scoped to one channel stay there: other channels refuse."""
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    worker = default_rows(str(TENANT))[2]
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([{**worker, "platform": "discord", "channel_id": "only-here"}]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))

    with pytest.raises(AdmissionDenied):
        select_runtime_manifest(
            request("gemini.inline_reuse", "gemini-3.8-flash", worker=True), GeminiPlan
        )
    assert (
        manifest_hold_usd(
            revision("gemini.inline_reuse", "gemini-3.8-flash"),
            tenant_id=str(TENANT),
            account_id="any-account",
        )
        is None
    ), "the hold agrees: no row"


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


async def test_default_holds_let_a_root_turn_and_three_gemini_workers_run_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from daimon.core.turn.gemini_state import SQLiteFlashAccounting
    from mux.contracts.ids import ModelRef
    from mux.drivers.gemini.flash import FlashAttempt

    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    flash = "gemini-3.8-flash"
    root = select_runtime_manifest(request("gemini.inline_reuse", flash), GeminiPlan)
    worker = select_runtime_manifest(request("gemini.inline_reuse", flash, worker=True), GeminiPlan)
    # One account's root turns and workers share one ledger (the same project).
    assert root.project == worker.project
    path = tmp_path / "holds.sqlite3"

    def owner(plan: GeminiPlan) -> SQLiteFlashAccounting:
        assert plan.hold_usd is not None and plan.cap_usd is not None
        return SQLiteFlashAccounting(
            path, namespace="account", hold_usd=plan.hold_usd, cap_usd=plan.cap_usd
        )

    async def attempt(plan: GeminiPlan, interaction: str) -> None:
        model = ModelRef(provider="gemini", id=flash)
        accounting = owner(plan)
        await accounting.before_attempt(model=model, attempt=1)
        await accounting.after_attempt(
            receipt=FlashAttempt(model, 1, "accepted", interaction_id=interaction)
        )

    # A Gemini root turn is running while it fans out to three Gemini workers.
    await attempt(root, "root")
    for index in range(3):
        await attempt(worker, f"worker-{index}")
    # A fourth refuses with its plain reason, at once, before any Gemini call.
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
        await attempt(worker, "worker-3")
    # The root's next turn still fits while its workers run, once its own settled.
    assert await owner(root).settle(interaction_id="root", model=flash, actual_usd=Decimal("0.01"))
    await attempt(root, "root-next")
