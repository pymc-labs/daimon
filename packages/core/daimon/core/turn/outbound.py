"""Keep another chat app's name out of what a turn posts on a chat platform.

`run_prepared_turn` wraps the adapter's lifecycle so the turn state it renders, the
final answer and any sealed answer reach a platform with the other apps' names already
replaced (`scrub_other_platforms`). The adapters stay unaware, and a lifecycle added for
a fourth platform inherits the rule. The wrapper only touches what the adapter is
handed: the driver, the transcript and the run outcome keep the model's own words.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from anthropic.types import RawMessageStreamEvent
from daimon.core.platform_words import blunt_scrub_other_platforms, scrub_other_platforms
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.state import ContentBlock, TextBlock, TurnState

log = structlog.get_logger()


def scrub_turn_state(
    state: TurnState, platform: str | None, *, known: dict[str, str] | None = None
) -> TurnState:
    """`state` with the model's text scrubbed for `platform`; `state` itself when nothing changes.

    Tool-call blocks are left alone: their names and results are not posted as written.
    `known` maps text already scrubbed to its result, so a render tick re-scrubs only the
    block still growing; it is replaced by this state's texts, which bounds its size."""
    changed = False
    content: list[ContentBlock] = []
    seen: dict[str, str] = {}
    for block in state.content:
        if isinstance(block, TextBlock):
            text = known.get(block.text) if known is not None else None
            if text is None:
                text = scrub_other_platforms(block.text, platform)
            seen[block.text] = text
            if text != block.text:
                block = dataclasses.replace(block, text=text)
                changed = True
        content.append(block)
    if known is not None:
        known.clear()
        known.update(seen)
    return dataclasses.replace(state, content=content) if changed else state


def _blunt_turn_state(state: TurnState, platform: str | None) -> TurnState:
    """`state` with the other apps' names removed bluntly: total, for when the scrub fails."""
    content: list[ContentBlock] = [
        dataclasses.replace(block, text=blunt_scrub_other_platforms(block.text, platform))
        if isinstance(block, TextBlock)
        else block
        for block in state.content
    ]
    return dataclasses.replace(state, content=content)


class OtherAppsScrubbedLifecycle:
    """A `TurnLifecycle` that hands `inner` scrubbed turn state.

    Hooks the protocol leaves optional (`on_acknowledgment`, `on_provider_event`,
    `on_render_stopped`) are looked up on `inner`, so they exist here only when they exist there."""

    def __init__(self, inner: TurnLifecycle, platform: str) -> None:
        self._inner = inner
        self._platform = platform
        self._scrubbed: dict[str, str] = {}

    def _for_adapter(self, state: TurnState) -> TurnState:
        # The render loop hands over the whole state every ~2 s: scrub what grew, not all of it.
        try:
            return scrub_turn_state(state, self._platform, known=self._scrubbed)
        except Exception as err:
            # A reply is never lost to the scrub, and never posted unscrubbed.
            log.error("outbound_scrub.failed", error_type=type(err).__name__)
            self._scrubbed.clear()
            return _blunt_turn_state(state, self._platform)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    async def on_render(self, state: TurnState) -> None:
        await self._inner.on_render(self._for_adapter(state))

    async def on_terminal_success(self, state: TurnState) -> None:
        await self._inner.on_terminal_success(self._for_adapter(state))

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await self._inner.on_terminal_failure(self._for_adapter(state), err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self._inner.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self._inner.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self._inner.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self._inner.on_interrupt_sent(source)


def scrubbed_for(
    platform: str,
    lifecycle: TurnLifecycle,
    recovery_lifecycle: Callable[[asyncio.Event], TurnLifecycle],
) -> tuple[TurnLifecycle, Callable[[asyncio.Event], TurnLifecycle]]:
    """The turn's lifecycle and its recovery lifecycle, scrubbed when `platform` is a chat app."""
    if platform not in CHAT_PLATFORMS:
        return lifecycle, recovery_lifecycle
    return (
        OtherAppsScrubbedLifecycle(lifecycle, platform),
        lambda cancel: OtherAppsScrubbedLifecycle(recovery_lifecycle(cancel), platform),
    )
