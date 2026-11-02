"""Switch previews use the worker preservation rule, without worker authority."""

import json
from pathlib import Path

import pytest
from daimon.core.turn.runtime_manifest import (
    classify_source_obligations,
    preview_source_drops,
    source_preservation_pin,
)
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, SkillRef
from mux.contracts.resources import MCPConnection, ResourceBinding, ToolSpec, WorkspaceSource
from mux.errors import UnsupportedCapability
from pydantic import ValidationError

from .source_projection_support import captured_source


@pytest.fixture
def captured():
    source = captured_source()
    spec = source.effective_spec.model_copy(update={"tools": (), "skills": (), "mcp_servers": ()})
    return source.model_copy(update={"effective_spec": spec, "asks_before_publishing": False})


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_preview_declares_exact_losses_and_preserves_instructions(captured, provider, model):
    source = captured.model_copy(update={"memory_required": True})
    target = preview_source_drops(source, ModelRef(provider=provider, id=model))
    assert target.declared_drops == ("memory", "sandbox")
    assert (
        target.system
        == source.effective_spec.system + "\n\nUnavailable capabilities: memory, sandbox."
    )
    assert target.name == source.effective_spec.name
    assert target.tools == target.skills == target.mcp_servers == ()
    assert not target.extensions and not target.model.options
    assert classify_source_obligations(source, target, environment=None) == frozenset()


@pytest.mark.parametrize("obligation", ["tools", "skills", "mcp", "repository", "memory", "files"])
def test_each_source_obligation_changes_the_required_drop_set(captured, obligation):
    updates = {}
    if obligation == "tools":
        updates["tools"] = (ToolSpec(name="helper", kind="custom"),)
    elif obligation == "skills":
        updates["skills"] = (SkillRef(id="bundle", version="1"),)
    elif obligation == "mcp":
        updates["mcp_servers"] = (MCPConnection(name="public", url="https://public.invalid/mcp"),)
    source = captured.model_copy(
        update={"effective_spec": captured.effective_spec.model_copy(update=updates)}
    )
    if obligation == "repository":
        source = source.model_copy(
            update={
                "repository_sources": (
                    WorkspaceSource(
                        kind="repository",
                        target_path=".",
                        repository_url="https://public.invalid/repo",
                    ),
                )
            }
        )
    if obligation == "memory":
        source = source.model_copy(update={"memory_required": True})
    if obligation == "files":
        source = source.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(
                        id="owned-artifact",
                        kind="artifact",
                        target_path="document.txt",
                        resource=source.source.ref.model_copy(
                            update={"id": "owned-file", "kind": "file"}
                        ),
                    ),
                )
            }
        )
    target = preview_source_drops(source, ModelRef(provider="openai", id="gpt-6-luna"))
    assert target.declared_drops == tuple(sorted((obligation, "sandbox")))
    omitted = target.model_copy(update={"declared_drops": ("sandbox",)})
    assert obligation in classify_source_obligations(source, omitted, environment=None)


@pytest.mark.parametrize(
    "field",
    [
        "effective_spec",
        "source",
        "environment",
        "resource_scope",
        "memory_required",
        "memory_read_only",
        "asks_before_publishing",
    ],
)
def test_incomplete_read_only_projection_is_not_guessed_absent(captured, field):
    with pytest.raises(UnsupportedCapability) as refused:
        preview_source_drops(
            captured.model_copy(update={field: None}), ModelRef(provider="openai", id="gpt-6-luna")
        )
    assert refused.value.missing == ("source_context",)


def test_a_bare_saved_key_is_a_declared_drop_never_carried(captured):
    """The built-in agent's saved keys: the target runs without them, and says so."""
    source = captured.model_copy(
        update={"resource_requirements": (ResourceBinding(id="key", kind="credential"),)}
    )
    for provider, model in (("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")):
        target = preview_source_drops(source, ModelRef(provider=provider, id=model))
        assert "credentials" in target.declared_drops
        assert not target.mcp_servers
        assert not classify_source_obligations(source, target, environment=None)


@pytest.mark.parametrize("obligation", ["private", "model_options", "unknown_extension"])
def test_nondroppable_obligations_never_become_consent_labels(captured, obligation):
    source = captured
    if obligation == "private":
        source = source.model_copy(
            update={"resource_requirements": (ResourceBinding(id="protected", kind="native"),)}
        )
    elif obligation == "model_options":
        spec = source.effective_spec.model_copy(
            update={
                "model": source.effective_spec.model.model_copy(
                    update={"options": {"unknown": True}}
                )
            }
        )
        source = source.model_copy(update={"effective_spec": spec})
    else:
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "extensions": (
                            ExtensionConfig(
                                namespace="anthropic.unknown_source", version=1, value={}
                            ),
                        )
                    }
                )
            }
        )
    with pytest.raises(UnsupportedCapability) as refused:
        preview_source_drops(source, ModelRef(provider="openai", id="gpt-6-luna"))
    assert refused.value.missing and "credentials" not in refused.value.missing


def test_dropping_execution_does_not_bypass_publishing_approval(captured):
    target = preview_source_drops(captured, ModelRef(provider="gemini", id="gemini-3.8-flash"))
    assert "tools" in classify_source_obligations(
        captured, target, environment=None, asks_before_publishing=True
    )
    with pytest.raises(UnsupportedCapability) as refused:
        preview_source_drops(
            captured.model_copy(update={"asks_before_publishing": True}), target.model
        )
    assert "tools" in refused.value.missing


def test_instruction_changes_are_not_discharged_by_any_drops(captured):
    target = preview_source_drops(captured, ModelRef(provider="gemini", id="gemini-3.8-flash"))
    target = target.model_copy(update={"system": "Replacement instructions"})
    assert "instructions" in classify_source_obligations(captured, target, environment=None)


def test_a_credentialed_repository_is_lost_only_as_declared_repository_and_credentials(captured):
    source = captured.model_copy(
        update={
            "repository_sources": (
                WorkspaceSource(
                    kind="repository",
                    target_path=".",
                    repository_url="https://protected.invalid/repo",
                    credential_ref="credential",
                ),
            )
        }
    )
    target = preview_source_drops(source, ModelRef(provider="gemini", id="gemini-3.8-flash"))
    assert {"credentials", "repository"} <= set(target.declared_drops)
    only_repository = target.model_copy(
        update={"declared_drops": tuple(d for d in target.declared_drops if d != "credentials")}
    )
    assert "credentials" in classify_source_obligations(source, only_repository, environment=None)


@pytest.mark.parametrize("attachment", ["foreign_file", "opaque_file"])
def test_dropping_a_connection_or_attachment_never_drops_unknown_resources(captured, attachment):
    ref = captured.source.ref.model_copy(update={"id": "file", "kind": "file"})
    if attachment == "foreign_file":
        ref = ref.model_copy(update={"tenant_id": "foreign"})
    captured = captured.model_copy(
        update={
            "resource_requirements": (
                ResourceBinding(
                    id="file",
                    kind="artifact",
                    target_path="document.txt",
                    resource=ref,
                    native=ExtensionConfig(
                        namespace="anthropic.private_resource", version=1, value={}
                    )
                    if attachment == "opaque_file"
                    else None,
                ),
            )
        }
    )
    with pytest.raises(UnsupportedCapability) as refused:
        preview_source_drops(captured, ModelRef(provider="gemini", id="gemini-3.8-flash"))
    assert "source_definition" in refused.value.missing


@pytest.mark.parametrize(
    "field",
    [
        "skills",
        "mcp_servers",
        "tools",
        "system",
        "environment",
        "memory_store_id",
        "resource_generations",
    ],
)
def test_pin_detects_drift_even_when_the_loss_labels_are_unchanged(captured, field):
    initial = {}
    if field == "skills":
        initial["skills"] = (SkillRef(id="bundle", version="1"),)
    elif field == "mcp_servers":
        initial["mcp_servers"] = (
            MCPConnection(name="original", url="https://original.invalid/mcp"),
        )
    elif field == "tools":
        initial["tools"] = (ToolSpec(name="original", kind="custom"),)
    before = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(update=initial),
            "memory_required": True,
        }
    )
    source = before
    if field == "skills":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={"skills": (SkillRef(id="bundle", version="2"),)}
                )
            }
        )
    elif field == "mcp_servers":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(name="changed", url="https://changed.invalid/mcp"),
                        )
                    }
                )
            }
        )
    elif field == "tools":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={"tools": (ToolSpec(name="changed", kind="custom", permission="ask"),)}
                )
            }
        )
    elif field == "system":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={"system": "Changed protected instruction"}
                )
            }
        )
    elif field == "environment":
        source = source.model_copy(
            update={
                "environment": source.environment.model_copy(
                    update={"spec": source.environment.spec.model_copy(update={"name": "changed"})}
                )
            }
        )
    elif field == "memory_store_id":
        source = source.model_copy(update={"memory_store_id": "different-store"})
    else:
        from datetime import UTC, datetime

        source = source.model_copy(
            update={"resource_generations": (("resource", datetime(2026, 10, 14, tzinfo=UTC)),)}
        )
    model = ModelRef(provider="openai", id="gpt-6-luna")
    assert (
        preview_source_drops(before, model).declared_drops
        == preview_source_drops(source, model).declared_drops
    )
    assert source_preservation_pin(source) != source_preservation_pin(before)


def test_pin_normalizes_caller_address_but_preserves_source_ownership(captured):
    switched = captured.model_copy(
        update={
            "resource_scope": captured.resource_scope.model_copy(
                update={"account_id": "another-caller", "authorization_id": "another-turn"}
            ),
            "source": captured.source.model_copy(
                update={
                    "ref": captured.source.ref.model_copy(update={"account_id": "another-caller"})
                }
            ),
            "environment": captured.environment.model_copy(
                update={
                    "ref": captured.environment.ref.model_copy(
                        update={"account_id": "another-caller"}
                    )
                }
            ),
        }
    )
    assert source_preservation_pin(switched) == source_preservation_pin(captured)
    foreign = switched.model_copy(
        update={
            "source": switched.source.model_copy(
                update={"ref": switched.source.ref.model_copy(update={"tenant_id": "foreign"})}
            )
        }
    )
    assert source_preservation_pin(foreign) != source_preservation_pin(captured)


@pytest.fixture
def preview(captured):
    from daimon.core.channel_backend import compatibility_preview
    from daimon.core.stores.domain import TenantBackendPolicy

    return compatibility_preview(
        captured, "openai", TenantBackendPolicy(tenant_id=captured.tenant_id, revision="policy")
    )


def test_public_preview_consent_uses_the_existing_canonical_drop_domain(preview):
    original = preview.model_dump(mode="json")
    assert "drop_consent" not in original
    parsed = type(preview).model_validate(
        {
            **original,
            "drop_consent": {
                "declared_drops": ["sandbox", "memory"],
                "source_pin": "a" * 64,
                "target_digest": "b" * 64,
            },
        }
    )
    assert parsed.declared_drops == ("memory", "sandbox")
    assert parsed.model_dump(mode="json")["drop_consent"] == {
        "declared_drops": ["memory", "sandbox"],
        "source_pin": "a" * 64,
        "target_digest": "b" * 64,
    }
    assert preview.declared_drops == () and preview.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "change",
    [
        {"declared_drops": ["instructions"]},
        {"declared_drops": ["source_definition"]},
        {"declared_drops": ["memory", "memory"]},
        {"declared_drops": "memory"},
        {"source_pin": "caller supplied"},
        {"target_digest": None},
        {"unknown": True},
    ],
)
def test_public_preview_refuses_unclosed_or_nondroppable_consent(preview, change):
    consent = {"declared_drops": ["memory"], "source_pin": "a" * 64, "target_digest": "b" * 64}
    with pytest.raises(ValidationError):
        type(preview).model_validate(
            {**preview.model_dump(), "drop_consent": {**consent, **change}}
        )


def test_source_provider_identity_retains_its_full_existing_spec(captured):
    target = preview_source_drops(captured, captured.effective_spec.model)
    assert target == captured.effective_spec and target.declared_drops == ()


def _daimon_mcp(captured, toolset=None, *, credential_ref=None):
    """What every chat-enabled agent carries: the daimon-mcp server and its toolset."""
    toolset = toolset or {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": [
            {
                "name": "request_agent_backend_switch",
                "enabled": True,
                "permission_policy": {"type": "always_ask"},
            }
        ],
    }
    spec = captured.effective_spec.model_copy(
        update={
            "mcp_servers": (
                MCPConnection(
                    name="daimon-mcp",
                    url="https://mcp.invalid/mcp",
                    credential_ref=credential_ref,
                ),
            ),
            "extensions": (
                *(captured.effective_spec.extensions or ()),
                ExtensionConfig(
                    namespace="anthropic.agent_tools", version=1, value={"tools": [toolset]}
                ),
            ),
        }
    )
    return captured.model_copy(update={"effective_spec": spec})


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_daimon_mcp_toolset_is_a_declarable_loss(captured, provider, model):
    source = _daimon_mcp(captured)
    target = preview_source_drops(source, ModelRef(provider=provider, id=model))
    assert target.declared_drops == ("mcp", "sandbox", "tools")
    assert target.tools == target.mcp_servers == ()
    assert classify_source_obligations(source, target, environment=None) == frozenset()
    for kept, expected in (
        ("tools", {"tools"}),
        (("mcp",), {"mcp"}),  # declaring `tools` alone must never discharge `mcp`
        (("mcp", "tools"), {"mcp", "tools"}),
    ):
        omitted = target.model_copy(
            update={"declared_drops": tuple(d for d in target.declared_drops if d not in kept)}
        )
        assert classify_source_obligations(source, omitted, environment=None) == expected


@pytest.mark.parametrize(
    "unknown",
    ["toolset_field", "config_field", "policy_value", "toolset_type"],
)
def test_unclassified_daimon_mcp_toolset_still_refuses_as_source_definition(captured, unknown):
    toolset = {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "configs": [{"name": "request_agent_backend_switch", "enabled": True}],
    }
    if unknown == "toolset_field":
        toolset["future_execution_policy"] = {"requires_confirmation": True}
    elif unknown == "config_field":
        toolset["configs"][0]["future_execution_policy"] = {}
    elif unknown == "policy_value":
        toolset["default_config"] = {"permission_policy": {"type": "future_policy"}}
    else:
        toolset["type"] = "future_toolset"
    source = _daimon_mcp(captured, toolset)
    for provider, model in (("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")):
        with pytest.raises(UnsupportedCapability) as refused:
            preview_source_drops(source, ModelRef(provider=provider, id=model))
        assert "source_definition" in refused.value.missing


def test_daimon_mcp_server_credential_is_lost_only_with_its_server(captured):
    source = _daimon_mcp(captured, credential_ref="vault:daimon-mcp")
    target = preview_source_drops(source, ModelRef(provider="openai", id="gpt-6-luna"))
    assert target.declared_drops == ("mcp", "sandbox", "tools")
    assert classify_source_obligations(source, target, environment=None) == frozenset()
    retained = target.model_copy(update={"mcp_servers": source.effective_spec.mcp_servers})
    assert "credentials" in classify_source_obligations(source, retained, environment=None)


def _daimon_haiku55(captured):
    """Recorded resolved builtin controls plus the daimon-mcp toolset."""
    toolset = [
        *json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "mux/tests/fixtures/anthropic_retrieved_builtin_tools.json"
            ).read_text()
        ),
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {
                    "name": "request_agent_backend_switch",
                    "enabled": True,
                    "permission_policy": {"type": "always_ask"},
                }
            ],
        },
    ]
    spec = captured.effective_spec.model_copy(
        update={
            "mcp_servers": (MCPConnection(name="daimon-mcp", url="https://mcp.invalid/mcp"),),
            "extensions": (
                *(captured.effective_spec.extensions or ()),
                ExtensionConfig(
                    namespace="anthropic.agent_tools", version=1, value={"tools": toolset}
                ),
            ),
        }
    )
    return captured.model_copy(update={"effective_spec": spec})


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_daimon_haiku55_shape_declares_its_exact_loss_set_on_switch(captured, provider, model):
    """The real root shape (agent_toolset_20260401 + daimon-mcp mcp_toolset) on
    an actual provider switch: the environment/sandbox obligation that every
    switch carries, the real attached mcp server, and the unrepresented
    mcp_toolset all land in the declared set — nothing is silently dropped,
    and nothing extra is required once all three are declared."""
    source = _daimon_haiku55(captured)
    target = preview_source_drops(source, ModelRef(provider=provider, id=model))
    assert target.declared_drops == ("mcp", "sandbox", "tools")
    assert target.tools == target.mcp_servers == ()
    assert classify_source_obligations(source, target, environment=None) == frozenset()
    for retained, expected in (
        (("mcp", "sandbox"), {"tools"}),
        (("sandbox", "tools"), {"mcp"}),
        (("mcp", "tools"), {"environment"}),
        ((), {"mcp", "environment", "tools"}),
    ):
        omitted = target.model_copy(update={"declared_drops": retained})
        assert classify_source_obligations(source, omitted, environment=None) == expected


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_represented_native_builtin_loss_requires_tools_even_on_the_same_provider(
    captured, provider
):
    extension = ExtensionConfig(
        namespace="anthropic.agent_tools",
        version=1,
        value={
            "tools": [
                {
                    "type": "agent_toolset_20260401",
                    "default_config": {
                        "enabled": False,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [
                        {
                            "name": "read",
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        }
                    ],
                }
            ]
        },
    )
    source = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={"extensions": (extension,)}
            )
        }
    )
    target = source.effective_spec.model_copy(
        update={"model": ModelRef(provider=provider, id="target"), "extensions": None, "tools": ()}
    )
    assert "tools" in classify_source_obligations(source, target, environment=None)
    retained = target.model_copy(update={"tools": (ToolSpec(name="read", kind="builtin"),)})
    assert "tools" not in classify_source_obligations(source, retained, environment=None)
    if provider == "anthropic":
        native_retained = target.model_copy(update={"extensions": (extension,)})
        assert "tools" not in classify_source_obligations(source, native_retained, environment=None)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "controls,required",
    [
        ({}, False),
        ({"speed": None}, False),
        ({"speed": "standard"}, True),
        ({"speed": "fast"}, True),
        ({"effort": None}, True),
        ({"effort": "low"}, True),
        ({"effort": {"type": "medium"}, "speed": "standard"}, True),
    ],
)
def test_model_controls_are_offered_only_for_valid_unrepresented_controls(
    captured, provider, controls, required
):
    extension = ExtensionConfig(
        namespace="anthropic.model_config",
        version=1,
        value={"model": {"id": "claude-haiku-5-5", **controls}},
    )
    source = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={"extensions": (extension,)}
            )
        }
    )
    model = ModelRef(
        provider=provider, id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
    )
    target = preview_source_drops(source, model)
    assert target.declared_drops == (("model_controls", "sandbox") if required else ("sandbox",))
    assert classify_source_obligations(source, target, environment=None) == frozenset()
    if required:
        omitted = target.model_copy(update={"declared_drops": ("sandbox",)})
        assert classify_source_obligations(source, omitted, environment=None) == {"model_controls"}


@pytest.mark.parametrize(
    "version,value",
    [
        (2, {"model": {"id": "claude-haiku-5-5", "effort": "high"}}),
        (1, {"model": {"id": "claude-haiku-5-5", "effort": "invalid"}}),
        (1, {"model": {"id": "claude-haiku-5-5", "speed": "turbo"}}),
        (1, {"model": {"id": "claude-haiku-5-5", "effort": {"type": "medium", "extra": True}}}),
        (1, {"model": {"id": "claude-haiku-5-5", "unknown": True}}),
        (1, {"model": {"id": "claude-haiku-5-5"}, "unknown": True}),
    ],
)
def test_model_control_consent_never_waives_unknown_or_invalid_source(captured, version, value):
    target = preview_source_drops(captured, ModelRef(provider="openai", id="gpt-6-luna"))
    target = target.model_copy(update={"declared_drops": ("model_controls", "sandbox")})
    source = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(
                update={
                    "extensions": (
                        ExtensionConfig(
                            namespace="anthropic.model_config",
                            version=version,
                            value=value,
                        ),
                    )
                }
            )
        }
    )
    assert "source_definition" in classify_source_obligations(source, target, environment=None)
    with pytest.raises(UnsupportedCapability):
        preview_source_drops(source, target.model)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("pair", ["explicit", "caller", "slack_caller"])
def test_mcp_loss_drops_only_paired_credentials_with_absent_servers(captured, provider, pair):
    from daimon.core.tool_safety import DAIMON_SERVER_NAME

    server = MCPConnection(
        name=DAIMON_SERVER_NAME, url="https://mcp.invalid/mcp", credential_ref="paired"
    )
    reference = "paired"
    if pair != "explicit":
        reference = f"caller-mcp:{captured.tenant_id}:{captured.resource_scope.account_id}:{captured.agent_id}"
        if pair == "slack_caller":
            reference += ":slack-turn:00000000-0000-0000-0000-000000000001"
    binding = ResourceBinding(id=reference, kind="credential", credential_ref=reference)
    spec = captured.effective_spec.model_copy(
        update={"mcp_servers": (server,) if pair == "explicit" else ()}
    )
    source = captured.model_copy(
        update={"effective_spec": spec, "resource_requirements": (binding,)}
    )
    target = preview_source_drops(
        source,
        ModelRef(
            provider=provider, id="gpt-6-luna" if provider == "openai" else "gemini-3.8-flash"
        ),
    )
    assert target.declared_drops == ("mcp", "sandbox")
    assert not target.mcp_servers
    assert classify_source_obligations(source, target, environment=None) == frozenset()
    assert "mcp" in classify_source_obligations(
        source, target.model_copy(update={"declared_drops": ("sandbox",)}), environment=None
    )
    retained = target.model_copy(update={"mcp_servers": (server,)})
    assert "credentials" in classify_source_obligations(source, retained, environment=None)
    renamed = target.model_copy(
        update={
            "mcp_servers": (
                server.model_copy(update={"name": "renamed", "credential_ref": reference}),
            )
        }
    )
    assert "credentials" in classify_source_obligations(source, renamed, environment=None)
    assert "credentials" in classify_source_obligations(
        source, target, environment=None, resources=(binding,)
    )


@pytest.mark.parametrize("reference", ["unpaired", "caller-mcp:wrong:owner:agent"])
def test_mcp_loss_never_drops_unpaired_credentials(captured, reference):
    source = captured.model_copy(
        update={
            "resource_requirements": (
                ResourceBinding(id=reference, kind="credential", credential_ref=reference),
            )
        }
    )
    target = preview_source_drops(captured, ModelRef(provider="openai", id="gpt-6-luna"))
    target = target.model_copy(update={"declared_drops": ("mcp", "sandbox")})
    assert "credentials" in classify_source_obligations(source, target, environment=None)
    # Only an explicit `credentials` loss, shown to the requester, discharges it.
    assert "credentials" in preview_source_drops(source, target.model).declared_drops


def test_shared_mcp_credential_cannot_be_lost_with_one_server_retained(captured):
    servers = tuple(
        MCPConnection(name=name, url="https://mcp.invalid/mcp", credential_ref="shared")
        for name in ("lost", "retained")
    )
    source = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(update={"mcp_servers": servers}),
            "resource_requirements": (
                ResourceBinding(id="shared", kind="credential", credential_ref="shared"),
            ),
        }
    )
    target = preview_source_drops(source, ModelRef(provider="openai", id="gpt-6-luna"))
    retained = target.model_copy(update={"mcp_servers": (servers[1],)})
    assert "credentials" in classify_source_obligations(source, retained, environment=None)


@pytest.mark.parametrize(
    "corruption", ["bad_suffix", "different_ref", "opaque_native", "mounted_file"]
)
def test_caller_mcp_pairing_requires_the_complete_known_binding(captured, corruption):
    reference = (
        f"caller-mcp:{captured.tenant_id}:{captured.resource_scope.account_id}:{captured.agent_id}"
    )
    if corruption == "bad_suffix":
        reference += ":slack-turn:unknown"
    binding = ResourceBinding(id=reference, kind="credential", credential_ref=reference)
    if corruption == "different_ref":
        binding = binding.model_copy(update={"credential_ref": "different"})
    elif corruption == "opaque_native":
        binding = binding.model_copy(
            update={
                "native": ExtensionConfig(
                    namespace="anthropic.unknown_credential", version=1, value={}
                )
            }
        )
    elif corruption == "mounted_file":
        binding = binding.model_copy(update={"target_path": ".env"})
    source = captured.model_copy(update={"resource_requirements": (binding,)})
    target = preview_source_drops(captured, ModelRef(provider="openai", id="gpt-6-luna"))
    target = target.model_copy(update={"declared_drops": ("mcp", "sandbox", "files")})
    assert "credentials" in classify_source_obligations(source, target, environment=None)


def test_a_declared_credentials_loss_never_covers_a_credential_its_kept_connection_needs(captured):
    """Dropping saved keys can't carry a key: a retained MCP server's credential still refuses."""
    server = MCPConnection(name="kept", url="https://mcp.invalid/mcp", credential_ref="kept-key")
    source = captured.model_copy(
        update={
            "effective_spec": captured.effective_spec.model_copy(update={"mcp_servers": (server,)}),
            "resource_requirements": (
                ResourceBinding(id="kept-key", kind="credential", credential_ref="kept-key"),
            ),
        }
    )
    target = preview_source_drops(source, ModelRef(provider="openai", id="gpt-6-luna"))
    retained = target.model_copy(
        update={
            "mcp_servers": (server,),
            "declared_drops": tuple(sorted({*target.declared_drops, "credentials"})),
        }
    )
    assert "credentials" in classify_source_obligations(source, retained, environment=None)
