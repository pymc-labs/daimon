"""`daimon.notify(name, data)`: an event from a page, saved to `_events` and sent to
the page's page-event watches.

The server fills in who sent it and what kind of sender they are, so a page cannot
claim to be someone else, and whether the page is visible outside the workspace, so a
turn knows it writes for outsiders. Each event carries its own id: two identical clicks
are two events. A watch's signed copy is bounded (`MAX_EVENT_BYTES`): a larger event
reaches it as a preview, and the whole event stays readable in `_events` by its id.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from daimon.core.artifact_pages.access import Sender
from daimon.core.artifact_pages.model import ArtifactError, has_json_nul
from daimon.core.continuity.page_events import MAX_EVENT_BYTES
from pydantic import JsonValue

EVENT_NAME: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
MAX_WATCHES_PER_PAGE: Final = 5


@dataclass(frozen=True)
class PageEvent:
    id: uuid.UUID
    document: dict[str, JsonValue]  # saved at _events/<id>
    body: bytes  # signed and delivered to each watch


def build_event(
    *,
    slug: str,
    name: str,
    data: JsonValue,
    sender: Sender,
    by: str | None,
    visible_outside_workspace: bool,
    event_id: uuid.UUID,
    now: datetime,
    max_bytes: int,
) -> PageEvent:
    """`by` is the sender's name, None for an anonymous viewer; the saved event is at
    most `max_bytes`."""
    if EVENT_NAME.match(name) is None:
        raise ArtifactError("invalid", f"Invalid event name {name!r}: 1-64 of A-Z a-z 0-9 _ . : -.")
    document: dict[str, JsonValue] = {
        "event": name,
        "data": data,
        "by": by,
        "sender": sender,
        "page_visible_outside_workspace": visible_outside_workspace,
        "at": now.isoformat(),
    }
    head: dict[str, JsonValue] = {"artifact": slug, "id": str(event_id)}
    text = json.dumps({**head, **document}, ensure_ascii=False)
    if has_json_nul(text):
        raise ArtifactError("invalid", "Events cannot hold the NUL character (\\u0000).")
    body = text.encode()
    if len(body) > max_bytes:
        raise ArtifactError(
            "too_large",
            f"The event is {len(body):,} bytes; the limit is {max_bytes:,}. Save large "
            "data in the page's data and send its id.",
        )
    if len(body) > MAX_EVENT_BYTES:
        body = _preview(head, document)
    return PageEvent(id=event_id, document=document, body=body)


def _preview(head: dict[str, JsonValue], document: dict[str, JsonValue]) -> bytes:
    """The event with its data cut to fit a watch's bounded copy, and where to read it all."""
    data = json.dumps(document["data"], ensure_ascii=False)
    preview: dict[str, JsonValue] = {
        **head,
        **document,
        "data": None,
        "data_truncated": True,
        "read_with": "artifact_db_query on collection _events with doc_id set to this id",
    }
    room = MAX_EVENT_BYTES - len(json.dumps(preview, ensure_ascii=False).encode()) - 32
    cut = data.encode()[: max(room, 0)].decode(errors="ignore")
    body = json.dumps({**preview, "data_preview": cut}, ensure_ascii=False).encode()
    while len(body) > MAX_EVENT_BYTES and cut:
        cut = cut[: len(cut) * 9 // 10]
        body = json.dumps({**preview, "data_preview": cut}, ensure_ascii=False).encode()
    return body
