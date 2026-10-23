"""Operator pins authorize owned readback, never incomplete source execution."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

import pytest
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import Environment
from pydantic import ValidationError

from .native_counterpart_support import (
    PINS as PINS,
)
from .native_counterpart_support import (
    SOURCE_ID as SOURCE_ID,
)
from .native_counterpart_support import (
    Owned as Owned,
)
from .native_counterpart_support import (
    application as application,
)
from .native_counterpart_support import (
    build_runtime as build_runtime,
)
from .native_counterpart_support import (
    clean_sdk_environment as clean_sdk_environment,
)
from .native_counterpart_support import (
    db_preflight as db_preflight,
)
from .native_counterpart_support import (
    deployment as deployment,
)
from .native_counterpart_support import (
    owned as owned,
)
from .native_counterpart_support import (
    owned_database_preflight as owned_database_preflight,
)
from .native_counterpart_support import (
    plan_type as plan_type,
)
from .test_provider_dispatch import REVISION, SCOPE


@pytest.fixture(params=["openai"])
def provider(request: pytest.FixtureRequest) -> Literal["openai", "gemini"]:
    return request.param


@pytest.mark.parametrize(
    "names",
    [choice for size in (1, 2, 3) for choice in combinations(PINS, size)],
)
def test_partial_counterpart_pins_are_refused(names: tuple[str, ...], provider: str) -> None:
    with pytest.raises(ValidationError, match="complete source and catalog revision pins"):
        plan_type(provider).model_validate(
            {**deployment(provider), **{key: PINS[key] for key in names}}
        )


@pytest.mark.parametrize("key", ["source_agent_version", "counterpart_catalog_revision"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_counterpart_revision_is_a_positive_strict_integer(
    key: str, value: object, provider: str
) -> None:
    with pytest.raises(ValidationError):
        plan_type(provider).model_validate({**deployment(provider), **PINS, key: value})


def test_existing_channel_deployment_has_no_implicit_counterpart(provider: str) -> None:
    row = plan_type(provider).model_validate(deployment(provider))
    assert row.source_agent_id is None
    assert row.source_agent_version is None
    assert row.counterpart_catalog_id is None
    assert row.counterpart_catalog_revision is None


def test_complete_operator_pins_remain_separate_from_configuration_authority(provider: str) -> None:
    row = plan_type(provider).model_validate({**deployment(provider), **PINS})
    assert row.source_agent_id == SOURCE_ID
    assert row.source_agent_version == 1
    assert row.counterpart_catalog_revision == 1
    assert row.config_digest is None
    assert row.configuration_grant is not None
    with pytest.raises(ValidationError, match="configuration authorization"):
        plan_type(provider).model_validate(
            {**deployment(provider), **PINS, "configuration_grant": None}
        )


async def test_owned_counterpart_reads_actual_native_resources_and_returns_retained_revision(
    owned: Owned,
) -> None:
    row = await owned.plan.read_counterpart(
        owned.request, source=owned.source, resources=owned.resources
    )
    assert row == owned.retained
    assert [r.url.path for r in owned.wire.requests] == (
        ["/v1/agents/owned-agent", "/v1/agents/environments/templates/owned-environment"]
        if owned.wire.provider == "openai"
        else ["/v1beta/agents/" + owned.plan.agent_id]
    )
    assert row.agent.ref.id != owned.request.admission.agent.id
    assert row.agent.spec.name != owned.source.name, "names never establish counterpart authority"
    assert row.agent.revision == owned.retained.agent.revision, (
        "readback cannot invent a native CAS"
    )


@pytest.mark.parametrize(
    "defect",
    [
        "source_id",
        "source_tenant",
        "source_version",
        "recreated_same_name",
        "tenant",
        "account",
        "project",
        "channel",
        "platform",
        "origin",
        "unset_origin",
        "disabled",
        "legacy",
        "catalog_id",
        "catalog_revision",
        "native_id",
        "skill_id",
        "profile",
        "model",
        "model_provider",
        "thread_mode",
        "requires",
        "exact_digest",
        "scope",
        "session_account",
    ],
)
async def test_source_bounds_and_catalog_mismatches_refuse_before_native_io(
    owned: Owned, defect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, source, plan, resources, _, wire, manifest_path = owned
    values = owned.plan.model_dump(mode="json")
    if defect in ("tenant", "account", "project", "channel", "platform", "native_id"):
        values[
            {
                "tenant": "tenant_id",
                "account": "account_id",
                "project": "project",
                "channel": "channel_id",
                "platform": "platform",
                "native_id": "agent_id",
            }[defect]
        ] = "foreign"
    elif defect == "source_id":
        values["source_agent_id"] = str(uuid.UUID(int=777))
    elif defect == "source_version":
        values["source_agent_version"] = 2
    elif defect == "source_tenant":
        source = source.model_copy(update={"tenant_id": uuid.UUID(int=777)})
    elif defect == "recreated_same_name":
        request = replace(
            request,
            admission=replace(
                request.admission,
                agent=request.admission.agent.model_copy(update={"id": "recreated-source"}),
            ),
        )
    elif defect in ("catalog_id", "catalog_revision", "skill_id"):
        values[
            {
                "catalog_id": "counterpart_catalog_id",
                "catalog_revision": "counterpart_catalog_revision",
                "skill_id": "skill_ids",
            }[defect]
        ] = {"catalog_id": "missing", "catalog_revision": 2, "skill_id": ["foreign-skill"]}[defect]
    elif defect == "origin":
        request = replace(
            request, admission=replace(request.admission, backend_revision_origin="channel")
        )
    elif defect == "unset_origin":
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            replace(request.admission, backend_revision_origin=None)
        damaged = replace(request.admission)
        object.__setattr__(damaged, "backend_revision_origin", None)
        request = replace(request, admission=damaged)
    elif defect in ("disabled", "legacy"):
        request = replace(
            request,
            deps=replace(request.deps, channel_backends=False)
            if defect == "disabled"
            else replace(request.deps, turn_path="legacy"),
        )
    elif defect == "scope":
        request = replace(
            request, scope=SCOPE.model_copy(update={"authorization_id": "other-authority"})
        )
    elif defect == "session_account":
        request = replace(request, session_account_id=uuid.UUID(int=777))
    elif defect == "exact_digest":
        values.update(configuration_grant=None, config_digest="caller-supplied-digest")
    elif defect == "model_provider":
        original_entry = resources.entry

        async def foreign_model(catalog_id: str) -> AgentRevision:
            row = await original_entry(catalog_id)
            # Isolate the manifest pin check from the resource host's own checks.
            spec = row.agent.spec
            model = spec.model.model_copy(update={"provider": "anthropic"})
            return replace(
                row,
                agent=row.agent.model_copy(
                    update={"spec": spec.model_copy(update={"model": model})}
                ),
            )

        async def forbidden_read(catalog_id: str) -> AgentRevision:
            pytest.fail("a foreign pinned model provider reached native readback")

        monkeypatch.setattr(resources, "entry", foreign_model)
        monkeypatch.setattr(resources, "read", forbidden_read)
    else:
        selection: dict[str, Any] = {
            "backend": wire.provider,
            "profile": plan.profile,
            "model": request.admission.backend_revision.model,
        }
        selection.update(
            {
                "profile": {"backend": "openai", "profile": "openai.conversation_only"},
                "model": {"model": "gpt-6-astra"},
                "thread_mode": {"thread_mode": "shared"},
                "requires": {"requires": {"artifacts": {"level": "required"}}},
            }[defect]
        )
        revision = ConfigRevision.create(
            REVISION.channel, 2, resolve_default(BackendConfig.model_validate(selection))
        )
        request = replace(request, admission=replace(request.admission, backend_revision=revision))
    plan = type(plan).model_validate(values)
    manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied) as error:
        await plan.read_counterpart(request, source=source, resources=resources)
    assert error.value.reason == "backend_unsupported"
    assert wire.requests == []


@pytest.mark.parametrize(
    "defect",
    [
        "instructions",
        "model",
        "tool",
        "wrong_agent",
        "wrong_environment",
        "agent_deleted",
        "environment_deleted",
        "malformed_agent",
        "malformed_environment",
        "foreign_native_tenant",
    ],
)
async def test_native_mutations_and_absence_cannot_certify_counterpart(
    owned: Owned, defect: str
) -> None:
    if owned.wire.provider == "gemini":
        field = {
            "instructions": "system_instruction",
            "model": "agent_config",
            "tool": "tools",
            "wrong_agent": "id",
            "wrong_environment": "base_environment",
            "malformed_agent": "base_agent",
            "malformed_environment": "base_environment",
            "foreign_native_tenant": "id",
        }.get(defect)
        if defect in ("agent_deleted", "environment_deleted"):
            owned.wire.error = ("native", 404)
        elif defect.startswith("malformed"):
            del owned.wire.agent[field]
        else:
            owned.wire.agent[field] = "foreign"
    else:
        if defect == "instructions":
            owned.wire.agent["instructions"] = "changed"
        elif defect == "model":
            owned.wire.agent["model"] = "gpt-6-astra"
        elif defect == "tool":
            owned.wire.agent["tools"] = [{"type": "web_search"}]
        elif defect == "wrong_agent":
            owned.wire.agent["id"] = "foreign"
        elif defect == "wrong_environment":
            owned.wire.environment["id"] = "foreign"
        elif defect == "malformed_agent":
            del owned.wire.agent["model"]
        elif defect == "malformed_environment":
            del owned.wire.environment["created_at"]
        elif defect == "foreign_native_tenant":
            owned.wire.agent["metadata"]["mux_tenant"] = "foreign"
        else:
            owned.wire.error = (
                "/v1/agents/owned-agent"
                if defect == "agent_deleted"
                else "/v1/agents/environments/templates/owned-environment",
                404,
            )
    with pytest.raises(AdmissionDenied) as error:
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
    assert "private provider text" not in str(error.value)
    assert all(r.method == "GET" for r in owned.wire.requests)


async def test_catalog_change_during_readback_is_refused(
    owned: Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = owned.resources.read

    async def change(catalog_id: str) -> AgentRevision:
        await owned.resources.catalog.put_agent(
            SCOPE, owned.retained.catalog_id, owned.retained.agent, expected_revision=1
        )
        return await original(catalog_id)

    monkeypatch.setattr(owned.resources, "read", change)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )


async def test_caller_cannot_substitute_an_uninstalled_operator_grant(owned: Owned) -> None:
    values = owned.plan.model_dump(mode="json")
    values.update(
        config_digest=owned.request.admission.backend_revision.digest, configuration_grant=None
    )
    forged = type(owned.plan).model_validate(values)
    with pytest.raises(AdmissionDenied):
        await forged.read_counterpart(owned.request, source=owned.source, resources=owned.resources)
    assert owned.wire.requests == []


async def test_operator_revocation_during_readback_is_refused(
    owned: Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = owned.resources.read

    async def revoke(catalog_id: str) -> AgentRevision:
        owned.manifest_path.write_text("[]")
        return await original(catalog_id)

    monkeypatch.setattr(owned.resources, "read", revoke)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )


async def test_complete_native_component_still_cannot_unlock_agent_origin_execution(
    owned: Owned, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
        == owned.retained
    )
    path = tmp_path / "operator-runtime.json"
    path.write_text(json.dumps([owned.plan.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    before = len(owned.wire.requests)
    # Agent backends are on by default; this is the operator stop.
    stopped = replace(owned.request, deps=replace(owned.request.deps, agent_backends=False))
    with pytest.raises(AdmissionDenied):
        build_runtime(owned.wire.provider, stopped)
    assert len(owned.wire.requests) == before


@pytest.mark.parametrize("provider", ["openai"], indirect=True)
@pytest.mark.parametrize("change", ["catalog", "manifest"])
async def test_openai_authority_change_during_environment_get_is_refused(
    owned: Owned,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    original = owned.resources.backend.environments.retrieve

    async def mutate(scope: Scope, ref: ResourceRef) -> Environment:
        if change == "catalog":
            await owned.resources.catalog.put_agent(
                scope,
                owned.retained.catalog_id,
                owned.retained.agent,
                expected_revision=1,
            )
        else:
            owned.manifest_path.write_text("[]")
        return await original(scope, ref)

    monkeypatch.setattr(owned.resources.backend.environments, "retrieve", mutate)
    with pytest.raises(AdmissionDenied):
        await owned.plan.read_counterpart(
            owned.request, source=owned.source, resources=owned.resources
        )
    assert [r.url.path for r in owned.wire.requests] == [
        "/v1/agents/owned-agent",
        "/v1/agents/environments/templates/owned-environment",
    ]
