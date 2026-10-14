"""Observe the native NEW27 prerequisite without substituting an MA submit.

A3 currently offers GitHub and environment input only. An unavailable native
MCP form supplies a source-bound PENDING diagnostic, never a used_at observation
or an inference from the Anthropic consume-before-probe result.
"""

from __future__ import annotations

import json
from typing import Literal, cast

from daimon.core.turn.openai_credential_requests import (
    PROFILE,
    NativeCredentialOrigin,
    OpenAINativeCredentialAdapter,
)
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.outcome_oracle import CheckResult, EvidenceModel, OutcomeReport
from mux.errors import UnsupportedCapability

if __package__:
    from .catalog_runner import ScenarioPlan, sha
    from .catalog_steps import validate_step_sources
else:
    from catalog_runner import ScenarioPlan, sha
    from catalog_steps import validate_step_sources

SCENARIO = "QA-NEW27-MISTYPED-TOKEN-HEADLESS"
SOURCE_SQL = "select used_at is null from credential_requests where id = '{headless.request_id}'"
DOCUMENT_PINS = {
    "5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd": "cc789be831f02d0c585032e8818c565de9d40e546b3cd3dbbe3fd50724c5c9a1",
    "21dbb0f275e8770c69a2896affa7755bf74a118bb91f8a63b8e76bb4f8c3310e": "a139bf8465206eab4a1306d1aa989ab35cfedd4a3bd1d8f90bff23945c6c1419",
}
STEP = {
    "do": "admin",
    "tool": "headless",
    "args": {
        "call": "credential_submit.mcp_token",
        "token": "wrong-token-123",
        "url": "{env.qa_token_mcp_url}",
    },
}


def validate_openai_new27(plan: ScenarioPlan) -> None:
    """Keep N7/N3's exact source, admin step and original unused predicate."""
    document = sha(json.dumps(plan.scenario.scenario, sort_keys=True, allow_nan=False).encode())
    if (
        plan.backend != "openai"
        or plan.scenario.id != SCENARIO
        or plan.scenario.load_error is not None
        or DOCUMENT_PINS.get(plan.scenario.sha256) != document
        or plan.bindings
        or plan.global_assertions
        or plan.assertions != ({"kind": "db_check", "sql": SOURCE_SQL, "expect": True},)
        or len(plan.invocations) != 1
        or plan.invocations[0].location != "steps[0]"
        or plan.invocations[0].operation != "unbound"
        or plan.invocations[0].source != STEP
        or any(
            getattr(plan.invocations[0], field) is not None
            for field in ("turn", "channel", "thread", "text", "fixture", "fixture_sha256")
        )
    ):
        raise ValueError("OpenAI NEW27 differs from the pinned administrative source")
    validate_step_sources(plan)


class OpenAIRejectedTokenGap(EvidenceModel):
    """An actual admission refusal; deliberately contains no request/readback."""

    scenario_id: Literal["QA-NEW27-MISTYPED-TOKEN-HEADLESS"] = SCENARIO
    backend: Literal["openai"] = "openai"
    product_profile: Literal["opt-in-product"] = "opt-in-product"
    source_sha256: Literal[
        "5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd",
        "21dbb0f275e8770c69a2896affa7755bf74a118bb91f8a63b8e76bb4f8c3310e",
    ]
    source_sql: Literal[
        "select used_at is null from credential_requests where id = '{headless.request_id}'"
    ] = SOURCE_SQL
    code: Literal["OPENAI_NATIVE_MCP_FORM_UNAVAILABLE"] = "OPENAI_NATIVE_MCP_FORM_UNAVAILABLE"
    observed_step: Literal["native_mcp_offer_admission"] = "native_mcp_offer_admission"
    submit_observed: Literal[False] = False
    rejecting_probe_observed: Literal[False] = False
    used_at_observed: Literal[False] = False


async def observe_openai_new27_prerequisite(
    plan: ScenarioPlan,
    adapter: OpenAINativeCredentialAdapter,
    origin: NativeCredentialOrigin,
    *,
    product_profile: Literal["default", "opt-in-product"] = "default",
) -> OpenAIRejectedTokenGap:
    """Call actual native admission; absence cannot authorize a legacy submit.

    No form is minted, no entered token is transmitted, and no SQL is executed.
    Once the product accepts MCP offers, this diagnostic refuses until a genuine
    submit, rejecting probe and scoped parameterized readback are bound.
    """
    validate_openai_new27(plan)
    if product_profile != "opt-in-product":
        raise ValueError("NEW27 OpenAI observation requires the opt-in profile")
    if adapter.operator_enabled is not True or adapter.delivery is None:
        raise UnsupportedCapability(("openai_native_credential_delivery",), PROFILE)
    if adapter.host.backend.capabilities().profile_id != PROFILE:
        raise UnsupportedCapability(("openai_native_credential_profile",), PROFILE)
    require_owned_sessionmaker(adapter.sessions)
    try:
        # Exercise the public runtime refusal for the absent kind. The cast
        # deliberately crosses the unsupported-kind boundary, not its guard.
        await adapter.offer(origin, kind=cast(Literal["github", "env"], "mcp"))
    except ValueError as error:
        if error.args != ("this native slice offers GitHub or an environment key only",):
            raise
    else:
        raise ValueError("native MCP support changed; bind real submit/probe/readback first")
    return OpenAIRejectedTokenGap(source_sha256=plan.scenario.sha256)  # pyright: ignore[reportArgumentType]


def openai_new27_pending_outcome(gap: OpenAIRejectedTokenGap) -> OutcomeReport:
    """Do not turn a product prerequisite refusal into the frozen db verdict."""
    gap = OpenAIRejectedTokenGap.model_validate_json(gap.model_dump_json())
    return OutcomeReport(
        scenario_id=gap.scenario_id,
        backend=gap.backend,
        checks=(CheckResult(kind="db_check", turn=None, status="PENDING", code=gap.code),),
    )
