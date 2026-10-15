"""Gemini counterpart coverage reuses the shared readback fixtures and assertions."""

from __future__ import annotations

import json

import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_deployment import GeminiPlan
from pydantic import ValidationError

from . import test_native_counterpart as shared
from .native_counterpart_support import (
    PINS as PINS,
)
from .native_counterpart_support import (
    Owned as Owned,
)
from .native_counterpart_support import (
    application as application,
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

test_partial_counterpart_pins_are_refused = shared.test_partial_counterpart_pins_are_refused
test_counterpart_revision_is_a_positive_strict_integer = (
    shared.test_counterpart_revision_is_a_positive_strict_integer
)
test_existing_channel_deployment_has_no_implicit_counterpart = (
    shared.test_existing_channel_deployment_has_no_implicit_counterpart
)
test_complete_operator_pins_remain_separate_from_configuration_authority = (
    shared.test_complete_operator_pins_remain_separate_from_configuration_authority
)
test_owned_counterpart_reads_actual_native_resources_and_returns_retained_revision = (
    shared.test_owned_counterpart_reads_actual_native_resources_and_returns_retained_revision
)
test_source_bounds_and_catalog_mismatches_refuse_before_native_io = (
    shared.test_source_bounds_and_catalog_mismatches_refuse_before_native_io
)
test_native_mutations_and_absence_cannot_certify_counterpart = (
    shared.test_native_mutations_and_absence_cannot_certify_counterpart
)
test_catalog_change_during_readback_is_refused = (
    shared.test_catalog_change_during_readback_is_refused
)
test_caller_cannot_substitute_an_uninstalled_operator_grant = (
    shared.test_caller_cannot_substitute_an_uninstalled_operator_grant
)
test_operator_revocation_during_readback_is_refused = (
    shared.test_operator_revocation_during_readback_is_refused
)
test_complete_native_component_still_cannot_unlock_agent_origin_execution = (
    shared.test_complete_native_component_still_cannot_unlock_agent_origin_execution
)


@pytest.fixture(params=["gemini"])
def provider(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_gemini_native_readback_retains_every_concrete_skill_file(owned: Owned) -> None:
    row = await owned.plan.read_counterpart(
        owned.request, source=owned.source, resources=owned.resources
    )
    pins = row.agent.spec.skills
    assert pins and pins[0].version not in (None, "latest")
    assert pins[0].digest == pins[0].version
    sources = owned.wire.agent["base_environment"]["sources"]
    assert {item["target"]: item["content"] for item in sources} == {
        f".agents/skills/{pins[0].id}/SKILL.md": "# Retained\nUse every file.\n",
        f".agents/skills/{pins[0].id}/scripts/check.sh": "true\n",
    }


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("defect", ["system", "tools", "environment", "catalog_disabled"])
async def test_gemini_wrapper_requires_actual_native_catalog_policy(
    owned: Owned,
    defect: str,
) -> None:
    values = owned.plan.model_dump(mode="json")
    if defect == "system":
        values["system"] = "Different operator intent"
    elif defect == "tools":
        values["tools"] = [{"name": "google_search", "kind": "builtin"}]
    elif defect == "environment":
        values["environment"] = {
            "name": "unproven",
            "network": {"mode": "limited", "allowed_hosts": ["example.invalid"]},
        }
    else:
        owned.resources.backend.agents.enabled = False
    plan = GeminiPlan.model_validate(values)
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied) as error:
        await plan.read_counterpart(owned.request, source=owned.source, resources=owned.resources)
    assert error.value.reason == "backend_unsupported"


def test_gemini_complete_pins_require_explicit_native_identity() -> None:
    with pytest.raises(ValidationError, match="native agent identity"):
        GeminiPlan.model_validate({**deployment("gemini"), **PINS, "agent_id": None})
