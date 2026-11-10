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
from mux.profiles import PROFILES
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
    from daimon.core.turn.consent_upgrade import with_tools_dropped
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
    rebuilt = with_tools_dropped(target, instructions)
    assert rebuilt is not None
    assert rebuilt.tools == () and rebuilt.declared_drops == before
    # An agent with no instructions of its own has the losses line alone.
    assert rebuilt.system == losses(before)
    assert with_tools_dropped(rebuilt, instructions) is None, "no tools: nothing to rebuild"


COPIES = [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]


def daimon_copy(
    provider: Provider,
    model: str,
    *,
    asks: tuple[str, ...] = (),
    runs: tuple[str, ...] = (),
    losses: tuple[str, ...] = ("memory",),
    mcp: bool = True,
):
    """A copy whose Daimon MCP tools run (`runs`) or are named losses (`asks`)."""
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec

    drops = tuple(sorted({*losses, *(f"mcp_tool:daimon-mcp:{name}" for name in asks)}))
    server = MCPConnection(
        name="daimon-mcp",
        url="https://daimon.invalid/mcp",
        credential_ref="turn",
        tool_policy={"allowed_tools": list(runs)},
    )
    return AgentSpec(
        name="copy",
        model=ModelRef(provider=provider, id=model),
        system="Source.\n\nUnavailable capabilities: " + ", ".join(drops) + ".",
        tools=(ToolSpec(name="bash", kind="builtin"),),
        mcp_servers=(server,) if mcp else (),
        declared_drops=drops,
    )


def consent_for(target):
    """What a switch confirmed for `target` recorded: its losses and whole digest."""
    from daimon.core.stores.domain import _BackendDropConsent
    from mux.state.operations import request_digest

    return _BackendDropConsent(
        declared_drops=target.declared_drops,
        source_pin="a" * 64,
        target_digest=request_digest(target.model_dump(mode="json")),
    )


def gate_also(monkeypatch: pytest.MonkeyPatch, *names: str, ungate: tuple[str, ...] = ()) -> None:
    """A later release that gates `names` (and stops gating `ungate`)."""
    from daimon.core import tool_safety

    gated = (tool_safety.confirmation_gated_tools() | set(names)) - set(ungate)
    monkeypatch.setattr(tool_safety, "confirmation_gated_tools", lambda: gated)


@pytest.mark.parametrize(("provider", "model"), COPIES)
def test_a_consent_from_before_fleet_removal_still_holds(provider: Provider, model: str) -> None:
    """Then request_fleet_config was gated, start_long_job not, and get_long_job was listed."""
    from daimon.core.turn.consent_upgrade import same_losses

    then = daimon_copy(
        provider,
        model,
        asks=("add_skill", "publish_report", "request_fleet_config"),
        runs=("describe_agent", "get_long_job", "start_long_job"),
    )
    today = daimon_copy(
        provider,
        model,
        asks=("add_skill", "publish_report", "start_long_job"),
        runs=("describe_agent", "get_long_job"),
        losses=("memory",),
    )
    assert same_losses(today, consent_for(then)), (
        "a tool Daimon removed and one it gated since are Daimon's change, not the owner's"
    )


def test_a_gemini_default_model_consent_survives_newly_gated_tools() -> None:
    """Its stored losses in full, on the provider default, against today's gated set."""
    from daimon.core.turn.consent_upgrade import model_consented, same_losses

    gated_then = (
        "add_skill",
        "create_attachment_upload_url",
        "create_notebook_upload_url",
        "publish_report",
    )
    losses = ("builtin_tool:web_fetch", "builtin_tool:web_search", "memory")
    losses += ("model_controls", "sandbox")
    then = daimon_copy(
        "gemini",
        "gemini-3.8-flash",
        asks=gated_then,
        runs=("describe_agent", "get_long_job", "start_long_job", "create_watch"),
        losses=losses,
    )
    assert then.declared_drops == (
        "builtin_tool:web_fetch",
        "builtin_tool:web_search",
        "mcp_tool:daimon-mcp:add_skill",
        "mcp_tool:daimon-mcp:create_attachment_upload_url",
        "mcp_tool:daimon-mcp:create_notebook_upload_url",
        "mcp_tool:daimon-mcp:publish_report",
        "memory",
        "model_controls",
        "sandbox",
    ), "the drops that consent stored"
    today = daimon_copy(
        "gemini",
        "gemini-3.8-flash",
        asks=(*gated_then, "start_long_job", "create_watch"),
        runs=("describe_agent", "get_long_job"),
        losses=losses,
    )
    assert same_losses(today, consent_for(then)), "only Daimon's gating changed"
    assert model_consented("gemini", "gemini-3.8-flash", None), "its default model"


@pytest.mark.parametrize(
    ("backend", "model", "picked", "consented"),
    [
        ("gemini", "gemini-3.8-flash", None, True),
        ("openai", "gpt-6-luna", None, True),
        ("gemini", "gemini-4-argon", None, False),
        ("openai", "gpt-6-astra", None, False),
        ("gemini", "gemini-4-argon", "gemini-4-argon", True),
        ("gemini", "gemini-3.8-flash", "gemini-4-argon", False),
        ("anthropic", None, None, True),
    ],
)
def test_losses_count_only_for_the_model_the_consent_was_for(
    backend: str, model: str | None, picked: str | None, consented: bool
) -> None:
    """A moved provider default is another model: it never rides on an old consent."""
    from daimon.core.turn.consent_upgrade import model_consented

    assert model_consented(backend, model, picked) is consented, (backend, model, picked)


@pytest.mark.parametrize(("provider", "model"), COPIES)
def test_an_owner_ask_tool_daimon_later_gates_needs_no_new_confirmation(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, model: str
) -> None:
    """hand_off was a loss the owner set to ask; gating it changes nothing they lose."""
    from daimon.core.turn.consent_upgrade import same_losses

    then = daimon_copy(provider, model, asks=("add_skill", "hand_off"), runs=("describe_agent",))
    gate_also(monkeypatch, "hand_off")
    today = daimon_copy(provider, model, asks=("add_skill", "hand_off"), runs=("describe_agent",))
    today = today.model_copy(update={"system": "Source, reworded.\n\nUnavailable: all of it."})
    assert same_losses(today, consent_for(then)), "gating and instructions text are Daimon's"


@pytest.mark.parametrize(("provider", "model"), COPIES)
@pytest.mark.parametrize("change", ["ungated", "owner_loss", "mcp_back", "fewer_losses"])
def test_a_gain_or_a_new_loss_still_asks_again(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, model: str, change: str
) -> None:
    """A tool gating gave back, a loss never confirmed, or MCP back on a copy without it."""
    from daimon.core.turn.consent_upgrade import same_losses

    then = daimon_copy(provider, model, asks=("add_skill",), runs=("describe_agent",))
    asks, runs, losses = ("add_skill",), ("describe_agent",), ("memory",)
    if change == "ungated":
        gate_also(monkeypatch, ungate=("add_skill",))
        asks, runs = (), ("add_skill", "describe_agent")
    elif change == "owner_loss":
        losses = ("memory", "skills")
    elif change == "mcp_back":
        then = daimon_copy(provider, model, losses=("mcp", "memory"), mcp=False)
    else:
        losses = ()
    today = daimon_copy(provider, model, asks=asks, runs=runs, losses=losses)
    assert not same_losses(today, consent_for(then)), f"{change} needs a fresh confirmation"


@pytest.mark.parametrize(("provider", "model"), COPIES)
def test_a_self_carded_tool_newly_offered_asks_again(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, model: str
) -> None:
    """A gated tool Daimon starts carding itself runs on provider turns: a gain."""
    from daimon.core import tool_safety
    from daimon.core.turn.consent_upgrade import same_losses

    then = daimon_copy(provider, model, asks=("add_skill",), runs=("describe_agent",))
    carded = tool_safety.self_carded_tools() | {"add_skill"}
    monkeypatch.setattr(tool_safety, "self_carded_tools", lambda: carded)
    today = daimon_copy(provider, model, runs=("add_skill", "describe_agent"))
    assert not same_losses(today, consent_for(then)), "add_skill now runs, never confirmed"


@pytest.mark.parametrize(("provider", "model"), COPIES)
def test_a_consent_with_the_switch_tool_off_holds_when_daimon_gates_more(
    monkeypatch: pytest.MonkeyPatch, provider: Provider, model: str
) -> None:
    """The owner turned the switch tool off before confirming; it stays a loss on both sides."""
    from daimon.core.turn.consent_upgrade import same_losses

    asks = ("add_skill", "request_agent_backend_switch")
    then = daimon_copy(provider, model, asks=asks, runs=("describe_agent", "hand_off"))
    gate_also(monkeypatch, "hand_off")
    today = daimon_copy(provider, model, asks=(*asks, "hand_off"), runs=("describe_agent",))
    assert same_losses(today, consent_for(then)), "the switch tool is the same loss as confirmed"


@pytest.mark.parametrize(
    ("provider", "today", "before"),
    [
        (
            "openai",
            ("builtin_tool:web_fetch", "builtin_tool:web_search"),
            ("builtin_tool:web_fetch",),
        ),
        ("gemini", ("builtin_tool:web_fetch", "builtin_tool:web_search"), ()),
    ],
)
def test_a_pre_1483_tools_loss_covers_todays_web_losses(
    provider: Provider, today: tuple[str, ...], before: tuple[str, ...]
) -> None:
    """A consent that lost `tools` whole holds once the template keeps all but web tools."""
    from daimon.core.turn.consent_upgrade import same_losses

    then = daimon_copy(provider, "m", losses=("memory", "tools", *before))
    now = daimon_copy(provider, "m", losses=("memory", *today))
    assert same_losses(now, consent_for(then)), "its tools come back; web tools stay lost"
    skills = daimon_copy(provider, "m", losses=("memory", "skills", *today))
    assert not same_losses(skills, consent_for(then)), "any other loss still asks again"


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


@pytest.mark.parametrize(
    "profile",
    sorted(p.profile_id for p in PROFILES.values() if p.provider in ("openai", "gemini")),
)
def test_every_template_profile_mounts_connectors_never_a_loss(profile: str) -> None:
    """Every OpenAI and Gemini profile relays each connector on Daimon's credential."""
    from daimon.core.turn.runtime_manifest import relayed_connectors
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec

    names = ("github", "linear", "notion")
    source = AgentSpec(
        name="source",
        model=ModelRef(provider="anthropic", id="m"),
        mcp_servers=tuple(MCPConnection(name=n, url=f"https://{n}.invalid/mcp") for n in names),
        tools=tuple(ToolSpec(name=n, kind="mcp_toolset") for n in names),
    )
    daimon = MCPConnection(name="daimon-mcp", url="https://d.invalid/mcp", credential_ref="turn")
    relayed = relayed_connectors(
        source,
        daimon,
        provider=PROFILES[profile].provider,
        relay_on=True,
        public_url=daimon.url,
    )
    assert [(s.name, s.url, s.credential_ref) for s in relayed] == [
        (n, f"https://d.invalid/mcp/connectors/{n}", "turn") for n in names
    ], "each connector mounts through the relay"
    off = relayed_connectors(
        source, daimon, provider=PROFILES[profile].provider, relay_on=False, public_url=daimon.url
    )
    assert off == (), "with the relay off nothing mounts"
