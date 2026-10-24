"""Actual configuration edges refuse reserved references before credential/client I/O."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from unittest.mock import Mock

import pytest
from daimon.core.config import load_settings, load_turn_settings
from daimon.core.turn.gemini_deployment import GeminiPlan
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.runtime_manifest import (
    ApiKeyEnvironment,
    ProviderRuntimeConfigurationError,
    validate_configured_runtime_manifest,
)
from mux.contracts.ids import Provider
from pydantic import TypeAdapter, ValidationError

PREFIXES = ("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_")


def settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://offline/unused")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "offline-settings-required-value")
    monkeypatch.setenv("DAIMON_TURN__PATH", "mux")
    monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", "true")


@pytest.mark.parametrize("loader", [load_settings, load_turn_settings])
@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("case", ["upper", "lower", "mixed"])
def test_actual_settings_load_refuses_reserved_reference_without_secret_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loader, prefix: str, case: str
) -> None:
    settings_env(monkeypatch)
    prefix = prefix if case == "upper" else prefix.lower() if case == "lower" else prefix.title()
    reference = prefix + "CHANNEL_KEY"
    path = tmp_path / "manifest.json"
    # This checks reference policy only. Provider plan/scope validation stays at admission.
    path.write_text(json.dumps([{"api_key_env": reference}]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    original = os.environ.get

    def get(name, default=None):
        if name == reference:
            raise AssertionError("configuration read a credential value")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", get)
    with pytest.raises(ProviderRuntimeConfigurationError) as refused:
        loader(_env_file=None)
    assert refused.value.reason == "reserved_key_reference"
    assert "DAIMON_CHANNEL_OPENAI_KEY" in str(refused.value)
    assert reference not in str(refused.value)


def plan(provider: str, key: str, tmp_path: Path) -> dict[str, object]:
    common: dict[str, object] = {
        "profile": "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse",
        "tenant_id": "tenant",
        "platform": "slack",
        "channel_id": "channel",
        "account_id": "account",
        "config_digest": "digest",
        "project": "project",
        "api_key_env": key,
    }
    if provider == "openai":
        return {**common, "agent_id": "agent", "environment_id": "environment"}
    return {
        **common,
        "records_path": tmp_path / "records.sqlite",
        "holds_path": tmp_path / "holds.sqlite",
        "hold_usd": "0.01",
        "cap_usd": "0.05",
        "agent_name": "explicit-agent",
        "tools": [],
        "environment": {"name": "explicit-inline"},
    }


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("prefix", PREFIXES)
def test_actual_provider_manifest_model_refuses_every_reserved_namespace(
    provider: str, prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os.environ, "get", Mock(side_effect=AssertionError("credential discovery")))
    model = OpenAIDeployment if provider == "openai" else GeminiPlan
    with pytest.raises(ProviderRuntimeConfigurationError) as refused:
        model.model_validate(plan(provider, prefix + "CUSTOM_KEY", tmp_path))
    assert refused.value.reason == "reserved_key_reference"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("key", ["DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"])
def test_host_scoped_references_validate_without_key_or_persistence(
    provider: str, key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os.environ, "get", Mock(side_effect=AssertionError("credential discovery")))
    model = OpenAIDeployment if provider == "openai" else GeminiPlan
    assert model.model_validate(plan(provider, key, tmp_path)).api_key_env == key
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("loader", [load_settings, load_turn_settings])
@pytest.mark.parametrize("selection", ["legacy-enabled", "mux-disabled"])
def test_inactive_path_does_not_discover_or_read_a_manifest(
    loader, selection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch)
    if selection == "legacy-enabled":
        monkeypatch.setenv("DAIMON_TURN__PATH", "legacy")
    else:
        monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", "false")
    original = os.environ.get

    def get(name, default=None):
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            raise AssertionError("inactive settings inspected runtime configuration")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", get)
    monkeypatch.setattr(Path, "open", Mock(side_effect=AssertionError("inactive manifest read")))
    settings = loader(_env_file=None)
    turn = settings.turn if loader is load_settings else settings
    assert turn.path == ("mux" if selection == "mux-disabled" else "legacy")


@pytest.mark.parametrize("loader", [load_settings, load_turn_settings])
def test_explicit_manifest_checks_every_row_without_looking_up_valid_keys(
    loader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch)
    path = tmp_path / "manifest.json"
    rows = [{"api_key_env": "DAIMON_CHANNEL_OPENAI_KEY"}, {"api_key_env": "GEMINI_LATE_KEY"}]
    path.write_text(json.dumps(rows))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    with pytest.raises(ProviderRuntimeConfigurationError, match="reserved provider prefix"):
        loader(_env_file=None)
    path.write_text(json.dumps(rows[:1]))
    loader(_env_file=None)  # Neither reference needs a value during configuration.


@pytest.mark.parametrize("problem", ["missing", "malformed", "shape", "oversize"])
def test_explicit_manifest_error_is_typed_static_and_does_not_reflect_content(
    problem: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "PRIVATE_OPERATOR_PATH.json"
    if problem == "malformed":
        path.write_text("PRIVATE_MANIFEST_CONTENT")
    elif problem == "shape":
        path.write_text('{"secret": "PRIVATE_MANIFEST_CONTENT"}')
    elif problem == "oversize":
        path.write_bytes(b" " * 1_048_577)
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    with pytest.raises(ProviderRuntimeConfigurationError) as refused:
        validate_configured_runtime_manifest()
    assert refused.value.reason == "invalid_manifest"
    rendered = "".join(traceback.format_exception(refused.value))
    assert "PRIVATE_OPERATOR_PATH" not in rendered
    assert "PRIVATE_MANIFEST_CONTENT" not in rendered


def test_absent_manifest_retains_deferred_selection_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    monkeypatch.setattr(Path, "open", Mock(side_effect=AssertionError("manifest discovery")))
    validate_configured_runtime_manifest()


@pytest.mark.parametrize("key", ["", "not an env name", "1KEY", "KEY=value"])
def test_existing_environment_identifier_validation_remains(key: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ApiKeyEnvironment).validate_python(key)


@pytest.mark.parametrize(
    ("provider", "model", "today", "before"),
    [
        # OpenAI's template already named web fetch a loss before #1483.
        (
            "openai",
            "gpt-6-luna",
            ("builtin_tool:web_fetch", "builtin_tool:web_search", "memory"),
            ("builtin_tool:web_fetch", "memory", "tools"),
        ),
        # Gemini's named no web tool: both granular losses are new.
        (
            "gemini",
            "gemini-3.8-flash",
            ("builtin_tool:web_fetch", "builtin_tool:web_search", "memory"),
            ("memory", "tools"),
        ),
    ],
)
@pytest.mark.parametrize("instructions", [None, "Source."])
def test_a_target_is_rebuilt_as_a_pre_1483_consent_named_it(
    provider: Provider,
    model: str,
    today: tuple[str, ...],
    before: tuple[str, ...],
    instructions: str | None,
) -> None:
    from daimon.core.turn.runtime_manifest import (
        _with_tools_dropped,  # pyright: ignore[reportPrivateUsage]
    )
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, ToolSpec

    def losses(drops: tuple[str, ...]) -> str:
        line = "Unavailable capabilities: " + ", ".join(drops) + "."
        return "\n\n".join(p for p in (instructions, line) if p)

    target = AgentSpec(
        name="copy",
        model=ModelRef(provider=provider, id=model),
        system=losses(today),
        tools=(ToolSpec(name="bash", kind="builtin"),),
        declared_drops=today,
    )
    rebuilt = _with_tools_dropped(target, instructions)
    assert rebuilt is not None
    assert rebuilt.tools == () and rebuilt.declared_drops == before
    # An agent with no instructions of its own has the losses line alone.
    assert rebuilt.system == losses(before)
    assert _with_tools_dropped(rebuilt, instructions) is None, "no tools: nothing to rebuild"


async def test_a_counterpart_that_wont_prepare_names_its_real_error(monkeypatch):
    """Smoke a1d991fb: Gemini copies and a Flash worker were refused at
    read_preparation_counterpart's catch-all with nothing logged."""
    from types import SimpleNamespace

    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.errors import AdmissionDenied
    from structlog.testing import capture_logs

    async def broken(_request):
        raise RuntimeError("catalogue read failed")

    monkeypatch.setattr(runtime_manifest, "prepare_source", broken)
    request = SimpleNamespace(
        admission=SimpleNamespace(backend_revision=SimpleNamespace(profile="gemini.inline_reuse")),
        worker=None,
    )
    with capture_logs() as logs, pytest.raises(AdmissionDenied) as refused:
        await runtime_manifest.read_preparation_counterpart(request)  # type: ignore[arg-type]
    assert refused.value.reason == "backend_unsupported"
    # Not proven missing setup: never "isn't set up here yet".
    assert refused.value.cause == "preparation_failed"
    (named,) = [e for e in logs if e["event"] == "turn.preparation_counterpart_error"]
    assert named["profile"] == "gemini.inline_reuse" and named["transient"] is False
    (cause,) = named["causes"]
    assert cause["type"] == "RuntimeError"
    assert cause["frame"].startswith("daimon.core.turn.runtime_manifest:")
    # Fixed labels only: the exception's own text never reaches the log.
    assert "catalogue read failed" not in json.dumps(logs)
