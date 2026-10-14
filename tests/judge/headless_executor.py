"""Offline catalog executor over the real mux turn driver and N3 outcome oracle.

Authored SDK tapes test host behavior. They do not certify model behavior, remote
files/tool execution, platform delivery, or production admission/billing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from daimon.core.channel_backend import check_backend
from daimon.core.errors import TurnError
from daimon.core.mux_backend import TurnBackendRequest, turn_backend
from daimon.core.turn.driver import run_turn
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.anthropic_fixtures import load_anthropic_fixtures
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.host_clock import current_host_clock
from daimon.testing.http_observation import (
    HTTP_SOURCE_ASSERTIONS,
    AdminHTTPRun,
    HTTPObservation,
    HTTPObserver,
    http_outcome,
    offline_http_apps,
)
from daimon.testing.ma import send_events_response
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.outcome_oracle import (
    CheckResult,
    OutcomeReport,
    RunEvidence,
    SessionUse,
    Status,
    TerminalEvidence,
    TurnEvidence,
    VisibleText,
    evaluate,
)
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.product_profiles import (
    DEFAULT_PRODUCT_PROFILE,
    OPT_IN_PASS,
    OPT_IN_PRODUCT_PROFILE,
    ProductProfileName,
    ProductProfileUnavailable,
    QAProductProfile,
    applied_product_flags,
    current_product_profile,
    use_product_profile,
)
from daimon.testing.provider_fixtures import (
    BackendFixture,
    FixtureGap,
    ScenarioFixture,
    load_provider_fixtures,
)
from daimon.testing.turn_clock import ClockedLifecycle, NativeRoot, await_root_completion
from mux.contracts.events import TurnEndedPayload
from mux.contracts.ids import Provider, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.anthropic.resources.session_tools import SessionTools
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from pydantic import Field, JsonValue, computed_field, model_validator

if __package__:
    from .anthropic_catalog_replay import AnthropicRecipe, AnthropicTurnSource
    from .catalog_product_context import ProductContext
    from .catalog_runner import (
        FROZEN_TARGET_SHA256,
        PROVIDERS,
        CatalogMatrix,
        Invocation,
        Record,
        ScenarioPlan,
        build_matrix,
        expand_text,
        sha,
    )
    from .catalog_steps import (
        AdminStepBinding,
        ExecutionGap,
        StepReceipt,
        http_only_source,
        run_steps,
        step_gaps,
    )
    from .discord_catalog_surface import bind_discord_text
    from .headless_output import HeadlessOutput, capture_headless_return
    from .platform_catalog_surface import (
        CatalogLifecycle,
        bind_platform_text,
        evaluate_platform_catalog,
    )
    from .rejected_token_catalog import (
        SCENARIO as REJECTED_TOKEN_SCENARIO,
    )
    from .rejected_token_catalog import (
        RejectedTokenBinding,
        RejectedTokenRun,
        rejected_token_host_outcome,
        rejected_token_outcome,
    )
else:
    from anthropic_catalog_replay import AnthropicRecipe, AnthropicTurnSource
    from catalog_product_context import ProductContext
    from catalog_runner import (
        FROZEN_TARGET_SHA256,
        PROVIDERS,
        CatalogMatrix,
        Invocation,
        Record,
        ScenarioPlan,
        build_matrix,
        expand_text,
        sha,
    )
    from catalog_steps import (
        AdminStepBinding,
        ExecutionGap,
        StepReceipt,
        http_only_source,
        run_steps,
        step_gaps,
    )
    from discord_catalog_surface import bind_discord_text
    from headless_output import HeadlessOutput, capture_headless_return
    from platform_catalog_surface import (
        CatalogLifecycle,
        bind_platform_text,
        evaluate_platform_catalog,
    )
    from rejected_token_catalog import SCENARIO as REJECTED_TOKEN_SCENARIO
    from rejected_token_catalog import (
        RejectedTokenBinding,
        RejectedTokenRun,
        rejected_token_host_outcome,
        rejected_token_outcome,
    )


TABLE_SCENARIO = "QA-NEW37-MARKDOWN-TABLE-READABLE"
TABLE_SOURCE_SHA256 = "9f12f8845f03468665c1bc13a86122ff2922fd3bf59dd8d02a0387da7831a2ca"
TABLE_PATTERN = r"(?m)^\s*\|.*\|\s*$"


class ExpectedFailure(Record):
    code: Literal["DISCORD_TABLE_RENDERING_DISABLED"] = "DISCORD_TABLE_RENDERING_DISABLED"
    source_sha256: Literal["9f12f8845f03468665c1bc13a86122ff2922fd3bf59dd8d02a0387da7831a2ca"] = (
        TABLE_SOURCE_SHA256
    )
    assertion_index: Literal[0] = 0
    diagnostic: Literal["FORBIDDEN_VISIBLE_TEXT"] = "FORBIDDEN_VISIBLE_TEXT"


def _expected_failure(plan: ScenarioPlan) -> ExpectedFailure | None:
    if (
        plan.scenario.id == TABLE_SCENARIO
        and plan.scenario.sha256 == TABLE_SOURCE_SHA256
        and plan.scenario.scenario.get("surface") == "discord"
        and str(plan.scenario.scenario.get("notes", "")).startswith(
            "Expected FAIL while table_rendering defaults off."
        )
        and plan.assertions
        and plan.assertions[0] == {"kind": "text_absent", "turn": 1, "pattern": TABLE_PATTERN}
    ):
        # CatalogLifecycle explicitly uses the unchanged render_tables=False default.
        return ExpectedFailure()
    return None


def _equivalent_table_control(text: str) -> bool:
    """Qualify authored comparison input, independently of the readability oracle.

    This is an offline fixture check, not a model-response requirement. A matched
    control supplies the requested three columns and four rows as a Markdown
    table. A regex miss on a malformed/wrong-column fixture earns no PASS.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 6 or any(not line.startswith("|") or not line.endswith("|") for line in lines):
        return False
    cells = [[cell.strip() for cell in line[1:-1].split("|")] for line in lines]
    if any(len(row) != 3 or any(not cell for cell in row) for row in cells):
        return False
    header = [cell.casefold() for cell in cells[0]]
    return (
        header[0] == "fruit"
        and header[1] in {"colour", "color"}
        and header[2] == "price"
        and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells[1])
    )


class ReplayTurn(Record):
    turn: int = Field(ge=1)
    request_text: str
    events: tuple[dict[str, JsonValue], ...]

    @model_validator(mode="after")
    def native_identity(self) -> ReplayTurn:
        ids = [event.get("id") for event in self.events]
        if not ids or any(not isinstance(id_, str) or not id_ for id_ in ids):
            raise ValueError("replay needs nonempty native record IDs")
        if len(set(cast(list[str], ids))) != len(ids):
            raise ValueError("replay native record IDs must be unique")
        first = self.events[0]
        if first.get("type") != "user.message" or first.get("content") != [
            {"type": "text", "text": self.request_text}
        ]:
            raise ValueError("replay must begin with the submitted user message")
        return self


def _equivalent_table_replay(tape: ReplayTurn) -> bool:
    parts: list[str] = []
    for event in tape.events:
        if event.get("type") == "agent.tool_use":
            return False
        if event.get("type") != "agent.message":
            continue
        content = event.get("content")
        if not isinstance(content, list):
            return False
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                return False
            text = part.get("text")
            if not isinstance(text, str):
                return False
            parts.append(text)
    return _equivalent_table_control("\n".join(parts))


class ScenarioReplay(Record):
    scenario_id: str
    scenario_sha256: str
    backend: Literal["anthropic"] = "anthropic"
    profile: Literal["anthropic.managed_agents"] = "anthropic.managed_agents"
    agent_model: Literal["claude-haiku-5-5"] = "claude-haiku-5-5"
    # Explicit fixture values only. No environment/credential lookup occurs.
    values: dict[str, str] = {}
    turns: tuple[ReplayTurn, ...]

    @model_validator(mode="after")
    def unique_turns(self) -> ScenarioReplay:
        if len({turn.turn for turn in self.turns}) != len(self.turns):
            raise ValueError("repeated replay turn")
        ids = [event["id"] for turn in self.turns for event in turn.events]
        if len(set(cast(list[str], ids))) != len(ids):
            raise ValueError("replay native record IDs must be unique across turns")
        return self


class CellResult(Record):
    scenario_id: str
    backend: Literal["anthropic", "openai", "gemini"]
    scored: bool
    source_sha256: str
    product_profile: ProductProfileName = "default"
    profile_state: Literal["executed", "unavailable", "unbound"] = "executed"
    mode: Literal["offline_scripted", "offline_admin_http", "offline_admin_credential"] = (
        "offline_scripted"
    )
    admin_http: AdminHTTPRun | None = None
    admin_credential: RejectedTokenRun | None = Field(default=None, exclude_if=lambda v: v is None)
    gaps: tuple[ExecutionGap, ...]
    evidence: RunEvidence
    catalog_outcome: OutcomeReport
    host_outcome: OutcomeReport
    request_count: int = 0
    # Compatibility view of actual headless returns; never render snapshots.
    rendered_text: tuple[str, ...] = ()
    headless_outputs: tuple[HeadlessOutput, ...] = ()
    discord_surfaces: tuple[DiscordSurfaceCapture, ...] = ()
    expected_failure: ExpectedFailure | None = None
    platform_surfaces: tuple[PlatformSurfaceCapture, ...] = ()
    step_receipts: tuple[StepReceipt, ...] = ()
    input_provenance: str = "catalog-authored"

    @computed_field
    @property
    def status(self) -> Status:
        if self.catalog_outcome.status == "FAIL" or self.host_outcome.status == "FAIL":
            return "FAIL"
        if self.mode == "offline_admin_credential" and self.host_outcome.status == "PENDING":
            return "PENDING"
        if self.gaps:
            return "PENDING"
        return self.catalog_outcome.status

    @model_validator(mode="after")
    def expected_failure_source(self) -> CellResult:
        if self.expected_failure is not None and (
            self.scenario_id != TABLE_SCENARIO
            or self.source_sha256 != self.expected_failure.source_sha256
        ):
            raise ValueError("expected failure differs from reviewed catalog source")
        return self

    @model_validator(mode="after")
    def administration_is_not_a_turn(self) -> CellResult:
        admin = self.admin_http
        if admin is None:
            if self.mode == "offline_admin_http":
                raise ValueError("HTTP administration needs its actual observation envelope")
            return self
        if (
            self.mode != "offline_admin_http"
            or (admin.scenario_id, admin.backend, admin.source_sha256)
            != (self.scenario_id, self.backend, self.source_sha256)
            or (self.evidence.scenario_id, self.evidence.backend)
            != (self.scenario_id, self.backend)
            or self.evidence.turns
            or self.step_receipts
            or self.headless_outputs
            or self.discord_surfaces
            or self.platform_surfaces
            or self.rendered_text
            or self.expected_failure is not None
            or self.request_count != len(admin.observations)
            or self.catalog_outcome != http_outcome(admin)
            or self.host_outcome != admin_source_outcome(admin)
        ):
            raise ValueError("HTTP administration cannot claim or replace turn evidence")
        return self

    @model_validator(mode="after")
    def credential_administration_is_not_a_turn(self) -> CellResult:
        admin = self.admin_credential
        if admin is None:
            if self.mode == "offline_admin_credential":
                raise ValueError("credential administration needs its actual observation envelope")
            return self
        if (
            self.mode != "offline_admin_credential"
            or (self.scenario_id, self.backend, self.source_sha256)
            != (admin.scenario_id, admin.backend, admin.source_sha256)
            or (self.evidence.scenario_id, self.evidence.backend)
            != (self.scenario_id, self.backend)
            or self.evidence.turns
            or self.gaps
            or self.step_receipts
            or self.headless_outputs
            or self.discord_surfaces
            or self.platform_surfaces
            or self.rendered_text
            or self.expected_failure is not None
            or self.request_count != 0
            or self.catalog_outcome != rejected_token_outcome(admin)
            or self.host_outcome != rejected_token_host_outcome(admin)
        ):
            raise ValueError("credential administration cannot claim or replace turn evidence")
        return self

    @computed_field
    @property
    def reported_status(
        self,
    ) -> Literal["PASS", "PASS under opt-in profile", "FAIL", "EXPECTED-FAIL", "PENDING"]:
        if self.product_profile == "opt-in-product" and self.profile_state != "executed":
            return "PENDING"
        # The mapped readback is diagnostic; the frozen invalid SQL earns no cell credit.
        if self.mode == "offline_admin_credential" and self.host_outcome.status == "PENDING":
            return "PENDING"
        failures = [
            i for i, check in enumerate(self.catalog_outcome.checks) if check.status == "FAIL"
        ]
        if (
            self.status == "FAIL"
            and self.expected_failure is not None
            and self.host_outcome.status == "PASS"
            and not self.gaps
            and failures == [self.expected_failure.assertion_index]
            and self.catalog_outcome.checks[0].code == self.expected_failure.diagnostic
            and self.catalog_outcome.checks[0].kind == "text_absent"
            and self.catalog_outcome.checks[0].turn == 1
            and self.evidence.turns
            and all(t.text_capture_complete for t in self.evidence.turns)
            and self.discord_surfaces
        ):
            return "EXPECTED-FAIL"
        if self.status == "PASS" and self.product_profile == "opt-in-product":
            return OPT_IN_PASS
        return self.status


class ExecutionReport(Record):
    version: Literal[1] = 1
    integration_sha: str
    target_sha256: str
    target_ids: tuple[str, ...]
    scored_denominator: Literal[159] = 159
    cells: tuple[CellResult, ...]
    product_profile: ProductProfileName = "default"
    profile_state: Literal["executed", "unavailable", "unbound"] = "executed"
    applied_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def frozen_cells(self) -> ExecutionReport:
        self.require_frozen_cells()
        return self

    def require_frozen_cells(self) -> None:
        """Validate copied boards too, without invoking a decorated validator."""
        canonical = sha(("\n".join(self.target_ids) + "\n").encode())
        if self.target_sha256 != FROZEN_TARGET_SHA256 or canonical != self.target_sha256:
            raise ValueError("execution report differs from frozen TARGET-53")
        pairs = {(cell.scenario_id, cell.backend) for cell in self.cells}
        expected = {(id_, backend) for id_ in self.target_ids for backend in PROVIDERS}
        if (
            len(self.target_ids) != 53
            or len(self.cells) != 159
            or pairs != expected
            or any(not cell.scored for cell in self.cells)
            or any(cell.product_profile != self.product_profile for cell in self.cells)
            or any(cell.profile_state != self.profile_state for cell in self.cells)
        ):
            raise ValueError("execution report must retain all 159 frozen scored cells")
        if (
            self.product_profile == "opt-in-product"
            and self.profile_state == "executed"
            and (
                len(self.applied_flags) != len(OPT_IN_PRODUCT_PROFILE.flags)
                or set(self.applied_flags) != set(OPT_IN_PRODUCT_PROFILE.flags)
            )
        ):
            raise ValueError(
                "executed opt-in profile requires all declared explicit flag applications"
            )
        if self.profile_state == "unavailable" and any(
            c.reported_status != "PENDING" or c.request_count or c.evidence.turns
            for c in self.cells
        ):
            raise ValueError("unavailable profile cannot claim executed evidence or a verdict")

    @computed_field
    @property
    def scored_counts(self) -> dict[str, int]:
        return {
            status: sum(c.scored and c.reported_status == status for c in self.cells)
            for status in (
                OPT_IN_PASS if self.product_profile == "opt-in-product" else "PASS",
                "FAIL",
                "EXPECTED-FAIL",
                "PENDING",
            )
        }


class ProductProfileReport(Record):
    """Two independent 159-cell boards; opt-in passes cannot increase default counts."""

    version: Literal[1] = 1
    default_board: ExecutionReport
    opt_in_board: ExecutionReport

    @model_validator(mode="after")
    def same_frozen_matrix(self) -> ProductProfileReport:
        a, b = self.default_board, self.opt_in_board
        a.require_frozen_cells()
        b.require_frozen_cells()
        if (
            a.product_profile != "default"
            or b.product_profile != "opt-in-product"
            or a.integration_sha != b.integration_sha
            or a.target_ids != b.target_ids
            or a.target_sha256 != b.target_sha256
            or {(c.scenario_id, c.backend, c.source_sha256) for c in a.cells}
            != {(c.scenario_id, c.backend, c.source_sha256) for c in b.cells}
        ):
            raise ValueError("product profile boards differ from the same frozen matrix")
        return self

    @computed_field
    @property
    def integration_sha(self) -> str:
        return self.default_board.integration_sha

    @computed_field
    @property
    def target_sha256(self) -> str:
        return self.default_board.target_sha256

    @computed_field
    @property
    def target_ids(self) -> tuple[str, ...]:
        return self.default_board.target_ids

    @computed_field
    @property
    def scored_denominator(self) -> Literal[159]:
        return 159

    @computed_field
    @property
    def profiles(self) -> dict[str, dict[str, bool]]:
        return {p.name: p.flags for p in (DEFAULT_PRODUCT_PROFILE, OPT_IN_PRODUCT_PROFILE)}

    @computed_field
    @property
    def scored_counts(self) -> dict[str, int]:
        return self.default_board.scored_counts

    @computed_field
    @property
    def opt_in_scored_counts(self) -> dict[str, int]:
        return self.opt_in_board.scored_counts

    @computed_field
    @property
    def cells(self) -> tuple[dict[str, Any], ...]:
        other = {(c.scenario_id, c.backend): c for c in self.opt_in_board.cells}
        return tuple(
            {
                **c.model_dump(mode="json"),
                "profiles": {
                    "default": c.model_dump(mode="json"),
                    "opt-in-product": other[c.scenario_id, c.backend].model_dump(mode="json"),
                },
            }
            for c in self.default_board.cells
        )


def _assertions(plan: ScenarioPlan, values: dict[str, str]) -> list[dict[str, JsonValue]]:
    # Round-trip into the oracle's JSON contract, retaining every original check.
    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return expand_text(value, values)
        if isinstance(value, list):
            return [expand(item) for item in cast(list[Any], value)]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in cast(dict[str, Any], value).items()}
        return value

    return cast(list[dict[str, JsonValue]], expand([*plan.assertions, *plan.global_assertions]))


def _host_assertions(evidence: RunEvidence) -> list[dict[str, JsonValue]]:
    checks: list[dict[str, JsonValue]] = []
    previous: dict[str, int] = {}
    for turn in evidence.turns:
        checks.extend(
            [
                {"kind": "turn_completed", "turn": turn.turn},
                {"kind": "no_session_replacement", "turn": turn.turn},
            ]
        )
        if turn.slot_id in previous:
            checks.append(
                {"kind": "same_session", "turn": turn.turn, "previous_turn": previous[turn.slot_id]}
            )
        previous[turn.slot_id] = turn.turn
    return checks


def _unbound_steps(plan: ScenarioPlan) -> tuple[ExecutionGap, ...]:
    return step_gaps(plan)


async def _run_turn(
    plan: ScenarioPlan,
    invocation: Invocation,
    tape: ReplayTurn,
    store: MemoryStateStore | None,
    binding: ProviderBinding | None,
    scope: Scope | None,
    timeout_s: float = 10.0,
    *,
    native: AnthropicTurnSource | None = None,
    product_context: ProductContext | None = None,
) -> tuple[
    TurnEvidence,
    int,
    str,
    HeadlessOutput | None,
    DiscordSurfaceCapture | None,
    PlatformSurfaceCapture | None,
]:
    """Capture journal authority and all selections in this owned host window."""
    if product_context is not None:
        product_capture = await product_context.run(plan, invocation, native, timeout_s)
        return (
            product_capture.evidence,
            product_capture.request_count,
            product_capture.output.text if product_capture.output else "",
            product_capture.output,
            product_capture.lifecycle.capture,
            product_capture.lifecycle.platform_capture,
        )
    if store is None or binding is None or scope is None:
        raise ValueError("scripted host requires a selected binding and scope")
    if invocation.turn is None:
        raise ValueError("host invocation has no catalog turn")
    channel = next(b for b in plan.bindings if b.ref == invocation.channel)
    if await store.latest_config_revision(channel.revision.channel) != channel.revision:
        raise ValueError("persisted channel revision differs from selected backend")
    check_backend(channel.revision)
    selected = await store.get_binding(binding_slot(binding))
    if selected != binding:
        raise ValueError("selected persisted binding changed")
    session = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="anthropic",
        account_scope_id="offline-scripted",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    key = f"host-turn-{invocation.turn}"
    persistence = TurnPersistence(store, binding, scope, operation_key=key)
    transport = ScriptedTransport()
    if native is None:
        # Verify the prepared session's frozen agent model through real SDK parsing.
        native_session = ma_session(id=session.id, model=channel.agent_model)
        transport.queue(
            ScriptedReply(
                "GET",
                f"/v1/sessions/{session.id}",
                httpx.Response(200, json=native_session.model_dump(mode="json")),
            ),
            ScriptedReply.stream(
                f"/v1/sessions/{session.id}/events/stream",
                [cast(dict[str, object], event) for event in tape.events],
            ),
            ScriptedReply(
                "POST",
                f"/v1/sessions/{session.id}/events",
                send_events_response([tape.events[0]]),
                request_json={
                    "events": [
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": tape.request_text}],
                        }
                    ]
                },
                check_json=True,
            ),
        )
    clock = current_host_clock()
    started = clock()
    root = cast(str, tape.events[0]["id"])
    output_id = f"{plan.scenario.id}:{plan.backend}:{session.id}:{root}:headless_return"
    surface_id = output_id.replace(
        ":headless_return", f":{plan.scenario.scenario.get('surface')}_adapter_posts"
    )
    lifecycle = CatalogLifecycle(
        surface=str(plan.scenario.scenario.get("surface")),
        evidence_id=surface_id,
        session_id=session.id,
        root_turn_id=root,
        model_id=channel.agent_model,
    )
    clocked = ClockedLifecycle(lifecycle, clock=clock, started_s=started)
    output: HeadlessOutput | None = None
    states: list[TurnState] = []
    journal_before = len(await store.read_events(session.id))

    async def native_root() -> NativeRoot:
        operation = await store.get_operation(scope, f"{key}:send:0")
        if (
            operation is None
            or not operation.result
            or (operation.result.get("input_ids") != [root])
        ):
            raise ValueError("root is not the acknowledged submitted input")
        journal = (await store.read_events(session.id))[journal_before:]
        if any(event.turn_id is not None and event.turn_id != root for event in journal):
            raise ValueError("journal record belongs to another root")
        if await store.get_binding(binding_slot(binding)) != binding:
            raise ValueError("native completion binding changed")
        return NativeRoot(session, root, journal)

    async with AsyncExitStack() as clients:
        native_replay = (
            await clients.enter_async_context(native.client(session.id)) if native else None
        )
        client = (
            native_replay.client
            if native_replay is not None
            else await clients.enter_async_context(transport.client())
        )
        request = TurnBackendRequest(
            profile=channel.revision.profile,
            client=client,
            scope=scope,
            session_id=session.id,
            session=session,
            config=channel.revision,
            read_timeout_s=5.0,
        )
        backend = turn_backend(request)
        snapshot = await backend.backend.extension(
            SessionTools, namespace="anthropic.session_tools", version=1
        ).retrieve(scope, session)
        # The SDK session builder/parser validates this fixture; still check the exact pin.
        agent = snapshot.native.get("agent")
        model = agent.get("model") if isinstance(agent, dict) else None
        model_id = model.get("id") if isinstance(model, dict) else None
        if snapshot.native.get("id") != session.id or model_id != channel.agent_model:
            raise ValueError("prepared session differs from selected model/session")
        with persistence.activate():

            async def invoke() -> str:
                # This executor is the selected headless host. Like the real
                # headless runner, its caller receives only the final response
                # after the last tool, matching the complete production return.
                state = await await_root_completion(
                    run_turn(
                        anthropic=client,
                        session_id=session.id,
                        user_message=tape.request_text,
                        lifecycle=clocked,
                        cancel=asyncio.Event(),
                        billing=BillingExempt(reason="headless-unrecorded"),
                        path="mux",
                        scope=scope,
                        session_ref=session,
                        profile=channel.revision.profile,
                        backend_request=request,
                        stream_read_timeout_s=5.0,
                        render_interval_s=3600,
                    ),
                    lifecycle=clocked,
                    timeout_s=timeout_s,
                    native_root=native_root,
                )
                states.append(state)
                if state.error is not None:
                    raise state.error
                return extract_final_response(state.content)

            async def captured() -> None:
                nonlocal output
                output = await capture_headless_return(invoke, evidence_id=output_id, clock=clock)

            try:
                await lifecycle.run(captured)
            except TurnError as error:
                # A recorded native failure still belongs in the oracle. It
                # does not produce a successful return receipt or text coverage.
                if not states or states[-1].error is not error:
                    raise
    observed = clock()
    # Legacy ScriptedTransport uses bare asserts; native WireReplay is explicit.
    if native_replay is not None:
        native_replay.wire.assert_consumed()
    elif transport.violations or transport.replies:
        raise ValueError("scripted HTTP capture did not close cleanly")
    if [event.model_dump(mode="json").get("id") for event in lifecycle.sse_events] != [
        event["id"] for event in tape.events
    ]:
        raise ValueError("scripted SSE capture did not consume every record")
    # Normalize SDK-populated optional defaults through the same event schema.
    # Arbitrary JSON input values remain intact; only model fields omit None.
    if native is not None and any(
        event.model_dump(mode="json", exclude_none=True)
        != type(event).model_validate(expected).model_dump(mode="json", exclude_none=True)
        for event, expected in zip(lifecycle.sse_events, tape.events, strict=True)
    ):
        raise ValueError("native SSE payload differs from its pinned recipe")
    operation = await store.get_operation(scope, f"{key}:send:0")
    if operation is None or not operation.result or (operation.result.get("input_ids") != [root]):
        raise ValueError("root is not the acknowledged submitted input")
    if len(states) != 1:
        raise ValueError("headless invocation did not produce exactly one host state")
    state = states[0]
    if native is not None:
        native.verify_usage(state)
    if state.error is None and output is None:
        raise ValueError("successful headless invocation has no return receipt")
    if output is not None and (
        output.evidence_id != output_id
        or output.started_s < started
        or output.returned_s > observed
    ):
        raise ValueError("headless return differs from its owned capture window")
    journal = (await store.read_events(session.id))[journal_before:]
    if any(event.turn_id is not None and event.turn_id != root for event in journal):
        raise ValueError("journal record belongs to another root")
    completed = clocked.completion_time(session=session, root_turn_id=root, journal=journal)
    terminals: list[TerminalEvidence] = []
    for event in journal:
        if event.type == "session.turn_ended":
            payload = TurnEndedPayload.model_validate(event.payload)
            if event.turn_id != root or payload.root_turn_id != root:
                raise ValueError("journal terminal differs from the acknowledged root")
            terminals.append(
                TerminalEvidence(
                    evidence_id=f"{session.id}:{event.id}",
                    session_id=event.session_id,
                    root_turn_id=payload.root_turn_id,
                    authority=event.authority,
                    outcome=payload.outcome,
                    observed_s=completed,
                )
            )
    # Lifecycle closure is a second host observation; it cannot substitute for a journal terminal.
    if len(lifecycle.terminal_success) + len(lifecycle.terminal_failures) != 1:
        raise ValueError("host terminal lifecycle did not close once")
    if await store.get_binding(binding_slot(binding)) != binding:
        raise ValueError("persisted binding changed during the turn")
    evidence = TurnEvidence(
        turn=invocation.turn,
        slot_id=binding_slot(binding).model_dump_json(),
        session_id=session.id,
        root_turn_id=root,
        started_s=started,
        terminals=tuple(terminals),
        sessions=(SessionUse(evidence_id=f"{key}:selected", session_id=session.id),),
        terminal_capture_complete=not any(event.authority == "gap" for event in journal),
        # Only this executor writes bindings; it never calls recovery or replacement hooks.
        session_capture_complete=True,
        texts=(
            VisibleText(
                evidence_id=output.evidence_id,
                message_id=output.evidence_id,
                order=0,
                observed_s=output.returned_s,
                text=output.text,
            ),
        )
        if output is not None
        and output.has_visible_text
        and plan.scenario.scenario.get("surface") == "headless"
        else (),
        text_capture_complete=(
            output is not None and plan.scenario.scenario.get("surface") == "headless"
        ),
    )
    evidence = bind_discord_text(
        evidence,
        lifecycle.capture,
        expected_id=surface_id,
        observed_s=observed,
        accounting_required=any(
            a.get("kind") in {"footer_cost_matches_ledger", "ledger_matches_usage"}
            for a in plan.assertions
        ),
    )
    evidence = bind_platform_text(
        evidence,
        lifecycle.platform_capture,
        expected_id=surface_id,
        expected_platform=str(plan.scenario.scenario.get("surface")),
        observed_s=observed,
        accounting_required=any(
            a.get("kind") in {"footer_cost_matches_ledger", "ledger_matches_usage"}
            for a in plan.assertions
        ),
    )
    # Preserve recorded native failures, but never let a host failure PASS.
    if (state.error is not None or lifecycle.terminal_failures) and (
        not terminals or all(item.outcome == "completed" for item in terminals)
    ):
        raise ValueError("host failed despite a completed or missing journal terminal")
    return (
        evidence,
        len(native_replay.wire.requests) if native_replay is not None else len(transport.requests),
        output.text if output is not None else "",
        output,
        lifecycle.capture,
        lifecycle.platform_capture,
    )


async def execute_cell(
    plan: ScenarioPlan,
    replay: ScenarioReplay | None,
    *,
    native_gaps: tuple[FixtureGap, ...] = (),
    native_recipe: AnthropicRecipe | None = None,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
    rejected_token: RejectedTokenBinding | None = None,
) -> CellResult:
    if rejected_token is not None:
        if native_gaps or any(
            value is not None
            for value in (replay, native_recipe, admin, admin_values, product_context)
        ):
            raise ValueError("credential administration cannot replace selected turn inputs")
        observed = await rejected_token.execute(plan)
        return CellResult(
            scenario_id=plan.scenario.id,
            backend=plan.backend,
            scored=plan.scored,
            source_sha256=plan.scenario.sha256,
            mode="offline_admin_credential",
            admin_credential=observed,
            gaps=(),
            evidence=RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=()),
            catalog_outcome=rejected_token_outcome(observed),
            host_outcome=rejected_token_host_outcome(observed),
            input_provenance="real-modal-loopback-post-scoped-readback",
        )
    if native_recipe is not None:
        native_recipe.validate(plan)
        if replay != ScenarioReplay.model_validate(native_recipe.scenario.model_dump(mode="json")):
            raise ValueError("Anthropic execution differs from its selected native recipe")
        if native_gaps:
            raise ValueError("Anthropic native gaps must come from the selected recipe")
        native_gaps = native_recipe.execution_gaps
    gaps: list[ExecutionGap] = []
    if native_recipe is not None:
        gaps.extend(
            ExecutionGap(code="PROVIDER_" + g.code, location=g.location)
            for g in native_recipe.gaps
            if g.location in {"mcp", "deepwiki"}
        )
    evidence = RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=())
    values = replay.values if replay is not None else {}
    try:
        assertions = _assertions(plan, values)
    except ValueError:
        # Preserve unresolved predicates. They cannot confer a PASS.
        assertions = cast(list[dict[str, JsonValue]], [*plan.assertions, *plan.global_assertions])
        gaps.append(ExecutionGap(code="PLACEHOLDER_UNBOUND", location="assertions"))
    if plan.backend != "anthropic":
        gaps.append(
            ExecutionGap(
                code="OPENAI_FIXTURE_UNAVAILABLE"
                if plan.backend == "openai"
                else "GEMINI_FIXTURE_UNAVAILABLE",
                location="channel",
            )
        )
    else:
        if replay is None:
            gaps.extend(_unbound_steps(plan))
        if replay is None:
            gaps.append(ExecutionGap(code="REPLAY_FIXTURE_UNAVAILABLE", location="scenario"))
    if replay is not None and (
        replay.scenario_id != plan.scenario.id
        or replay.scenario_sha256 != plan.scenario.sha256
        or replay.backend != plan.backend
    ):
        raise ValueError("replay differs from the pinned catalog scenario/provider")
    host_invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    tapes: dict[int, ReplayTurn] = {}
    if replay is not None:
        tapes = {t.turn: t for t in replay.turns}
        if native_recipe is None and set(tapes) != {i.turn for i in host_invocations}:
            raise ValueError("replay must cover exactly the planned host turns")
        for invocation in host_invocations:
            if invocation.turn is None or invocation.text is None:
                raise ValueError("host invocation lacks turn/text")
            if invocation.turn not in tapes:
                continue  # Native recipe validation requires an explicit pinned prerequisite gap.
            if expand_text(invocation.text, replay.values) != tapes[invocation.turn].request_text:
                raise ValueError("replay request differs from the planned text")
    if replay is not None and plan.scenario.id == TABLE_SCENARIO:
        for tape in replay.turns:
            if not _equivalent_table_replay(tape):
                gaps.append(
                    ExecutionGap(
                        code="REPLAY_TABLE_CONTROL_NOT_EQUIVALENT",
                        location=f"replay.turn[{tape.turn}]",
                    )
                )
    turns: list[TurnEvidence] = []
    rendered: list[str] = []
    outputs: list[HeadlessOutput] = []
    surfaces: list[DiscordSurfaceCapture] = []
    platform_surfaces: list[PlatformSurfaceCapture] = []
    requests = 0
    step_receipts: tuple[StepReceipt, ...] = ()
    if replay is not None and not any(
        g.code == "REPLAY_TABLE_CONTROL_NOT_EQUIVALENT" for g in gaps
    ):
        store = MemoryStateStore() if product_context is None else None
        bindings: dict[tuple[str, str], ProviderBinding] = {}

        async def prepare_channel(invocation: Invocation) -> None:
            if product_context is not None:
                await product_context.configure(invocation)
                return
            channel = next(b for b in plan.bindings if b.ref == invocation.channel)
            check_backend(channel.revision)
            if store is None:
                raise ValueError("unbound scripted state")
            await store.put_config_revision(channel.revision)

        async def invoke(invocation: Invocation, timeout_s: float) -> TurnEvidence:
            nonlocal requests
            if invocation.turn not in tapes:
                raise ValueError("host reached an omitted native turn")
            if product_context is not None:
                result = await _run_turn(
                    plan,
                    invocation,
                    tapes[invocation.turn],
                    store,
                    bindings.get((invocation.channel or "", invocation.thread or "")),
                    None,
                    timeout_s,
                    native=native_recipe.turn(invocation.turn) if native_recipe else None,
                    product_context=product_context,
                )
                turn, count, text, output, surface, platform_surface = result
                turns.append(turn)
                requests += count
                rendered.append(text)
                if output is not None:
                    outputs.append(output)
                if surface is not None:
                    surfaces.append(surface)
                if platform_surface is not None:
                    platform_surfaces.append(platform_surface)
                return turn
            if store is None:
                raise ValueError("unbound scripted state")
            channel = next(b for b in plan.bindings if b.ref == invocation.channel)
            scope = Scope(
                tenant_id=channel.revision.channel.tenant_id,
                account_id="qa-caller",
                principal_id="offline-catalog-host",
                authorization_id="scripted-fixture",
            )
            slot = (channel.ref, invocation.thread or "")
            if slot not in bindings:
                binding = ProviderBinding(
                    id=f"binding-{len(bindings)}",
                    thread=ThreadRef(
                        channel=channel.revision.channel, thread_id=invocation.thread or ""
                    ),
                    provider="anthropic",
                    profile=channel.revision.profile,
                    native_refs={
                        "session": native_recipe.session_id(invocation.turn)
                        if native_recipe is not None
                        else f"session-{len(bindings)}"
                    },
                    generation=1,
                    config_revision=channel.revision.local,
                    legacy_account_id=scope.account_id,
                )
                await store.put_binding(binding, expected_generation=0)
                bindings[slot] = binding
            turn, count, text, output, surface, platform_surface = await _run_turn(
                plan,
                invocation,
                tapes[invocation.turn],
                store,
                bindings[slot],
                scope,
                timeout_s,
                native=native_recipe.turn(invocation.turn) if native_recipe else None,
            )
            if surface is not None:
                surfaces.append(surface)
            if platform_surface is not None:
                platform_surfaces.append(platform_surface)
            turns.append(turn)
            requests += count
            rendered.append(text)
            if output is not None:
                outputs.append(output)
            return turn

        steps = await run_steps(
            plan,
            invoke=invoke,
            prepare_channel=prepare_channel,
            native_gaps=native_gaps,
            admin=admin,
            admin_values=admin_values,
            product_context=product_context,
        )
        gaps.extend(steps.gaps)
        step_receipts = steps.receipts
        evidence = RunEvidence(
            scenario_id=plan.scenario.id, backend=plan.backend, turns=tuple(turns)
        )
    gaps.extend(
        ExecutionGap(code="CATALOG_CAPABILITY_PENDING", location=gap.location) for gap in plan.gaps
    )
    return CellResult(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        scored=plan.scored,
        source_sha256=plan.scenario.sha256,
        expected_failure=_expected_failure(plan),
        gaps=tuple(gaps),
        evidence=evidence,
        catalog_outcome=evaluate_platform_catalog(evidence, assertions, tuple(platform_surfaces)),
        host_outcome=evaluate(evidence, _host_assertions(evidence)),
        request_count=requests,
        rendered_text=tuple(rendered),
        headless_outputs=tuple(outputs),
        discord_surfaces=tuple(surfaces),
        platform_surfaces=tuple(platform_surfaces),
        step_receipts=step_receipts,
        input_provenance="authored-sdk-wire"
        if native_recipe is not None
        else "catalog-authored"
        if replay
        else "missing",
    )


async def execute_openai_cell(
    plan: ScenarioPlan,
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    *,
    source_root: Path,
    repository_root: Path,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
) -> CellResult:
    if (
        plan.backend != "openai"
        or fixture.backend != "openai"
        or scenario.scenario_id != plan.scenario.id
        or scenario.source.sha256 != plan.scenario.sha256
        or scenario.source.path != f"scenarios/{plan.scenario.filename}"
        or fixture not in scenario.providers
        or fixture.profile != "openai.persistent_workspace"
        or fixture.model != "gpt-6-luna"
    ):
        raise ValueError(
            "OpenAI executor requires the pinned scenario and explicit OpenAI selection"
        )
    gaps: list[ExecutionGap] = []
    try:
        assertions = _assertions(plan, scenario.values)
    except ValueError:
        assertions = cast(list[dict[str, JsonValue]], [*plan.assertions, *plan.global_assertions])
        gaps.append(ExecutionGap(code="PLACEHOLDER_UNBOUND", location="assertions"))
    evidence = RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=())
    outputs: tuple[HeadlessOutput, ...] = ()
    surfaces: tuple[DiscordSurfaceCapture, ...] = ()
    platform_surfaces: tuple[PlatformSurfaceCapture, ...] = ()
    rendered: tuple[str, ...] = ()
    requests = 0
    if __package__:
        from .openai_catalog_replay import execute_openai_turns
    else:
        from openai_catalog_replay import execute_openai_turns

    capture = await execute_openai_turns(
        plan,
        scenario,
        fixture,
        source_root=source_root,
        repository_root=repository_root,
        admin=admin,
        admin_values=admin_values,
        product_context=product_context,
    )
    evidence = RunEvidence(
        scenario_id=plan.scenario.id,
        backend="openai",
        turns=tuple(t.evidence for t in capture.turns),
    )
    outputs = tuple(t.output for t in capture.turns if t.output is not None)
    surfaces = tuple(t.surface for t in capture.turns if t.surface is not None)
    platform_surfaces = tuple(
        t.platform_surface for t in capture.turns if t.platform_surface is not None
    )
    rendered = tuple(t.output.text if t.output is not None else "" for t in capture.turns)
    requests = capture.request_count
    gaps.extend(capture.step_gaps)
    gaps.extend(
        ExecutionGap(code="CATALOG_CAPABILITY_PENDING", location=g.location) for g in plan.gaps
    )
    return CellResult(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        scored=plan.scored,
        source_sha256=plan.scenario.sha256,
        expected_failure=_expected_failure(plan),
        gaps=tuple(gaps),
        evidence=evidence,
        catalog_outcome=evaluate_platform_catalog(evidence, assertions, tuple(platform_surfaces)),
        host_outcome=evaluate(evidence, _host_assertions(evidence)),
        request_count=requests,
        rendered_text=rendered,
        headless_outputs=outputs,
        discord_surfaces=surfaces,
        platform_surfaces=platform_surfaces,
        step_receipts=capture.step_receipts,
        input_provenance=fixture.provenance,
    )


async def execute_gemini_cell(
    plan: ScenarioPlan,
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    *,
    source_root: Path,
    repository_root: Path,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
) -> CellResult:
    if (
        plan.backend != "gemini"
        or fixture.backend != "gemini"
        or scenario.scenario_id != plan.scenario.id
        or scenario.source.sha256 != plan.scenario.sha256
        or scenario.source.path != f"scenarios/{plan.scenario.filename}"
        or fixture not in scenario.providers
        or fixture.profile != "gemini.inline_reuse"
        or fixture.model != "gemini-3.8-flash"
    ):
        raise ValueError(
            "Gemini executor requires the pinned scenario and explicit Gemini selection"
        )
    gaps: list[ExecutionGap] = []
    try:
        assertions = _assertions(plan, scenario.values)
    except ValueError:
        assertions = cast(list[dict[str, JsonValue]], [*plan.assertions, *plan.global_assertions])
        gaps.append(ExecutionGap(code="PLACEHOLDER_UNBOUND", location="assertions"))
    evidence = RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=())
    outputs: tuple[HeadlessOutput, ...] = ()
    surfaces: tuple[DiscordSurfaceCapture, ...] = ()
    platform_surfaces: tuple[PlatformSurfaceCapture, ...] = ()
    rendered: tuple[str, ...] = ()
    requests = 0
    if __package__:
        from .gemini_catalog_replay import execute_gemini_turns
    else:
        from gemini_catalog_replay import execute_gemini_turns

    capture = await execute_gemini_turns(
        plan,
        scenario,
        fixture,
        source_root=source_root,
        repository_root=repository_root,
        admin=admin,
        admin_values=admin_values,
        product_context=product_context,
    )
    evidence = RunEvidence(
        scenario_id=plan.scenario.id,
        backend="gemini",
        turns=tuple(t.evidence for t in capture.turns),
    )
    outputs = tuple(t.output for t in capture.turns if t.output is not None)
    surfaces = tuple(t.surface for t in capture.turns if t.surface is not None)
    platform_surfaces = tuple(
        t.platform_surface for t in capture.turns if t.platform_surface is not None
    )
    rendered = tuple(t.output.text if t.output is not None else "" for t in capture.turns)
    requests = capture.request_count
    gaps.extend(capture.step_gaps)
    gaps.extend(
        ExecutionGap(code="CATALOG_CAPABILITY_PENDING", location=g.location) for g in plan.gaps
    )
    return CellResult(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        scored=plan.scored,
        source_sha256=plan.scenario.sha256,
        expected_failure=_expected_failure(plan),
        gaps=tuple(gaps),
        evidence=evidence,
        catalog_outcome=evaluate_platform_catalog(evidence, assertions, tuple(platform_surfaces)),
        host_outcome=evaluate(evidence, _host_assertions(evidence)),
        request_count=requests,
        rendered_text=rendered,
        headless_outputs=outputs,
        discord_surfaces=surfaces,
        platform_surfaces=platform_surfaces,
        step_receipts=capture.step_receipts,
        input_provenance=fixture.provenance,
    )


def admin_source_outcome(admin: AdminHTTPRun) -> OutcomeReport:
    return OutcomeReport(
        scenario_id=admin.scenario_id,
        backend=admin.backend,
        checks=(
            CheckResult(
                kind="admin_only_source", turn=None, status="PASS", code="SOURCE_REQUIRES_NO_TURN"
            ),
        ),
    )


async def execute_http_cell(plan: ScenarioPlan, observer: HTTPObserver) -> CellResult:
    if not http_only_source(plan):
        raise ValueError("HTTP hook requires the complete pinned administrative source")
    expectations = HTTP_SOURCE_ASSERTIONS[plan.scenario.id]
    observations: list[HTTPObservation] = []
    urls: list[str | None] = []
    gaps: list[ExecutionGap] = []
    for n, expectation in enumerate(expectations):
        try:
            _, _, url = observer.resolve(expectation.url)
        except ValueError:
            urls.append(None)
            gaps.append(ExecutionGap(code="HTTP_ORIGIN_UNBOUND", location=f"assert[{n}]"))
            continue
        urls.append(url)
        try:
            observations.append(await observer.observe(n, expectation))
        except (ValueError, httpx.HTTPError, TimeoutError):
            gaps.append(ExecutionGap(code="HTTP_RESPONSE_UNAVAILABLE", location=f"assert[{n}]"))
    admin = AdminHTTPRun(
        scenario_id=plan.scenario.id,
        source_sha256=plan.scenario.sha256,
        backend=plan.backend,
        assertions=expectations,
        urls=tuple(urls),
        observations=tuple(observations),
    )
    return CellResult(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        scored=plan.scored,
        source_sha256=plan.scenario.sha256,
        mode="offline_admin_http",
        admin_http=admin,
        gaps=tuple(gaps),
        evidence=RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=()),
        catalog_outcome=http_outcome(admin),
        host_outcome=admin_source_outcome(admin),
        request_count=len(observations),
        input_provenance="actual-in-process-asgi",
    )


async def run_product_profiles(
    execute_default: Callable[[], Awaitable[ExecutionReport]],
    *,
    execute_opt_in: Callable[[QAProductProfile], Awaitable[ExecutionReport]] | None = None,
) -> ProductProfileReport:
    """One job, two independent executions; an absent opt-in binding stays PENDING.

    The explicit opt-in callback must apply the supplied profile to its actual
    product fixture factories. Merely relabelling a default run is not a binding.
    Neither profile changes any source assertion or earns a PASS from configuration.
    """
    with use_product_profile(DEFAULT_PRODUCT_PROFILE):
        default = await execute_default()
    if default.product_profile != "default":
        raise ValueError("default executor returned another product profile")
    try:
        OPT_IN_PRODUCT_PROFILE.require_available()
        if execute_opt_in is None:
            raise ProductProfileUnavailable(("explicit-opt-in-fixture-binding",))
    except ProductProfileUnavailable as error:
        opt_in = ExecutionReport(
            integration_sha=default.integration_sha,
            target_sha256=default.target_sha256,
            target_ids=default.target_ids,
            product_profile="opt-in-product",
            profile_state="unavailable",
            cells=tuple(
                CellResult(
                    scenario_id=c.scenario_id,
                    backend=c.backend,
                    scored=c.scored,
                    source_sha256=c.source_sha256,
                    product_profile="opt-in-product",
                    profile_state="unavailable",
                    gaps=tuple(
                        ExecutionGap(code="PRODUCT_PROFILE_UNAVAILABLE", location=missing)
                        for missing in error.missing
                    ),
                    evidence=RunEvidence(scenario_id=c.scenario_id, backend=c.backend, turns=()),
                    catalog_outcome=OutcomeReport(
                        scenario_id=c.scenario_id,
                        backend=c.backend,
                        checks=tuple(
                            check.model_copy(
                                update={
                                    "status": "PENDING",
                                    "code": "PRODUCT_PROFILE_UNAVAILABLE",
                                    "evidence_ids": (),
                                }
                            )
                            for check in c.catalog_outcome.checks
                        ),
                    ),
                    host_outcome=OutcomeReport(
                        scenario_id=c.scenario_id, backend=c.backend, checks=()
                    ),
                )
                for c in default.cells
            ),
        )
    else:
        with use_product_profile(OPT_IN_PRODUCT_PROFILE):
            raw = await execute_opt_in(OPT_IN_PRODUCT_PROFILE)
            applied = applied_product_flags()
        missing = tuple(sorted(OPT_IN_PRODUCT_PROFILE.flags.keys() - applied))
        state = "unbound" if missing else "executed"
        opt_in = ExecutionReport(
            integration_sha=raw.integration_sha,
            target_sha256=raw.target_sha256,
            target_ids=raw.target_ids,
            product_profile="opt-in-product",
            profile_state=state,
            applied_flags=tuple(sorted(applied)),
            cells=tuple(
                c.model_copy(
                    update={
                        "product_profile": "opt-in-product",
                        "profile_state": state,
                        "gaps": (
                            *c.gaps,
                            *(
                                ExecutionGap(code="PRODUCT_PROFILE_NOT_APPLIED", location=field)
                                for field in missing
                            ),
                        ),
                    }
                )
                for c in raw.cells
            ),
        )
    return ProductProfileReport(default_board=default, opt_in_board=opt_in)


@dataclass(frozen=True)
class CatalogProductBindings:
    """Actual disposable owner surfaces for one explicitly selected execution.

    Factories apply the profile to the settings they pass to product code. An
    absent surface retains the executor's existing gaps; settings alone supply
    no native, delivery, accounting or source-outcome evidence.
    """

    profile: QAProductProfile
    http_observer: HTTPObserver | None = None
    admin_runs: Mapping[tuple[str, Provider], AdminStepBinding] = field(
        default_factory=dict[tuple[str, Provider], AdminStepBinding]
    )
    product_contexts: Mapping[tuple[str, Provider], ProductContext] = field(
        default_factory=dict[tuple[str, Provider], ProductContext]
    )
    rejected_token_runs: Mapping[tuple[str, Provider], RejectedTokenBinding] = field(
        default_factory=dict[tuple[str, Provider], RejectedTokenBinding]
    )


async def execute_catalog_profiles(
    root: Path,
    *,
    integration_sha: str,
    run_id: str,
    replays: tuple[ScenarioReplay, ...],
    bindings: Callable[[QAProductProfile], AbstractAsyncContextManager[CatalogProductBindings]],
    provider_fixtures: Path | None = None,
    anthropic_fixtures: Path | None = None,
) -> ProductProfileReport:
    """Build fresh product surfaces inside each profile, then execute both boards.

    Mutable DB bindings, transport observers and credential callbacks cannot be
    reused from default execution: that would attribute spent resources and
    settings to another profile. Source-pinned native fixture inputs are shared
    unchanged. Failures after dispatch propagate, including factory cleanup.
    """
    prior: list[object] = []

    async def execute(profile: QAProductProfile) -> ExecutionReport:
        if profile != current_product_profile():
            raise ValueError("catalog factory differs from active product profile")
        async with bindings(profile) as actual:
            if actual.profile != profile:
                raise ValueError("catalog factory returned a different product profile")
            surfaces: list[object] = [actual]
            if actual.http_observer is not None:
                surfaces.append(actual.http_observer)
            surfaces.extend(run.binding for run in actual.admin_runs.values())
            surfaces.extend(actual.product_contexts.values())
            surfaces.extend(actual.rejected_token_runs.values())
            if any(surface is old for surface in surfaces for old in prior):
                raise ValueError("catalog profiles reused a mutable product surface")
            prior.extend(surfaces)
            return await execute_catalog(
                root,
                integration_sha=integration_sha,
                run_id=run_id,
                replays=replays,
                provider_fixtures=provider_fixtures,
                anthropic_fixtures=anthropic_fixtures,
                admin_runs=actual.admin_runs,
                product_contexts=actual.product_contexts,
                http_observer=actual.http_observer,
                rejected_token_runs=actual.rejected_token_runs,
            )

    return await run_product_profiles(
        lambda: execute(DEFAULT_PRODUCT_PROFILE), execute_opt_in=execute
    )


async def execute_catalog(
    root: Path,
    *,
    integration_sha: str,
    run_id: str,
    replays: tuple[ScenarioReplay, ...],
    provider_fixtures: Path | None = None,
    anthropic_fixtures: Path | None = None,
    admin_runs: Mapping[tuple[str, Provider], AdminStepBinding] | None = None,
    product_contexts: Mapping[tuple[str, Provider], ProductContext] | None = None,
    http_observer: HTTPObserver | None = None,
    rejected_token_runs: Mapping[tuple[str, Provider], RejectedTokenBinding] | None = None,
) -> ExecutionReport:
    # Always regenerate from pinned source files, never execute an unvalidated saved plan.
    matrix: CatalogMatrix = build_matrix(root, integration_sha=integration_sha, run_id=run_id)
    admins = dict(admin_runs or {})
    contexts = dict(product_contexts or {})
    credential_runs = dict(rejected_token_runs or {})
    if any(key != (REJECTED_TOKEN_SCENARIO, "anthropic") for key in credential_runs):
        raise ValueError("rejected-token administrative opt-in is Anthropic-only (A3)")
    if set(credential_runs) & (set(admins) | set(contexts)):
        raise ValueError("rejected-token administrative opt-in conflicts with turn inputs")
    cells_with_source = {(p.scenario.id, p.backend) for p in matrix.plans if p.scored}
    if any(key not in cells_with_source for key in admins) or any(
        selected != run.binding.backend for (_, selected), run in admins.items()
    ):
        raise ValueError("admin runs differ from frozen scenario/provider cells")
    if any(key not in admins or contexts[key].admin is not admins[key].binding for key in contexts):
        raise ValueError("product contexts require matching frozen admin runs")
    by_id = {replay.scenario_id: replay for replay in replays}
    if len(by_id) != len(replays) or set(by_id) - set(matrix.target_ids):
        raise ValueError("replays must uniquely identify frozen target scenarios")
    pack = (
        load_provider_fixtures(provider_fixtures, catalog_root=root) if provider_fixtures else None
    )
    repository_root = Path(__file__).resolve().parents[2]
    anthropic_pack = (
        load_anthropic_fixtures(anthropic_fixtures, source_root=repository_root)
        if anthropic_fixtures is not None
        else None
    )
    manifest_hash = sha(anthropic_fixtures.read_bytes()) if anthropic_fixtures else ""
    cells: list[CellResult] = []
    for plan in matrix.plans:
        if plan.scored:
            if http_observer is not None and http_only_source(plan):
                cells.append(await execute_http_cell(plan, http_observer))
                continue
            rejected_token = credential_runs.get((plan.scenario.id, plan.backend))
            if rejected_token is not None:
                cells.append(await execute_cell(plan, None, rejected_token=rejected_token))
                continue
            admin_run = admins.get((plan.scenario.id, plan.backend))
            admin = admin_run.binding if admin_run is not None else None
            admin_values = admin_run.values if admin_run is not None else None
            product_context = contexts.get((plan.scenario.id, plan.backend))
            if plan.backend == "anthropic" and anthropic_pack is not None and anthropic_fixtures:
                entry, row = anthropic_pack.select(plan.scenario.id)
                recipe = AnthropicRecipe(
                    entry,
                    row,
                    anthropic_pack.index.codec,
                    anthropic_fixtures,
                    repository_root,
                    manifest_hash,
                )
                cells.append(
                    await execute_cell(
                        plan,
                        ScenarioReplay.model_validate(row.model_dump(mode="json")),
                        native_recipe=recipe,
                        admin=admin,
                        admin_values=admin_values,
                        product_context=product_context,
                    )
                )
                continue
            if (
                plan.backend in ("openai", "gemini")
                and pack is not None
                and provider_fixtures is not None
            ):
                scenario, fixture = pack.select(plan.scenario.id, plan.backend)
                cells.append(
                    await (
                        execute_openai_cell if plan.backend == "openai" else execute_gemini_cell
                    )(
                        plan,
                        scenario,
                        fixture,
                        source_root=provider_fixtures.parent / "catalog",
                        repository_root=Path(__file__).resolve().parents[2],
                        admin=admin,
                        admin_values=admin_values,
                        product_context=product_context,
                    )
                )
                continue
            replay = by_id.get(plan.scenario.id) if plan.backend == "anthropic" else None
            cells.append(
                await execute_cell(
                    plan,
                    replay,
                    admin=admin,
                    admin_values=admin_values,
                    product_context=product_context,
                )
            )
    return ExecutionReport(
        integration_sha=matrix.integration_sha,
        target_sha256=matrix.target_sha256,
        target_ids=matrix.target_ids,
        cells=tuple(cells),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--replays", type=Path, required=True)
    parser.add_argument("--provider-fixtures", type=Path)
    parser.add_argument("--anthropic-fixtures", type=Path)
    parser.add_argument(
        "--http-apps",
        action="store_true",
        help="bind the two admin-only HTTP scenarios to real disposable in-process apps",
    )
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw: list[object] = json.loads(args.replays.read_text())
    replays = tuple(ScenarioReplay.model_validate(item) for item in raw)

    @asynccontextmanager
    async def bindings(profile: QAProductProfile) -> AsyncIterator[CatalogProductBindings]:
        if not args.http_apps:
            yield CatalogProductBindings(profile=profile)
            return
        from tempfile import TemporaryDirectory

        # Fresh settings, link stores and ASGI observers for each profile.
        with TemporaryDirectory(prefix="daimon-qa-http-") as folder:
            async with offline_http_apps(
                Path(folder),
                nonce="qa-http",
                profile=profile if profile != DEFAULT_PRODUCT_PROFILE else None,
            ) as observer:
                yield CatalogProductBindings(profile=profile, http_observer=observer)

    result = asyncio.run(
        execute_catalog_profiles(
            args.catalog.resolve(),
            integration_sha=args.integration_sha,
            run_id=args.run_id,
            replays=replays,
            provider_fixtures=args.provider_fixtures.resolve() if args.provider_fixtures else None,
            anthropic_fixtures=args.anthropic_fixtures.resolve()
            if args.anthropic_fixtures
            else None,
            bindings=bindings,
        )
    )
    args.output.write_text(result.model_dump_json(indent=2) + "\n")
    print(f"Default-profile scored cells: {result.scored_counts}; no live calls.")
    print(f"Opt-in product profile: {result.opt_in_scored_counts}; separate 159-cell board.")


if __name__ == "__main__":
    main()
