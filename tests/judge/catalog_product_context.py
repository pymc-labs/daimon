"""Explicit, owned-DB admission/preparation; absent pinned inputs stay pending."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from daimon.core.channel_backend import current_backend
from daimon.core.config import McpSettings
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.session_preparation import (
    PreparationBusy,
    PreparationDeferred,
    PreparationFailure,
)
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.admission import admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import (
    PreparedTurn,
    admitted_session_scope,
    bind_session,
)
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.state import extract_final_response
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.admin_bindings import AdminBindings, AdminCommand
from daimon.testing.admin_resources import ResourceAdminBindings
from daimon.testing.anthropic_fixtures import AnthropicTape, anthropic_replay
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.outcome_oracle import SessionUse, TerminalEvidence, TurnEvidence, VisibleText
from daimon.testing.provider_replay import Record, SourcePin
from mux.contracts.config import ConfigRevision
from mux.contracts.events import TurnEndedPayload
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.state.lease import Slot

if __package__:
    from .anthropic_catalog_replay import AnthropicTurnSource
    from .catalog_runner import Invocation, ScenarioPlan
    from .discord_catalog_surface import bind_discord_text
    from .headless_output import HeadlessOutput, capture_headless_return
    from .platform_catalog_surface import CatalogLifecycle, bind_platform_text
else:
    from anthropic_catalog_replay import AnthropicTurnSource
    from catalog_runner import Invocation, ScenarioPlan
    from discord_catalog_surface import bind_discord_text
    from headless_output import HeadlessOutput, capture_headless_return
    from platform_catalog_surface import CatalogLifecycle, bind_platform_text


class ProductContextPending(Exception):
    """The public product path did not produce a verified host turn."""


class ProductTurnRecipe(Record):
    scenario_id: str
    scenario_sha256: str
    location: str
    turn: int
    tenant_id: str
    channel_id: str
    thread_id: str
    external_user_id: str
    session_id: str
    root_turn_id: str
    tape: AnthropicTape


@dataclass(frozen=True)
class ProductCapture:
    evidence: TurnEvidence
    request_count: int
    output: HeadlessOutput | None
    lifecycle: CatalogLifecycle


@dataclass
class ProductContext:
    """Opt in per cell; callers supply owned destinations and N3-pinned inputs.

    No dependency or admission callback is injectable. Native preparation is
    unavailable through the granted public function and never uses Anthropic.
    """

    admin: AdminBindings
    channels: Mapping[str, str]
    threads: Mapping[tuple[str, str], str]
    external_user_id: str
    sources: Mapping[int, SourcePin]
    source_root: Path
    repository_root: Path
    _completed: dict[str, TurnEvidence] = field(
        default_factory=dict[str, TurnEvidence], init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.admin.resource_edge is None:
            self.admin.resource_edge = ResourceAdminBindings(self.admin)

    def channel(self, invocation: Invocation) -> str:
        require_owned_sessionmaker(self.admin.sessions)
        channel = self.channels.get(invocation.channel or "")
        if channel is None or channel not in self.admin.channels:
            raise ProductContextPending("owned product channel is unbound")
        return channel

    def destination(self, invocation: Invocation) -> tuple[str, str]:
        channel = self.channel(invocation)
        thread = self.threads.get((invocation.channel or "", invocation.thread or ""))
        if not thread or not self.external_user_id:
            raise ProductContextPending("owned product thread or caller is unbound")
        return channel, thread

    async def configure(self, invocation: Invocation) -> None:
        channel = self.channel(invocation)
        receipt = await self.admin.configure_channel(channel)
        if receipt.status != "executed" or receipt.backend != self.admin.backend:
            raise ProductContextPending("real channel configuration did not execute")

    async def revision(self, invocation: Invocation) -> ConfigRevision:
        channel, _ = self.destination(invocation)
        await self.admin.authorize_command(
            AdminCommand("host_turn", self.admin.platform, self.admin.workspace_id, channel)
        )
        async with self.admin.sessions() as db:
            revision = await current_backend(
                db,
                ChannelRef(
                    tenant_id=self.admin.scope.tenant_id,
                    platform=self.admin.platform,
                    channel_id=channel,
                ),
            )
        if revision is None or revision.backend != self.admin.backend:
            raise ProductContextPending("real channel revision is unbound")
        return revision

    def verify_execution(self, invocation: Invocation, evidence: TurnEvidence) -> None:
        if self._completed.get(invocation.location) != evidence:
            raise ValueError("product context has no real prepared-turn capture")

    async def native_pending(self, invocation: Invocation) -> None:
        await self.revision(invocation)
        raise ProductContextPending("public preparation has no provider-native dispatch")

    async def prepare(
        self,
        deps: TurnDeps,
        invocation: Invocation,
        revision: ConfigRevision,
        timeout_s: float,
    ) -> PreparedTurn:
        """The granted public path, without an injected admission/session factory."""
        channel, thread = self.destination(invocation)
        if (
            deps.sessionmaker is not self.admin.sessions
            or deps.state_store is not None
            or not deps.channel_backends
            or deps.turn_path != "mux"
        ):
            raise ValueError("product dependencies differ from the owned DB path")
        if self.admin.backend != "anthropic":
            await self.native_pending(invocation)
        if revision.thread_mode != "per_caller":
            raise ProductContextPending("shared product preparation is unbound")
        tenant = uuid.UUID(self.admin.scope.tenant_id)
        if await self.revision(invocation) != revision:
            raise ProductContextPending("real channel revision changed before admission")
        try:
            admission = await admit(
                deps,
                requester_org=RequesterOrg.HOST,
                tenant_id=tenant,
                platform=self.admin.platform,
                external_user_id=self.external_user_id,
                channel_id=channel,
                thread_id=thread,
                now=datetime.now(UTC),
            )
            if admission.backend_revision != revision or admission.grant is None:
                raise ValueError("admission did not use the real configured revision/policy")
            prepared = await bind_session(
                deps,
                admission,
                tenant_id=tenant,
                platform=self.admin.platform,
                external_user_id=self.external_user_id,
                thread_id=thread,
                session_account_id=admission.account_id,
                reuse_existing=True,
                deadline=datetime.now(UTC) + timedelta(seconds=timeout_s),
            )
        except DaimonError as error:
            raise ProductContextPending("real admission/preparation refused the turn") from error
        if isinstance(prepared, (PreparationBusy, PreparationDeferred, PreparationFailure)):
            raise ProductContextPending("real preparation is busy, deferred or failed")
        return prepared

    async def run(
        self,
        plan: ScenarioPlan,
        invocation: Invocation,
        native: AnthropicTurnSource | None,
        timeout_s: float,
    ) -> ProductCapture:
        self._completed.pop(invocation.location, None)
        if plan.backend != self.admin.backend:
            raise ValueError("product context differs from selected provider")
        if plan.backend != "anthropic":
            await self.native_pending(invocation)
        if native is None or invocation.turn is None:
            raise ProductContextPending("source-pinned native turn is unbound")
        native.recipe.validate(plan)
        pin = self.sources.get(invocation.turn)
        if pin is None:
            raise ProductContextPending("source-pinned product preparation is unbound")
        pin.verify(self.source_root)
        recipe = ProductTurnRecipe.model_validate_json((self.source_root / pin.path).read_bytes())
        channel, thread = self.destination(invocation)
        if (
            recipe.scenario_id != plan.scenario.id
            or recipe.scenario_sha256 != plan.scenario.sha256
            or recipe.location != invocation.location
            or recipe.turn != invocation.turn
            or recipe.tenant_id != self.admin.scope.tenant_id
            or recipe.channel_id != channel
            or recipe.thread_id != thread
            or recipe.external_user_id != self.external_user_id
            or recipe.session_id != native.recipe.session_id(invocation.turn)
            or recipe.root_turn_id != native.turn.events[0]["id"]
            or recipe.tape.source != native.recipe.entry.replay
            or recipe.tape.codec != native.recipe.codec
            or self.repository_root != native.recipe.repository_root
        ):
            raise ValueError("product recipe differs from owned scope or native source")
        revision = await self.revision(invocation)
        store = PostgresStateStore(self.admin.sessions)
        tenant = uuid.UUID(self.admin.scope.tenant_id)
        started = time.monotonic()
        output_id = f"{plan.scenario.id}:anthropic:{recipe.session_id}:{recipe.root_turn_id}:headless_return"
        surface_id = output_id.replace(
            ":headless_return", f":{plan.scenario.scenario.get('surface')}_adapter_posts"
        )
        lifecycle = CatalogLifecycle(
            surface=str(plan.scenario.scenario.get("surface")),
            evidence_id=surface_id,
            session_id=recipe.session_id,
            root_turn_id=recipe.root_turn_id,
            model_id=native.recipe.scenario.agent_model,
        )
        output: HeadlessOutput | None = None
        async with anthropic_replay(
            recipe.tape,
            source_root=native.recipe.manifest.parent,
            codec_root=self.repository_root,
        ) as replay:
            deps = TurnDeps(
                anthropic=replay.client,
                sessionmaker=self.admin.sessions,
                deployment_default=self.admin.default,
                resolver_cache=new_resolver_cache(),
                defaults_root=self.repository_root / "defaults",
                mcp=McpSettings(),
                billing_config=None,
                markup=Decimal("1"),
                fernet=None,
                github_fallback_pat=None,
                github_app_id=None,
                github_app_private_key=None,
                public_url=None,
                turn_path="mux",
                channel_backends=True,
                tool_safety=OPEN_TOOL_SAFETY,
            )
            prepared = await self.prepare(deps, invocation, revision, timeout_s)
            admission = prepared.admission
            if prepared.ma_session_id != recipe.session_id:
                raise ValueError("real preparation differs from the pinned native session")
            async with self.admin.sessions() as db:
                mapped = await get_live_thread_session(
                    db,
                    tenant_id=tenant,
                    platform=self.admin.platform,
                    thread_id=thread,
                    account_id=prepared.session_account_id,
                )
            if (
                mapped is None
                or mapped.id != prepared.mapping_id
                or mapped.ma_session_id != recipe.session_id
                or mapped.effective_config is None
                or mapped.effective_config.model_id != native.recipe.scenario.agent_model
            ):
                raise ValueError("real prepared mapping/model differs from the pinned session")
            scope = admitted_session_scope(
                prepared.admission, tenant_id=tenant, session_id=recipe.session_id
            )
            before = await store.read_events(recipe.session_id)
            if any(e.turn_id == recipe.root_turn_id for e in before):
                raise ValueError("product root already belongs to an earlier invocation")
            outcomes: list[RunOutcome] = []

            async def invoke() -> str:
                async def reseed() -> str:
                    raise ValueError("source-pinned product replay cannot invent recovery")

                def recovery(_cancel: asyncio.Event) -> CatalogLifecycle:
                    raise ValueError("source-pinned product replay cannot invent recovery")

                result = await run_prepared_turn(
                    deps,
                    prepared,
                    tenant_id=tenant,
                    platform=self.admin.platform,
                    thread_id=thread,
                    external_user_id=self.external_user_id,
                    user_message=native.turn.request_text,
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    reseed_user_message=reseed,
                    recovery_lifecycle=recovery,
                    render_interval_s=3600,
                    operation_key=f"host-turn-{invocation.turn}",
                )
                outcomes.append(result)
                if result.state.error is not None:
                    raise result.state.error
                return extract_final_response(result.state.content)

            async def captured() -> None:
                nonlocal output
                output = await capture_headless_return(invoke, evidence_id=output_id)

            await asyncio.wait_for(lifecycle.run(captured), timeout=timeout_s)
            replay.wire.assert_consumed()
        observed = time.monotonic()
        if (
            len(outcomes) != 1
            or outcomes[0].recovered
            or outcomes[0].ma_session_id != recipe.session_id
        ):
            raise ValueError("product turn did not retain its source-pinned session")
        native.verify_usage(outcomes[0].state)
        if [e.model_dump(mode="json", exclude_none=True) for e in lifecycle.sse_events] != [
            type(e).model_validate(expected).model_dump(mode="json", exclude_none=True)
            for e, expected in zip(lifecycle.sse_events, native.turn.events, strict=True)
        ]:
            raise ValueError("product stream differs from pinned native events")
        operation = await store.get_operation(
            scope, f"turn:{recipe.session_id}:host-turn-{invocation.turn}:send:0"
        )
        if (
            operation is None
            or not operation.result
            or operation.result.get("input_ids") != [recipe.root_turn_id]
        ):
            raise ValueError("product root has no durable input acknowledgement")
        journal = (await store.read_events(recipe.session_id))[len(before) :]
        if any(e.turn_id is not None and e.turn_id != recipe.root_turn_id for e in journal):
            raise ValueError("product journal contains a foreign root")
        terminals: list[TerminalEvidence] = []
        for event in journal:
            if event.type == "session.turn_ended":
                payload = TurnEndedPayload.model_validate(event.payload)
                if (
                    event.turn_id != recipe.root_turn_id
                    or payload.root_turn_id != recipe.root_turn_id
                ):
                    raise ValueError("product terminal contains a foreign root")
                terminals.append(
                    TerminalEvidence(
                        evidence_id=f"{recipe.session_id}:{event.id}",
                        session_id=event.session_id,
                        root_turn_id=payload.root_turn_id,
                        authority=event.authority,
                        outcome=payload.outcome,
                        observed_s=observed,
                    )
                )
        if (
            len(terminals) != 1
            or len(lifecycle.terminal_success) != 1
            or lifecycle.terminal_failures
        ):
            raise ValueError("product turn has no unique successful terminal")
        if await self.revision(invocation) != prepared.admission.backend_revision:
            raise ProductContextPending("product revision changed before capture closed")
        evidence = TurnEvidence(
            turn=invocation.turn,
            slot_id=f"{tenant}:{channel}:{thread}:{admission.account_id}",
            session_id=recipe.session_id,
            root_turn_id=recipe.root_turn_id,
            started_s=started,
            terminals=tuple(terminals),
            sessions=(
                SessionUse(evidence_id=output_id + ":selected", session_id=recipe.session_id),
            ),
            terminal_capture_complete=not any(e.authority == "gap" for e in journal),
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
            if output is not None and plan.scenario.scenario.get("surface") == "headless"
            else (),
            text_capture_complete=output is not None
            and plan.scenario.scenario.get("surface") == "headless",
        )
        accounting = any(
            a.get("kind") in {"footer_cost_matches_ledger", "ledger_matches_usage"}
            for a in plan.assertions
        )
        evidence = bind_discord_text(
            evidence,
            lifecycle.capture,
            expected_id=surface_id,
            observed_s=observed,
            accounting_required=accounting,
        )
        evidence = bind_platform_text(
            evidence,
            lifecycle.platform_capture,
            expected_id=surface_id,
            expected_platform=str(plan.scenario.scenario.get("surface")),
            observed_s=observed,
            accounting_required=accounting,
        )
        published = await store.get_binding(
            Slot(
                thread=ThreadRef(channel=revision.channel, thread_id=thread),
                account_id=scope.account_id,
            )
        )
        if (
            published is None
            or published.provider != "anthropic"
            or published.native_refs.get("session") != recipe.session_id
            or published.config_revision != revision.local
        ):
            raise ValueError("real product binding changed before capture closed")
        self._completed[invocation.location] = evidence
        return ProductCapture(evidence, len(replay.wire.requests), output, lifecycle)
