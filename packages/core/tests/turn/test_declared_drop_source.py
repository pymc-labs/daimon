"""Switch previews use the worker preservation rule, without worker authority."""

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

from ..test_fleet_permissions import captured_worker_source, parent


@pytest.fixture
def captured():
    _, source = captured_worker_source(parent())
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


@pytest.mark.parametrize(
    "obligation", ["credentials", "private", "model_options", "unknown_extension"]
)
def test_nondroppable_obligations_never_become_consent_labels(captured, obligation):
    source = captured
    if obligation in ("credentials", "private"):
        source = source.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(
                        id="protected",
                        kind="credential" if obligation == "credentials" else "native",
                    ),
                )
            }
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
    assert set(refused.value.missing) & {"credentials", "source_definition"}


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


@pytest.mark.parametrize("attachment", ["mcp", "repository", "foreign_file", "opaque_file"])
def test_dropping_a_connection_or_attachment_never_drops_credentials_or_unknown_resources(
    captured, attachment
):
    if attachment == "mcp":
        captured = captured.model_copy(
            update={
                "effective_spec": captured.effective_spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(
                                name="protected",
                                url="https://protected.invalid/mcp",
                                credential_ref="credential",
                            ),
                        ),
                    }
                )
            }
        )
    elif attachment == "repository":
        captured = captured.model_copy(
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
    else:
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
    assert set(refused.value.missing) & {"credentials", "source_definition"}


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
        {"declared_drops": ["credentials"]},
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
