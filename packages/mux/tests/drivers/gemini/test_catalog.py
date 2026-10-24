"""Opt-in native managed-agent compiler/readback, never a live cell certificate."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from typing import NoReturn, cast

import httpx
import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, ThreadRef
from mux.contracts.resources import (
    AgentFilter,
    EnvironmentSpec,
    MCPConnection,
    SessionSpec,
    ToolSpec,
)
from mux.drivers.gemini import GeminiCatalogAdapter, GeminiManagedAgents
from mux.drivers.gemini.core import GeminiAgents
from mux.drivers.gemini.transport import Object
from mux.errors import OperationConflict, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.catalog_wire import (
    SCOPE,
    WORKSPACE,
    Case,
    bundle,
    replay_client,
    spec,
)
from mux.testing.drivers.gemini.catalog_wire import (
    case as case,
)
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from pydantic import TypeAdapter

_OBJECT: TypeAdapter[Object] = TypeAdapter(Object)


async def test_native_catalog_create_uses_real_sdk_and_independent_get(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="create:source")
    adapter = case.driver.catalog_adapter
    assert adapter is not None
    expected = await adapter.compile(SCOPE, spec(), id_=made.ref.id)
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    assert (
        case.wire.requests[0].content
        == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert expected["tools"] == [{"type": "code_execution"}]
    assert made.revision.native is None  # No fictional provider version.
    assert made.ref.id in case.wire.agents
    assert made.spec.metadata == spec().metadata  # Host-only intent, not provider metadata.
    assert await adapter.verify(SCOPE, made) == made
    assert not case.interactions.requests


async def test_default_false_keeps_existing_inline_port_and_never_calls_native(case: Case) -> None:
    driver = GeminiManagedAgents(
        case.interactions,
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id=WORKSPACE,
        resource_catalog_transport=case.native,
    )
    assert driver.catalog_adapter is None and type(driver.agents) is GeminiAgents
    made = await driver.agents.create(SCOPE, spec(), key="default")
    assert made.native is None
    assert not case.wire.requests
    disabled = GeminiCatalogAdapter(case.interactions, case.storage, WORKSPACE, case.native)
    with pytest.raises(UnsupportedCapability, match="gemini_resource_catalog"):
        await disabled.create(SCOPE, spec(), key="disabled")
    assert not case.wire.requests


def test_enabled_requires_explicit_transport_without_allocating_sdk() -> None:
    with pytest.raises(UnsupportedCapability, match="gemini_resource_catalog_transport"):
        GeminiManagedAgents(
            FakeTransport(),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
        )


@pytest.mark.parametrize("flag", [1, "true", None])
def test_catalog_capability_is_an_explicit_bool(flag: object) -> None:
    with pytest.raises(ValueError, match="explicit bool"):
        GeminiManagedAgents(
            FakeTransport(),
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=cast(bool, flag),
        )


async def test_restart_reconciles_accepted_lost_post_by_get_without_resending(case: Case) -> None:
    case.wire.lose_post = True
    with pytest.raises(ProviderError):
        await case.driver.agents.create(SCOPE, spec(), key="lost-post")
    async with case.storage.transaction() as records:
        assert records.creations and not records.agents
    restarted = GeminiManagedAgents(
        case.interactions,
        storage=case.storage,
        state_store=MemoryStateStore(),
        account_scope_id=WORKSPACE,
        resource_catalog_enabled=True,
        resource_catalog_transport=case.native,
    )
    made = await restarted.agents.create(SCOPE, spec(), key="lost-post")
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    assert made.ref.id in case.wire.agents
    with pytest.raises(OperationConflict):
        await restarted.agents.create(SCOPE, spec(name="changed"), key="lost-post")
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]


async def test_unverified_get_keeps_fence_without_fabricating_agent(case: Case) -> None:
    case.wire.lose_post = True
    with pytest.raises(ProviderError):
        await case.driver.agents.create(SCOPE, spec(), key="unknown")
    case.wire.agents.clear()
    with pytest.raises(ProviderError) as error:
        await case.driver.agents.create(SCOPE, spec(), key="unknown")
    assert error.value.native_code == "catalog_create_unverified"
    assert [r.method for r in case.wire.requests] == ["POST", "GET"]
    async with case.storage.transaction() as records:
        assert records.creations and not records.agents


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "base_agent",
        "agent_config",
        "tools",
        "base_environment",
        "description",
        "system_instruction",
    ],
)
async def test_native_readback_drift_refuses_instead_of_echoing_catalog(
    case: Case, field: str
) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="drift")
    case.wire.get_changes[field] = None
    with pytest.raises(ProviderError) as error:
        await case.driver.agents.retrieve(SCOPE, made.ref)
    assert error.value.category == "conflict"
    assert len([r for r in case.wire.requests if r.method == "POST"]) == 1


async def test_sparse_null_sdk_ack_and_nullable_metadata_do_not_require_native_refs(
    case: Case,
) -> None:
    case.wire.sparse_ack = True
    requested = spec().model_copy(update={"description": None, "system": None, "tools": ()})
    case.wire.get_changes = {"description": None, "system_instruction": None, "metadata": None}
    made = await case.driver.agents.create(SCOPE, requested, key="nulls")
    assert made.spec == requested
    assert case.wire.agents[made.ref.id]["tools"] == []
    assert await case.driver.agents.retrieve(SCOPE, made.ref) == made


async def test_recorded_empty_list_readback_reconciles_without_replaying_post(case: Case) -> None:
    # Recorded GET shape: id/base_agent/agent_config/system_instruction, with
    # tools omitted and base_environment containing only type.
    requested = spec().model_copy(update={"description": None, "tools": ()})
    case.wire.omit_empty_lists = True
    case.wire.lose_post = True
    with pytest.raises(ProviderError):
        await case.driver.agents.create(SCOPE, requested, key="empty-readback")
    made = await case.driver.agents.create(SCOPE, requested, key="empty-readback")
    assert await case.driver.agents.retrieve(SCOPE, made.ref) == made
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]
    assert made.spec == requested
    assert case.driver.catalog_adapter is not None
    expected = await case.driver.catalog_adapter.compile(SCOPE, requested, id_=made.ref.id)
    assert expected["tools"] == []
    assert expected["base_environment"] == {"type": "remote", "sources": []}
    observed = await case.native.get_agent(made.ref.id)
    assert set(observed) == {
        "id",
        "base_agent",
        "agent_config",
        "system_instruction",
        "base_environment",
    }
    assert observed["base_agent"] == expected["base_agent"]
    assert observed["agent_config"] == expected["agent_config"]
    assert observed["base_environment"] == {"type": "remote"}


@pytest.mark.parametrize(
    "change",
    [
        {"tools": [{"type": "code_execution"}]},
        {"tools": None},
        {"base_environment": {"type": "remote", "unknown": []}},
        {"base_environment": {"type": "remote", "sources": [{"target": "/extra"}]}},
        {"base_environment": {"type": "remote", "sources": None}},
        {"base_environment": {}},
    ],
)
async def test_empty_list_omission_does_not_hide_added_or_unknown_capabilities(
    case: Case, change: Object
) -> None:
    requested = spec().model_copy(update={"description": None, "tools": ()})
    case.wire.omit_empty_lists = True
    made = await case.driver.agents.create(SCOPE, requested, key="empty-negative")
    case.wire.get_changes = change
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await case.driver.agents.retrieve(SCOPE, made.ref)
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("missing", ["tools", "sources"])
async def test_missing_nonempty_list_still_refuses(case: Case, missing: str) -> None:
    pin = await case.driver.skills.create(SCOPE, bundle(), key="mounted-skill")
    assert pin.latest_version is not None
    made = await case.driver.agents.create(
        SCOPE, spec(pins=(pin.latest_version,)), key="nonempty-readback"
    )
    body = case.wire.agents[made.ref.id]
    if missing == "tools":
        del body["tools"]
    else:
        environment = body["base_environment"]
        assert isinstance(environment, dict)
        del environment["sources"]
    with pytest.raises(ProviderError, match="catalog_native_definition_drift"):
        await case.driver.agents.retrieve(SCOPE, made.ref)


@pytest.mark.parametrize(
    "target", ["tenant_id", "account_id", "account_scope_id", "provider", "kind"]
)
async def test_foreign_native_reference_refuses_before_readback(case: Case, target: str) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="scope")
    count = len(case.wire.requests)
    wrong = made.ref.model_copy(update={target: "other"})
    with pytest.raises(ScopeViolation):
        await case.driver.agents.retrieve(SCOPE, wrong)
    assert len(case.wire.requests) == count


@pytest.mark.parametrize("bad", ["model", "pro", "latest", "digest", "credentials", "tool"])
async def test_unsupported_authored_intent_refuses_before_native_post(case: Case, bad: str) -> None:
    requested = spec()
    if bad == "model":
        requested = requested.model_copy(
            update={"model": ModelRef(provider="anthropic", id="claude-haiku-4-5-20251001")}
        )
    elif bad == "pro":
        requested = requested.model_copy(
            update={"model": ModelRef(provider="gemini", id="gemini-3.5-pro")}
        )
    elif bad == "credentials":
        requested = requested.model_copy(
            update={
                "mcp_servers": (
                    MCPConnection(
                        name="mcp", url="https://mcp.invalid", credential_ref="credential:owned"
                    ),
                )
            }
        )
    elif bad == "tool":
        requested = requested.model_copy(update={"tools": (ToolSpec(name="bash", kind="builtin"),)})
    else:
        skill = await case.driver.skills.create(SCOPE, bundle(), key="skill")
        assert skill.latest_version is not None
        pin = skill.latest_version.model_copy(
            update={"version": "latest"} if bad == "latest" else {"digest": "wrong"}
        )
        requested = requested.model_copy(update={"skills": (pin,)})
    with pytest.raises(UnsupportedCapability):
        await case.driver.agents.create(SCOPE, requested, key="refusal")
    assert not case.wire.requests


async def test_argon_is_an_allowed_native_catalog_model(case: Case) -> None:
    argon = spec().model_copy(update={"model": ModelRef(provider="gemini", id="gemini-4-argon")})
    made = await case.driver.agents.create(SCOPE, argon, key="create:argon")
    posted = _OBJECT.validate_json(case.wire.requests[0].content, strict=True)
    assert posted["agent_config"] == {"type": "antigravity", "model": "gemini-4-argon"}
    assert made.spec.model.id == "gemini-4-argon"


@pytest.mark.parametrize("near", ["gemini-4-argon-preview", "gemini-4", "gemini-4-argon-latest"])
async def test_argon_near_miss_ids_refuse_before_native_post(case: Case, near: str) -> None:
    requested = spec().model_copy(update={"model": ModelRef(provider="gemini", id=near)})
    with pytest.raises(UnsupportedCapability):
        await case.driver.agents.create(SCOPE, requested, key="near")
    assert not case.wire.requests


async def test_copy_preserves_concrete_bundle_bytes_and_old_pin_after_publish(case: Case) -> None:
    source = await case.driver.skills.create(SCOPE, bundle(), key="skill:source")
    assert source.latest_version is not None
    original = source.latest_version
    made = await case.driver.agents.create(SCOPE, spec(pins=(original,)), key="agent:source")
    copied = await case.driver.skills.create(SCOPE, bundle(), key="skill:copy")
    assert copied.latest_version is not None and copied.id != source.id
    forked = await case.driver.agents.create(
        SCOPE, spec(name="fork", pins=(copied.latest_version,)), key="agent:fork"
    )
    await case.driver.skills.publish_version(
        SCOPE, source.id, bundle(b"# Changed\n"), key="skill:v2"
    )
    assert made.spec.skills == (original,)
    assert await case.driver.agents.retrieve(SCOPE, made.ref) == made
    assert await case.driver.agents.retrieve(SCOPE, forked.ref) == forked
    old_sources = _OBJECT.validate_python(case.wire.agents[made.ref.id]["base_environment"])[
        "sources"
    ]
    copied_sources = _OBJECT.validate_python(case.wire.agents[forked.ref.id]["base_environment"])[
        "sources"
    ]
    assert isinstance(old_sources, list) and isinstance(copied_sources, list)
    assert [_OBJECT.validate_python(s)["content"] for s in old_sources] == [
        _OBJECT.validate_python(s)["content"] for s in copied_sources
    ]
    assert all(source.id in str(_OBJECT.validate_python(s)["target"]) for s in old_sources)
    assert all(copied.id in str(_OBJECT.validate_python(s)["target"]) for s in copied_sources)


async def test_local_bundle_drift_refuses_before_native_read(case: Case) -> None:
    skill = await case.driver.skills.create(SCOPE, bundle(), key="bundle-drift")
    assert skill.latest_version is not None and skill.latest_version.version is not None
    made = await case.driver.agents.create(
        SCOPE, spec(pins=(skill.latest_version,)), key="bundle-agent"
    )
    async with case.storage.transaction() as records:
        records.skills[skill.id].bundles[skill.latest_version.version] = bundle(b"# Forged\n")
    before = len(case.wire.requests)
    with pytest.raises(ProviderError, match="catalog_skill_drift"):
        await case.driver.agents.retrieve(SCOPE, made.ref)
    assert len(case.wire.requests) == before


async def test_named_agent_cannot_silently_enter_inline_session_plan(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="native")
    environment = await case.driver.environments.create(
        SCOPE, EnvironmentSpec(name="inline"), key="env"
    )
    with pytest.raises(UnsupportedCapability, match="named_agent_session_plan"):
        await case.driver.sessions.create(
            SCOPE,
            SessionSpec(
                agent=made.ref,
                agent_revision=made.revision,
                environment=environment.ref,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={
                            "thread": ThreadRef(
                                channel=ChannelRef(
                                    tenant_id=SCOPE.tenant_id,
                                    platform="discord",
                                    channel_id="channel",
                                ),
                                thread_id="thread",
                            ).model_dump(mode="json"),
                            "binding_id": "binding",
                        },
                    )
                },
            ),
            key="session",
        )
    assert not case.interactions.requests


async def test_adapter_lists_only_owned_native_definitions_and_verifies_each(case: Case) -> None:
    made = await case.driver.agents.create(SCOPE, spec(), key="owned")
    inline = GeminiAgents(case.interactions, case.storage, WORKSPACE)
    await inline.create(SCOPE, spec(name="inline"), key="inline")
    page = await case.driver.agents.list(SCOPE, filters=AgentFilter(), page=PageRequest())
    assert page.data == (made,)
    assert [r.method for r in case.wire.requests] == ["POST", "GET", "GET"]


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise AssertionError("read ambient value")


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_API_KEY",
        "GEMINI_FUTURE_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SSLKEYLOGFILE",
        "HTTPS_PROXY",
    ],
)
def test_sdk_replay_refuses_present_names_before_any_value_or_allocation(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("allocated SDK/HTTP client")

    wire = httpx.MockTransport(lambda _: httpx.Response(500))
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", NamesOnly(name))
        patch.setattr("google.genai.Client", forbidden)
        patch.setattr(httpx, "Client", forbidden)
        patch.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="ambient SDK"):
            replay_client(wire)
