"""Per-resource drift tests: the authoring spec must mirror the SDK TypedDict.

These fail loud on SDK upgrade when a new `NotRequired` field appears —
pyright cannot catch this (the SDK still accepts our smaller kwargs) and
`extra="forbid"` cannot catch it (we're not sending anything extra). Fix by
adding the new field to the spec.
"""

from __future__ import annotations

from anthropic.types.beta import agent_create_params, environment_create_params
from daimon.core.specs import AgentSpec, EnvironmentSpec


def test_environment_spec_mirrors_sdk() -> None:
    sdk_fields = set(environment_create_params.EnvironmentCreateParams.__annotations__)
    spec_fields = set(EnvironmentSpec.model_fields)
    # metadata synthesized at upload from (account_id, name); betas not author-facing.
    exempt = {"metadata", "betas"}
    assert spec_fields == sdk_fields - exempt, (
        "EnvironmentSpec drifted from the SDK. "
        f"spec={spec_fields}, sdk-exempt={sdk_fields - exempt}"
    )


def test_agent_spec_mirrors_sdk() -> None:
    sdk_fields = set(agent_create_params.AgentCreateParams.__annotations__)
    spec_fields = set(AgentSpec.model_fields)
    # metadata synthesized at upload; betas not author-facing; skills is the
    # identity-reference exception (authoring names, not SDK skill params).
    exempt = {"metadata", "betas"}
    # The spec's `skills` and `skill_repos` are sibling authoring fields not
    # sent to the SDK (skills resolved at upload; skill_repos consumed by
    # the sync subsystem). Excluded from the mirror comparison.
    sibling = {"skills", "skill_repos"}
    assert spec_fields - sibling == sdk_fields - exempt - sibling, (
        "AgentSpec drifted from the SDK. "
        f"spec-minus-sibling={spec_fields - sibling}, "
        f"sdk-exempt={sdk_fields - exempt - sibling}"
    )
