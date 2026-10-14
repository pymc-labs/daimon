"""Local draft display from neutral events; no history or terminal authority."""

from mux.contracts.events import AgentMessageDeltaPayload, AgentMessagePayload, Event, TextPart
from mux.profiles import emits_legacy_events_for


class ProviderDraft:
    """Keep previews ordered and let authoritative snapshots replace them."""

    def __init__(self) -> None:
        self._revisions: dict[str, int] = {}
        self._retired: set[str] = set()
        self._item: str | None = None
        self._parts: dict[int, str] = {}
        self._sequences: dict[int, int] = {}

    def text(self, event: Event) -> str | None:
        if emits_legacy_events_for(event.native.provider) or event.thread_id is not None:
            return None
        payload = event.typed_payload()
        if isinstance(payload, AgentMessagePayload) and event.authority in {
            "record",
            "reconciled",
        }:
            if payload.revision <= self._revisions.get(payload.item_id, 0):
                return None
            self._revisions[payload.item_id] = payload.revision
            if not self._select(payload.item_id):
                return None
            return "".join(
                part.text for part in payload.content if isinstance(part, TextPart)
            ).strip()
        if not isinstance(payload, AgentMessageDeltaPayload) or event.authority != "preview":
            return None
        if payload.item_id in self._revisions or not self._select(payload.item_id):
            return None
        if payload.preview_sequence <= self._sequences.get(payload.content_index, -1):
            return None
        self._sequences[payload.content_index] = payload.preview_sequence
        self._parts[payload.content_index] = (
            self._parts.get(payload.content_index, "") + payload.text
        )
        return "".join(self._parts[index] for index in sorted(self._parts)).strip()

    def _select(self, item: str) -> bool:
        if item in self._retired:
            return False
        if item != self._item:
            if self._item is not None:
                self._retired.add(self._item)
            self._item = item
            self._parts.clear()
            self._sequences.clear()
        return True
