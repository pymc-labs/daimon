"""Owned Discord adapter capture for offline catalog host invocations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason
from daimon.core.turn.state import TurnState
from daimon.testing.discord_surface import (
    DiscordMessageCapture,
    DiscordSurfaceCapture,
    DiscordSurfaceHarness,
)
from daimon.testing.host_clock import current_host_clock
from daimon.testing.outcome_oracle import TurnEvidence, VisibleText
from daimon.testing.turn_fakes import RecordingLifecycle


class CatalogLifecycle(RecordingLifecycle):
    """Record host closure while forwarding every hook to the actual adapter.

    A fresh harness covers only this turn's owned transport. No external tool
    posts, thread titles, DB-backed balance footer or real Discord gateway is
    certified. Non-Discord surfaces retain the recording lifecycle alone.
    """

    def __init__(
        self, *, surface: str, evidence_id: str, session_id: str, root_turn_id: str, model_id: str
    ) -> None:
        super().__init__()
        self.harness = (
            DiscordSurfaceHarness(
                clock=current_host_clock(),
                evidence_id=evidence_id,
                session_id=session_id,
                root_turn_id=root_turn_id,
                model_id=model_id,
                # The catalog's table case explicitly checks the ordinary default.
                render_tables=False,
            )
            if surface == "discord"
            else None
        )

    @property
    def capture(self) -> DiscordSurfaceCapture | None:
        return self.harness.snapshot() if self.harness is not None else None

    async def run(self, invoke: Callable[[], Awaitable[None]]) -> None:
        if self.harness is None:
            await invoke()
        else:

            async def delivered(_lifecycle: object) -> None:
                if self.harness is None:
                    raise ValueError("Discord capture disappeared")
                await self.harness.lifecycle.post_initial()
                await invoke()

            await self.harness.run(delivered)

    async def on_render(self, state: TurnState) -> None:
        await super().on_render(state)
        if self.harness is not None:
            await self.harness.lifecycle.on_render(state)

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        if self.harness is not None:
            await self.harness.lifecycle.on_terminal_success(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        if self.harness is not None:
            await self.harness.lifecycle.on_terminal_failure(state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await super().on_sse_event(event)
        if self.harness is not None:
            await self.harness.lifecycle.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await super().on_reconnect(reason)
        if self.harness is not None:
            await self.harness.lifecycle.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await super().on_rate_limited(until)
        if self.harness is not None:
            await self.harness.lifecycle.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await super().on_interrupt_sent(source)
        if self.harness is not None:
            await self.harness.lifecycle.on_interrupt_sent(source)

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        if self.harness is not None:
            await self.harness.lifecycle.on_acknowledgment(phase)


def bind_discord_text(
    evidence: TurnEvidence,
    capture: DiscordSurfaceCapture | None,
    *,
    expected_id: str,
    observed_s: float,
    accounting_required: bool = False,
) -> TurnEvidence:
    """Bind fresh surviving posts only after independent native ownership checks.

    Historical drafts/deleted messages are diagnostic events, never final text.
    Attachment pixels and unclosed adapter delivery keep text coverage partial.
    """
    if capture is None:
        return evidence
    if (
        capture.evidence_id != expected_id
        or capture.session_id != evidence.session_id
        or capture.root_turn_id != evidence.root_turn_id
        or capture.started_s < evidence.started_s
        or capture.observed_s > observed_s
    ):
        raise ValueError("Discord surface differs from the acknowledged root window")
    ids = [m.message_id for m in capture.messages]
    orders = [m.order for m in capture.messages]
    event_ids = [e.evidence_id for e in capture.events]
    channels = {m.channel_id for m in capture.messages} | {
        e.message.channel_id for e in capture.events
    }
    if (
        len(set(ids)) != len(ids)
        or len(set(orders)) != len(orders)
        or orders != sorted(orders)
        or len(set(event_ids)) != len(event_ids)
        or len(channels) > 1
        or any(
            not capture.started_s <= m.observed_s <= capture.observed_s for m in capture.messages
        )
    ):
        raise ValueError("Discord surface has inconsistent post identity or order")
    survivors: dict[str, DiscordMessageCapture] = {}
    for index, event in enumerate(capture.events):
        if event.evidence_id != f"{expected_id}:post:{index}":
            raise ValueError("Discord surface has inconsistent post identity or order")
        if event.operation == "delete":
            survivors.pop(event.message.message_id, None)
        else:
            survivors[event.message.message_id] = event.message
    if ids != list(survivors) or any(
        message.model_dump(exclude={"observed_s"})
        != survivors[message.message_id].model_dump(exclude={"observed_s"})
        for message in capture.messages
    ):
        raise ValueError("Discord surviving text differs from the actual post history")
    if capture.text_capture_complete and (
        not capture.post_capture_complete or any(m.attachment_names for m in capture.messages)
    ):
        raise ValueError("Discord text coverage contains an unobserved boundary")
    texts: list[VisibleText] = []
    for message in capture.messages:
        text = "\n".join(
            part
            for part in (message.content, *message.embed_texts, *message.component_labels)
            if part
        )
        if text.strip():
            texts.append(
                VisibleText(
                    evidence_id=f"{expected_id}:message:{message.message_id}",
                    message_id=message.message_id,
                    order=message.order,
                    observed_s=message.observed_s,
                    text=text,
                )
            )
    # Construct, rather than model_copy: retain strict clock/order validation.
    return TurnEvidence.model_validate(
        {
            **evidence.model_dump(),
            "texts": tuple(texts),
            "text_capture_complete": capture.text_capture_complete and not accounting_required,
        }
    )
