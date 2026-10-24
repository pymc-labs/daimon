"""Declared profiles and extension addressing."""

from __future__ import annotations

import inspect
import pickle

import pytest
from mux import profiles
from mux.contracts import ports
from mux.contracts.extensions import ExtensionConfig, ExtensionRef
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.profile import CORE_CAPABILITIES, Profile
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.errors import ExtensionVersionError, InvalidConfig, UnsupportedCapability
from mux.profiles import (
    INLINE_REUSE,
    MANAGED_AGENTS,
    PERSISTENT_WORKSPACE,
    PROFILES,
    emits_legacy_events_for,
    get_profile,
    native_agent_tools,
    neutral_catalog_profile_for,
)
from pydantic import ValidationError


def test_declared_profiles_and_their_core_status() -> None:
    assert {pid: p.core for pid, p in PROFILES.items()} == {
        "anthropic.managed_agents": True,
        "openai.persistent_workspace": True,
        "openai.conversation_only": False,
        "gemini.inline_reuse": False,
        "selfhosted.loop": False,
    }


@pytest.mark.parametrize("profile", tuple(PROFILES.values()), ids=lambda p: p.profile_id)
def test_legacy_rendering_declarations_round_trip(profile: Profile) -> None:
    assert profile.emits_legacy_events == (profile.provider == "anthropic")
    assert emits_legacy_events_for(profile.provider) is profile.emits_legacy_events
    assert Profile.model_validate_json(profile.model_dump_json()) == profile
    assert pickle.loads(pickle.dumps(profile)) == profile


def test_missing_rendering_declaration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles, "PROFILES", {})
    with pytest.raises(InvalidConfig, match="unambiguous"):
        emits_legacy_events_for("anthropic")


def test_conflicting_rendering_declarations_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conflicting = PERSISTENT_WORKSPACE.model_copy(update={"emits_legacy_events": True})
    monkeypatch.setattr(
        profiles, "PROFILES", {"existing": PERSISTENT_WORKSPACE, "conflicting": conflicting}
    )
    with pytest.raises(InvalidConfig, match="unambiguous"):
        emits_legacy_events_for("openai")


def test_old_profile_defaults_to_normalized_rendering() -> None:
    old = PERSISTENT_WORKSPACE.model_dump(exclude={"emits_legacy_events"})
    assert Profile.model_validate(old).emits_legacy_events is False


def test_core_flag_is_checked_against_support() -> None:
    support = dict.fromkeys(CORE_CAPABILITIES, "native")
    support["cancel"] = "unknown"
    with pytest.raises(ValidationError, match="lacks cancel"):
        Profile(
            provider="openai",
            profile_id="openai.x",
            schema_version="1",
            sdk_pin="openai",
            core=True,
            support=support,  # pyright: ignore[reportArgumentType]
        )


def test_undeclared_capability_is_unknown() -> None:
    assert PERSISTENT_WORKSPACE.support_for("tool_confirmation") == "unknown"


def test_unknown_profile_id() -> None:
    with pytest.raises(InvalidConfig):
        get_profile("anthropic.nope")
    assert get_profile("anthropic.managed_agents") is MANAGED_AGENTS


def test_extension_lookup_distinguishes_never_from_wrong_version() -> None:
    assert MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 1) == ExtensionRef(
        namespace="anthropic.memory_stores", version=1
    )
    with pytest.raises(ExtensionVersionError) as caught:
        MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 2)
    assert caught.value.offered == (1,)
    with pytest.raises(UnsupportedCapability):
        PERSISTENT_WORKSPACE.offered_extension("anthropic.memory_stores", 1)


def test_extensions_are_namespaced_by_provider() -> None:
    with pytest.raises(ValidationError):
        ExtensionConfig(namespace="memory_stores", version=1)
    with pytest.raises(ValidationError, match="not a openai namespace"):
        Profile.model_validate(
            PERSISTENT_WORKSPACE.model_dump()
            | {"extensions": [{"namespace": "anthropic.vaults", "version": 1}]}
        )


def test_no_port_exposes_a_raw_client() -> None:
    for _, port in inspect.getmembers(ports, inspect.isclass):
        if port.__module__ != ports.__name__:
            continue
        names = {n for n in dir(port) if not n.startswith("__")}
        assert not {n for n in names if "client" in n or n.startswith("raw")}, port.__name__


@pytest.mark.parametrize(
    ("provider", "expected"),
    (("anthropic", None), ("openai", PERSISTENT_WORKSPACE), ("gemini", INLINE_REUSE)),
)
def test_catalog_selection_uses_profile_support(
    provider: Provider, expected: Profile | None
) -> None:
    assert neutral_catalog_profile_for(provider) is expected
    assert neutral_catalog_profile_for("unregistered") is None


def test_catalog_support_can_be_removed_without_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = PERSISTENT_WORKSPACE.model_copy(
        update={"support": {**PERSISTENT_WORKSPACE.support, "neutral_catalog": "unsupported"}}
    )
    monkeypatch.setattr(profiles, "PROFILES", {disabled.profile_id: disabled})
    assert neutral_catalog_profile_for(disabled.provider) is None


def test_ambiguous_catalog_declarations_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    duplicate = PERSISTENT_WORKSPACE.model_copy(update={"profile_id": "openai.other_workspace"})
    monkeypatch.setattr(
        profiles,
        "PROFILES",
        {PERSISTENT_WORKSPACE.profile_id: PERSISTENT_WORKSPACE, duplicate.profile_id: duplicate},
    )
    with pytest.raises(InvalidConfig, match="ambiguous neutral catalog"):
        neutral_catalog_profile_for(PERSISTENT_WORKSPACE.provider)


def test_resource_profile_native_projection_preserves_authored_intent() -> None:
    authored = AgentSpec(
        name="tools",
        model=ModelRef(provider="openai", id="offline"),
        tools=(
            ToolSpec(name="bash", kind="builtin"),
            ToolSpec(name="web_search", kind="builtin"),
            ToolSpec(name="server", kind="mcp_toolset"),
            ToolSpec(name="helper", kind="custom", description="", input_schema=None),
        ),
    )
    assert native_agent_tools(PERSISTENT_WORKSPACE, authored) == (
        ToolSpec(name="web_search", kind="builtin"),
        ToolSpec(name="helper", kind="custom", input_schema={}),
    )
    assert native_agent_tools(INLINE_REUSE, authored) == authored.tools
    assert (
        native_agent_tools(
            PERSISTENT_WORKSPACE.model_copy(update={"tool_readback": "authored"}), authored
        )
        == authored.tools
    )
    assert PERSISTENT_WORKSPACE.execution_tool == "bash"
    assert PERSISTENT_WORKSPACE.skill_digest == "content"
    assert PERSISTENT_WORKSPACE.support_for("agent_delete") == "native"
    assert INLINE_REUSE.execution_tool == "code_execution"
    assert INLINE_REUSE.skill_digest == "version"
    assert INLINE_REUSE.catalog_readback == "verify"
    assert INLINE_REUSE.support_for("agent_delete") == "unsupported"


def test_new_resource_declarations_default_closed_for_old_profile_rows() -> None:
    row = PERSISTENT_WORKSPACE.model_dump()
    for key in ("execution_tool", "skill_digest", "tool_readback", "catalog_readback"):
        row.pop(key)
    row["support"] = {
        k: v for k, v in row["support"].items() if k not in {"neutral_catalog", "agent_delete"}
    }
    restored = Profile.model_validate(row)
    assert restored.support_for("neutral_catalog") == "unknown"
    assert restored.support_for("agent_delete") == "unknown"
    assert restored.skill_digest is None
    assert restored.tool_readback == "authored"
    assert restored.catalog_readback == "retrieve"


def test_native_agent_tool_projection_uses_the_closed_anthropic_compiler():
    from mux.drivers.anthropic.schemas import agent_tools

    extension = ExtensionConfig(
        namespace="anthropic.agent_tools",
        version=1,
        value={
            "tools": [
                {
                    "type": "custom",
                    "name": "helper",
                    "description": "Helper",
                    "input_schema": {"type": "object"},
                }
            ]
        },
    )
    spec = AgentSpec(
        name="source",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        tools=agent_tools(extension).intent(),
    )
    assert native_agent_tools(MANAGED_AGENTS, spec) == (
        ToolSpec(
            name="helper", kind="custom", description="Helper", input_schema={"type": "object"}
        ),
    )
    assert agent_tools(extension.model_copy(update={"value": {"tools": []}})).intent() == ()
    # A well-formed toolset with no neutral projection — an unconfigured
    # agent_toolset_20260401 (ambiguous native defaults) or any mcp_toolset
    # (e.g. daimon-mcp, which every chat-enabled agent carries) — is excluded
    # per toolset rather than raised: `.intent()` only drops its contribution,
    # `has_unrepresented_toolset()` reports the drop for obligation bookkeeping.
    bare_toolset = agent_tools(
        extension.model_copy(update={"value": {"tools": [{"type": "agent_toolset_20260401"}]}})
    )
    assert bare_toolset.intent() == () and bare_toolset.has_unrepresented_toolset()
    mcp_toolset = agent_tools(
        extension.model_copy(
            update={
                "value": {
                    "tools": [
                        {
                            "type": "agent_toolset_20260401",
                            "default_config": {"enabled": False},
                            "configs": [
                                {
                                    "name": "bash",
                                    "enabled": True,
                                    "permission_policy": {"type": "always_allow"},
                                }
                            ],
                        },
                        {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"},
                    ]
                }
            }
        )
    )
    assert mcp_toolset.intent() == (ToolSpec(name="bash", kind="builtin"),)
    assert mcp_toolset.has_unrepresented_toolset()
    # Schema-invalid/unknown native content is still a hard refusal, never excluded.
    with pytest.raises(ValueError):
        agent_tools(
            extension.model_copy(update={"value": {"tools": [{"type": "unknown_toolset"}]}})
        )


def test_conversation_worker_is_explicit_without_changing_openai_selection():
    from mux.profiles import (
        CONVERSATION_ONLY,
        RUNNABLE_PROFILES,
        backend_selection,
        runnable_selection,
    )

    selected = backend_selection("openai")
    assert selected.profile == PERSISTENT_WORKSPACE.profile_id
    conversation = selected.model_copy(update={"profile": CONVERSATION_ONLY.profile_id})
    assert conversation.profile in RUNNABLE_PROFILES and runnable_selection(conversation)
    assert not runnable_selection(conversation.model_copy(update={"model": "unapproved-model"}))
    assert not runnable_selection(conversation.model_copy(update={"thread_mode": "shared"}))
    assert neutral_catalog_profile_for("openai") is PERSISTENT_WORKSPACE
    assert (
        neutral_catalog_profile_for("openai", profile_id=CONVERSATION_ONLY.profile_id)
        is CONVERSATION_ONLY
    )
    assert neutral_catalog_profile_for("gemini", profile_id=CONVERSATION_ONLY.profile_id) is None


@pytest.mark.parametrize(
    "names", [("read",), ("read", "grep", "glob"), ("read", "edit", "grep", "glob", "write")]
)
def test_execution_mapping_refuses_restricted_filesystem_authority(names):
    from mux.profiles import represented_execution_tools

    with pytest.raises(InvalidConfig):
        represented_execution_tools(
            INLINE_REUSE, tuple(ToolSpec(name=n, kind="builtin") for n in names)
        )


def test_execution_mapping_requires_complete_automatic_authority():
    from mux.profiles import represented_execution_tools

    tools = tuple(
        ToolSpec(name=n, kind="builtin") for n in ("bash", "read", "edit", "grep", "glob", "write")
    )
    assert represented_execution_tools(INLINE_REUSE, tools) == (
        ToolSpec(name="code_execution", kind="builtin"),
    )
    for permission in ("ask",):
        with pytest.raises(InvalidConfig):
            represented_execution_tools(
                INLINE_REUSE, (tools[0].model_copy(update={"permission": permission}), *tools[1:])
            )


@pytest.mark.parametrize("disabled", ["bash", "write", "edit"])
def test_native_disabled_capability_cannot_acquire_general_execution(disabled):
    from mux.drivers.anthropic.schemas import AgentToolsConfig
    from mux.profiles import represented_execution_tools

    source = AgentToolsConfig.model_validate(
        {
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "enabled": False,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [
                        {"name": name, "enabled": name != disabled}
                        for name in ("bash", "read", "edit", "grep", "glob", "write")
                    ],
                }
            ]
        }
    )
    with pytest.raises(InvalidConfig):
        represented_execution_tools(INLINE_REUSE, source.intent(exact=True))


@pytest.mark.parametrize("web", [("web_fetch",), ("web_search",), ("web_fetch", "web_search")])
def test_enabled_native_web_authority_is_preserved_by_execution_mapping(web):
    from mux.profiles import represented_execution_tools

    tools = tuple(
        ToolSpec(name=name, kind="builtin")
        for name in ("bash", "read", "edit", "grep", "glob", "write", *web)
    )
    mapped = represented_execution_tools(INLINE_REUSE, tools)
    expected = (
        {"code_execution"}
        | ({"url_context"} if "web_fetch" in web else set())
        | ({"google_search"} if "web_search" in web else set())
    )
    assert {tool.name for tool in mapped} == expected


@pytest.mark.parametrize("name", ["pdf", "docx", "pptx", "xlsx"])
@pytest.mark.parametrize(
    "source,expected", [(None, True), ("anthropic", True), ("custom", False), ("other", False)]
)
def test_builtin_bare_id_fallback_only_without_source(
    name: str, source: str | None, expected: bool
) -> None:
    from mux.contracts.ids import SkillRef
    from mux.profiles.anthropic import is_builtin_skill

    assert is_builtin_skill(SkillRef(id=name, source=source)) is expected


def test_selectable_models_list_the_default_first_and_never_pro() -> None:
    from mux.profiles import selectable_models

    assert selectable_models("gemini") == ("gemini-3.8-flash", "gemini-4-argon")
    assert selectable_models("openai") == ("gpt-6-luna", "gpt-6-astra")
    assert not any("pro" in model for model in selectable_models("gemini"))
