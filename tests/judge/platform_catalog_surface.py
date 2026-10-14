"""Bind actual Slack/Teams post windows to independently acknowledged catalog roots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason, acknowledge
from daimon.core.turn.state import TurnState
from daimon.testing.host_clock import current_host_clock
from daimon.testing.outcome_oracle import (
    OutcomeReport,
    RunEvidence,
    TurnEvidence,
    VisibleText,
    evaluate,
)
from daimon.testing.platform_surface import PlatformMessageCapture, PlatformSurfaceCapture
from daimon.testing.slack_surface import SlackSurfaceHarness
from daimon.testing.teams_surface import TeamsSurfaceHarness
from pydantic import JsonValue

if __package__:
    from .discord_catalog_surface import CatalogLifecycle as DiscordCatalogLifecycle
else:
    from discord_catalog_surface import CatalogLifecycle as DiscordCatalogLifecycle


class CatalogLifecycle(DiscordCatalogLifecycle):
    """Preserve Discord/host recording and forward new platforms to real adapters."""

    def __init__(
        self, *, surface: str, evidence_id: str, session_id: str, root_turn_id: str, model_id: str
    ) -> None:
        super().__init__(
            surface=surface,
            evidence_id=evidence_id,
            session_id=session_id,
            root_turn_id=root_turn_id,
            model_id=model_id,
        )
        self.platform_harness: SlackSurfaceHarness | TeamsSurfaceHarness | None = None
        if surface == "slack":
            # The catalog's ordinary table case preserves the adapter's default.
            self.platform_harness = SlackSurfaceHarness(
                clock=current_host_clock(),
                evidence_id=evidence_id,
                session_id=session_id,
                root_turn_id=root_turn_id,
                model_id=model_id,
                render_tables=False,
            )
        elif surface == "teams":
            self.platform_harness = TeamsSurfaceHarness(
                clock=current_host_clock(),
                evidence_id=evidence_id,
                session_id=session_id,
                root_turn_id=root_turn_id,
                model_id=model_id,
            )

    @property
    def platform_capture(self) -> PlatformSurfaceCapture | None:
        return self.platform_harness.snapshot() if self.platform_harness is not None else None

    async def run(self, invoke: Callable[[], Awaitable[None]]) -> None:
        if self.platform_harness is None:
            await super().run(invoke)
        else:

            async def delivered(_lifecycle: object) -> None:
                if self.platform_harness is None:
                    raise ValueError("platform capture disappeared")
                await self.platform_harness.lifecycle.post_initial()
                await invoke()

            await self.platform_harness.run(delivered)

    async def on_render(self, state: TurnState) -> None:
        await super().on_render(state)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_render(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_terminal_success(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_terminal_failure(state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await super().on_sse_event(event)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await super().on_reconnect(reason)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await super().on_rate_limited(until)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await super().on_interrupt_sent(source)
        if self.platform_harness is not None:
            await self.platform_harness.lifecycle.on_interrupt_sent(source)

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        await super().on_acknowledgment(phase)
        if self.platform_harness is not None:
            await acknowledge(self.platform_harness.lifecycle, phase)


def bind_platform_text(
    evidence: TurnEvidence,
    capture: PlatformSurfaceCapture | None,
    *,
    expected_id: str,
    expected_platform: str,
    observed_s: float,
    accounting_required: bool = False,
    include_history: bool = False,
) -> TurnEvidence:
    """Validate full post history and bind surviving text; never bind fallbacks.

    Completion of adapter delivery is independent of native turn completion.
    Accounting-dependent assertions remain partial without real footer evidence.
    Explicit refusals remain effective under python -O.
    """
    if capture is None:
        return evidence
    # model_copy can bypass validators; refuse corrupted serialized receipts too.
    capture = PlatformSurfaceCapture.model_validate_json(capture.model_dump_json())
    if (
        capture.platform != expected_platform
        or capture.evidence_id != expected_id
        or capture.session_id != evidence.session_id
        or capture.root_turn_id != evidence.root_turn_id
        or capture.started_s < evidence.started_s
        or capture.observed_s > observed_s
    ):
        raise ValueError("platform surface differs from the acknowledged root window")
    clocks = [event.message.observed_s for event in capture.events]
    destinations = {(m.destination_id, m.thread_id) for m in capture.messages} | {
        (event.message.destination_id, event.message.thread_id) for event in capture.events
    }
    if clocks != sorted(clocks) or len(destinations) > 1:
        raise ValueError("platform surface has inconsistent destination or clock order")
    survivors: dict[str, PlatformMessageCapture] = {}
    created: set[str] = set()
    for index, event in enumerate(capture.events):
        message = event.message
        if event.evidence_id != f"{expected_id}:post:{index}":
            raise ValueError("platform surface has inconsistent post identity or order")
        if event.operation == "send":
            if message.message_id in created or message.order != len(created):
                raise ValueError("platform surface has duplicate or unordered message creation")
            created.add(message.message_id)
        elif message.message_id not in survivors:
            raise ValueError("platform edit/delete refers to an unowned message")
        elif message.order != survivors[message.message_id].order:
            raise ValueError("platform edit/delete changes message order")
        if event.operation == "delete":
            if message.model_dump(exclude={"observed_s"}) != survivors[
                message.message_id
            ].model_dump(exclude={"observed_s"}):
                raise ValueError("platform delete differs from its owned message")
            del survivors[message.message_id]
            continue
        survivors[message.message_id] = message
    if capture.messages != tuple(survivors.values()):
        raise ValueError("platform surviving text differs from actual post history")
    texts: list[VisibleText] = []
    # Positive predicates use the final surviving snapshot; negative predicates
    # must also inspect text that was displayed and later replaced/deleted.
    observations = (
        [
            (e.evidence_id, e.message, i, e.message.observed_s)
            for i, e in enumerate(capture.events)
            if e.operation != "delete"
        ]
        if include_history
        else [
            (f"{expected_id}:message:{m.message_id}", m, m.order, capture.observed_s)
            for m in capture.messages
        ]
    )
    for text_id, message, order, observed in observations:
        text = "\n".join(
            part
            for part in (*message.body_texts, *message.card_texts, *message.control_labels)
            if part
        )
        if text.strip():
            texts.append(
                VisibleText(
                    evidence_id=text_id,
                    message_id=message.message_id,
                    order=order,
                    observed_s=observed,
                    text=text,
                )
            )
    # The oracle owns other effect streams. Refuse order collisions instead of
    # renumbering independently bound cards, approvals or tool effects.
    return TurnEvidence.model_validate(
        {
            **evidence.model_dump(),
            "texts": tuple(texts),
            "text_capture_complete": capture.text_capture_complete and not accounting_required,
        }
    )


def evaluate_platform_catalog(
    evidence: RunEvidence,
    assertions: Sequence[Mapping[str, JsonValue]],
    captures: tuple[PlatformSurfaceCapture, ...],
) -> OutcomeReport:
    """Run unchanged predicates: final positives, complete history for negatives."""
    final = evaluate(evidence, assertions)
    if not captures:
        return final
    histories: dict[tuple[str, str], TurnEvidence] = {}
    owned = {(t.session_id, t.root_turn_id): t for t in evidence.turns}
    for capture in captures:
        key = (capture.session_id, capture.root_turn_id)
        if key not in owned or key in histories:
            raise ValueError("platform history is foreign or duplicated")
        turn = owned[key]
        histories[key] = bind_platform_text(
            turn,
            capture,
            expected_id=capture.evidence_id,
            expected_platform=capture.platform,
            observed_s=capture.observed_s,
            accounting_required=not turn.text_capture_complete,
            include_history=True,
        )
    history = RunEvidence.model_validate(
        {
            **evidence.model_dump(),
            "turns": tuple(
                histories.get((t.session_id, t.root_turn_id), t) for t in evidence.turns
            ),
        }
    )
    observed = evaluate(history, assertions)
    return OutcomeReport(
        scenario_id=final.scenario_id,
        backend=final.backend,
        checks=tuple(
            h if f.kind in {"text_absent", "no_preamble"} else f
            for f, h in zip(final.checks, observed.checks, strict=True)
        ),
    )
