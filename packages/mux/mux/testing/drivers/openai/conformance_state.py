"""Offline restart bridge: real native delivery, restartable fenced host intent.

This bridge is explicitly injected by the offline adapter. It does not replace
the production host's Postgres persistence or enable automatic redelivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from mux.contracts.actions import InputEvent
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProviderBinding
from mux.drivers.openai._common import Context, objects, owned, text
from mux.drivers.openai.sessions import OpenAISessions
from mux.drivers.openai.transport import segment
from mux.drivers.openai.turn import OpenAIEvents, RecoveryJournal
from mux.errors import ProviderError
from mux.state.operations import SendClaimed, request_digest
from mux.state.store import StateStore, binding_slot

NOW = datetime(2026, 10, 9, tzinfo=UTC)


class OpenAIConformanceEvents(OpenAIEvents):
    """No native key lookup exists; ambiguous input stays outcome_unknown.

    A native 202 can durably establish accepted status without an input ID.
    After a restart, saved text/current turn cannot associate it with a key.
    Observe owned native inventory, but never manufacture that association or
    issue another POST. This is the conservative branch of the shared restart probe.
    """

    def __init__(
        self,
        context: Context,
        sessions: OpenAISessions,
        journal: RecoveryJournal,
        *,
        store: Callable[[], StateStore],
        binding: Callable[[], ProviderBinding],
    ) -> None:
        super().__init__(context, sessions, journal)
        self._store, self._binding = store, binding

    async def _observe_unknown(self, scope: Scope, session: ResourceRef) -> None:
        await self._sessions.retrieve(scope, session)
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(5):
            params: dict[str, str | int] = {"order": "asc", "limit": 100}
            if cursor is not None:
                params["after"] = cursor
            raw = await self._c.call(
                "GET", f"/agents/sessions/{segment(session.id)}/items", params=params
            )
            values = objects(raw["data"])
            if len(values) > 100:
                raise ValueError("native item page exceeds bound")
            if raw["has_more"] is False:
                return
            if raw["has_more"] is not True or not values:
                raise ValueError("invalid native item inventory")
            cursor = text(raw["last_id"])
            if cursor in seen or values[-1].get("id") != cursor:
                raise ValueError("native item cursor is inconsistent")
            seen.add(cursor)
        raise ProviderError("upstream", retryable=False, native_code="input_inventory_bound")

    @owned
    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        self._c.check(scope, session, "session")
        if expected_turn is not None:
            raise self._c.unsupported("expected_turn")
        slot = binding_slot(self._binding())
        store = self._store()
        begun = await store.begin_operation(
            scope,
            key=key,
            operation_id=key,
            slot=slot,
            request_digest=request_digest(
                {
                    "session": session.model_dump(mode="json"),
                    "events": [event.model_dump(mode="json") for event in events],
                    "expected_turn": expected_turn,
                }
            ),
            now=NOW,
        )
        if begun.record.operation.status != "pending":
            async with asyncio.timeout(8):
                await self._observe_unknown(scope, session)
            return SendReceipt(
                operation_id=begun.record.operation.id, status="outcome_unknown", input_ids=()
            )
        fence = await store.acquire_lease(
            slot, holder="reference", turn_id="root", now=NOW, ttl=timedelta(minutes=5)
        )
        try:
            await store.claim_send(scope, key, now=NOW, fence=fence)
        except SendClaimed:
            return SendReceipt(
                operation_id=begun.record.operation.id, status="outcome_unknown", input_ids=()
            )
        receipt = await super().send(scope, session, events, key=key)
        await store.advance_operation(
            scope,
            key,
            "accepted" if receipt.status == "queued" else "outcome_unknown",
            now=NOW,
            fence=fence,
            resource=session,
            result={"input_ids": list(receipt.input_ids), "turn_id": receipt.turn_id},
        )
        return receipt
