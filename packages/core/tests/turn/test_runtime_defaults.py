"""A never-configured tenant gets Gemini and OpenAI agent turns from code."""

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
from daimon.core.turn.runtime_manifest import select_runtime_manifest

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
    origin: str = "agent",
    environment: str | None = None,
) -> Any:
    return SimpleNamespace(
        tenant_id=TENANT,
        admission=SimpleNamespace(
            backend_revision=revision(profile, model),
            backend_revision_origin=origin,
            agent=SimpleNamespace(id="agent_ma_1", version=3),
        ),
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

    assert plan.root_materialization
    assert plan.tenant_id == str(TENANT) and plan.channel_id == "never-set-up"
    assert plan.template_capabilities == "mcp", "the agent keeps its Daimon tools"
    assert (plan.hold_usd, plan.cap_usd) == (Decimal("0.25"), Decimal("1.75"))


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
    assert plan.template_capabilities == "mcp", "the agent keeps its Daimon tools (J6)"


def test_capture_sees_both_agent_templates_before_any_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source capture reads template capabilities without needing a provider key.

    Capture runs in the MCP server, which may not carry the key the chat host has;
    its preview must still see the template that keeps MCP (smoke 0eee1b8e J6).
    """
    from daimon.core.turn.runtime_defaults import capture_template_rows, default_rows

    for name in ("DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"):
        monkeypatch.delenv(name, raising=False)
    rows = capture_template_rows(str(TENANT))
    assert {row["profile"] for row in rows} == {
        "gemini.inline_reuse",
        "openai.persistent_workspace",
    }, "an unkeyed capture process still sees both agent templates"
    assert default_rows(str(TENANT)) == [], "a turn still refuses without a key"
    assert all(row["root_materialization"] is True for row in rows)
    assert all(row["template_capabilities"] == "mcp" for row in rows)
    assert all(row["tenant_id"] == str(TENANT) for row in rows)
    assert all("environment_id" not in row for row in rows)


@pytest.mark.parametrize(
    "changes",
    [
        {"root_materialization": False, "agent_id": "agent_1"},
        {"profile": "openai.conversation_only", "environment_id": None},
        {"mcp_credential_file": "/tmp/credential"},
    ],
)
def test_template_capabilities_belong_to_a_persistent_workspace_template(changes) -> None:
    from pydantic import ValidationError

    from .test_openai_agent_template import template_row

    OpenAIDeployment.model_validate(template_row("t", template_capabilities="mcp"))
    with pytest.raises(ValidationError):
        OpenAIDeployment.model_validate(template_row("t", template_capabilities="mcp", **changes))


def test_a_channel_pinned_to_gemini_runs_from_the_code_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")

    plan = select_runtime_manifest(
        request("gemini.inline_reuse", "gemini-3.8-flash", origin="channel"), GeminiPlan
    )

    assert not plan.root_materialization
    assert plan.tools == () and plan.template_capabilities is None


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
