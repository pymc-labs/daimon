"""Source-bound administrative NEW27 observation; no provider turn is implied."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import discord
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.discord_credential_conformance import (
    SOURCE_ASSERTION,
    SOURCE_SHA256,
    LoopbackRejectingProbe,
    RejectedTokenReceipt,
    rejected_token_submit,
)
from daimon.testing.outcome_oracle import CheckResult, EvidenceModel, OutcomeReport
from pydantic import Field, model_validator

if __package__:
    from .catalog_runner import ScenarioPlan, sha
    from .catalog_steps import validate_step_sources
else:
    from catalog_runner import ScenarioPlan, sha
    from catalog_steps import validate_step_sources

SCENARIO = "QA-NEW27-MISTYPED-TOKEN-HEADLESS"
HOOK_HEAD = "8629bad435485b1980db4688b0d9c256364684b6"
MAPPING_NOTE = "20261010T195610Z-N7-N3-N9-N5-C2-BINDING-PIN-NOTE.md"
MAPPING_NOTE_SHA256 = "313d5059d5c1c07c43824f284c36f50c73e9db178c3ada02e61887abe00c7ae8"
PROJECTION_SHA256 = "21dbb0f275e8770c69a2896affa7755bf74a118bb91f8a63b8e76bb4f8c3310e"
DOCUMENT_PINS = {
    SOURCE_SHA256: "cc789be831f02d0c585032e8818c565de9d40e546b3cd3dbbe3fd50724c5c9a1",
    PROJECTION_SHA256: "a139bf8465206eab4a1306d1aa989ab35cfedd4a3bd1d8f90bff23945c6c1419",
}
EXPECTED_STEP = {
    "do": "admin",
    "tool": "headless",
    "args": {
        "call": "credential_submit.mcp_token",
        "token": "wrong-token-123",
        "url": "{env.qa_token_mcp_url}",
    },
}
EXPECTED_ASSERTION = {"kind": "db_check", "sql": SOURCE_ASSERTION, "expect": True}


def validate_rejected_token_source(plan: ScenarioPlan) -> None:
    """Both original and independently pinned public projection preserve the check."""
    document = sha(json.dumps(plan.scenario.scenario, sort_keys=True, allow_nan=False).encode())
    if (
        plan.scenario.id != SCENARIO
        or plan.scenario.load_error is not None
        or DOCUMENT_PINS.get(plan.scenario.sha256) != document
        or plan.bindings
        or plan.global_assertions
        or plan.assertions != (EXPECTED_ASSERTION,)
        or len(plan.invocations) != 1
        or plan.invocations[0].location != "steps[0]"
        or plan.invocations[0].operation != "unbound"
        or plan.invocations[0].source != EXPECTED_STEP
        or any(
            getattr(plan.invocations[0], field) is not None
            for field in ("turn", "channel", "thread", "text", "fixture", "fixture_sha256")
        )
    ):
        raise ValueError("rejected-token binding differs from pinned administrative source")
    validate_step_sources(plan)


class RejectedTokenRun(EvidenceModel):
    scenario_id: Literal["QA-NEW27-MISTYPED-TOKEN-HEADLESS"] = SCENARIO
    backend: Literal["anthropic"] = "anthropic"
    source_sha256: Literal[
        "5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd",
        "21dbb0f275e8770c69a2896affa7755bf74a118bb91f8a63b8e76bb4f8c3310e",
    ]
    hook_head: Literal["8629bad435485b1980db4688b0d9c256364684b6"] = HOOK_HEAD
    mapping_note_sha256: Literal[
        "313d5059d5c1c07c43824f284c36f50c73e9db178c3ada02e61887abe00c7ae8"
    ] = MAPPING_NOTE_SHA256
    location: Literal["steps[0]"] = "steps[0]"
    assertion_index: Literal[0] = 0
    source_sql: Literal[
        "select used_at is null from credential_requests where id = '{headless.request_id}'"
    ] = SOURCE_ASSERTION
    expected_unused: Literal[True] = True
    request_token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submitted_token_sha256: Literal[
        "38fbbc225090619cf6b544ee5e910f18f556200ebb9929fab0e3cdafaa57b285"
    ]
    started_s: float = Field(ge=0)
    probe_s: float = Field(ge=0)
    finished_s: float = Field(ge=0)
    receipt: RejectedTokenReceipt

    @model_validator(mode="after")
    def measured_source(self) -> RejectedTokenRun:
        receipt = self.receipt
        if not self.started_s <= self.probe_s <= self.finished_s:
            raise ValueError("rejected-token probe is outside the administrative window")
        if receipt.request_token_sha256 != self.request_token_sha256:
            raise ValueError("rejected-token readback belongs to a foreign request")
        if (
            any(
                read.unused != (read.used_at is None)
                for read in (receipt.before, receipt.at_probe, receipt.after)
            )
            or receipt.unused_assertion != receipt.after.unused
        ):
            raise ValueError("rejected-token predicate differs from actual readback")
        if not receipt.before.unused or receipt.before.outcome is not None:
            raise ValueError("rejected-token binding requires a fresh unused form")
        if receipt.after.outcome != "token_rejected":
            raise ValueError("rejected-token submit lacks its actual refusal readback")
        # This mapped readback is diagnostic; it cannot validate the frozen SQL.
        if receipt.after.used_at is not None and receipt.at_probe.used_at != receipt.after.used_at:
            raise ValueError("rejected-token readbacks changed across the rejecting probe")
        return self


@dataclass(frozen=True)
class RejectedTokenBinding:
    """Explicit owned runtime/form/interaction and entered fresh N7 loopback probe."""

    runtime: DiscordRuntime
    row: CredentialRequestRow
    interaction: discord.Interaction[Any]
    probe: LoopbackRejectingProbe

    async def execute(self, plan: ScenarioPlan) -> RejectedTokenRun:
        validate_rejected_token_source(plan)
        if plan.backend != "anthropic":
            raise ValueError("rejected-token administrative binding is Anthropic-only (A3)")
        started = time.monotonic()
        receipt = await rejected_token_submit(
            runtime=self.runtime,
            row=self.row,
            interaction=self.interaction,
            token_value="wrong-token-123",
            probe=self.probe,
        )
        # Revalidate the hook contract even if supplied with model_copy elsewhere.
        receipt = RejectedTokenReceipt.model_validate_json(receipt.model_dump_json())
        post = self.probe.posts[0]
        return RejectedTokenRun(
            source_sha256=plan.scenario.sha256,  # pyright: ignore[reportArgumentType]
            request_token_sha256=hashlib.sha256(self.row.token.encode()).hexdigest(),
            submitted_token_sha256=post.token_sha256,  # pyright: ignore[reportArgumentType]
            started_s=started,
            probe_s=post.observed_s,
            finished_s=time.monotonic(),
            receipt=receipt,
        )


def rejected_token_outcome(run: RejectedTokenRun) -> OutcomeReport:
    run = RejectedTokenRun.model_validate_json(run.model_dump_json())
    unused = run.receipt.after.used_at is None
    return OutcomeReport(
        scenario_id=run.scenario_id,
        backend=run.backend,
        checks=(
            CheckResult(
                kind="db_check",
                turn=None,
                status="PASS" if unused else "FAIL",
                code="CREDENTIAL_REQUEST_UNUSED" if unused else "CREDENTIAL_REQUEST_CONSUMED",
                evidence_ids=(run.request_token_sha256,),
            ),
        ),
    )


def rejected_token_host_outcome(run: RejectedTokenRun) -> OutcomeReport:
    """Administrative coverage alone does not prove a provider host turn."""
    return OutcomeReport(
        scenario_id=run.scenario_id,
        backend=run.backend,
        checks=(
            CheckResult(kind="provider_turn", turn=None, status="PENDING", code="NO_PROVIDER_TURN"),
            CheckResult(kind="db_check", turn=None, status="PENDING", code="SOURCE_SQL_INVALID"),
        ),
    )
