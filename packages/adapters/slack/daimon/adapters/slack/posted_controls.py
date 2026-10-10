"""Edit a posted control card in place, from the Slack bot process.

The MCP process posts the `requested` card
(`daimon.adapters.mcp.tools.slack._credential_button`); this process owns
every later state of that same message. Both build the card in core and
render it with `build_card_blocks`, so the edit lands the same four slots in
the same order as the initial post rather than collapsing the card to a
one-line marker.

The card's identity is durable: the request row records the channel the card
was posted in and its `ts`, so an edit targets the original card even when
the submission arrives from somewhere else (a different channel's modal, a
restarted bot). A row with no recorded `ts` predates its post or never got
one; there is nothing to edit and nothing to report.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_submit import credential_card_edit, note_credential_save_failure
from daimon.core.posted_controls import (
    CardState,
    RefusalReason,
    build_card_blocks,
    card_for_request,
    card_notification_text,
)
from daimon.core.stores.domain import CredentialRequestRow
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = ["edit_posted_card"]

log = structlog.get_logger()


async def edit_posted_card(
    client: AsyncWebClient,
    *,
    row: CredentialRequestRow,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
    refusal_lines: Sequence[str] = (),
    replaces: str | None = None,
    retry_reason: str | None = None,
    saving_notice: str | None = None,
) -> None:
    """Re-render the request's own message into `state`.

    Same four slots in the same order as the initial post. Only the
    `requested` state carries the button, so only that state hands the token
    to the renderer.

    A Slack refusal (`SlackApiError`) is logged rather than raised: a failed
    edit is a downgrade in feedback, not in correctness — the request row is
    already spent, and the lifecycle the card announces has already happened.
    """
    async with credential_card_edit(row, state):
        if state == "partial":
            note_credential_save_failure(row, outcome)
        if row.posted_message_id is None:
            return
        card = card_for_request(
            row,
            state=state,
            outcome=outcome,
            refusal=refusal,
            refusal_lines=refusal_lines,
            replaces=replaces,
            retry_reason=retry_reason,
            saving_notice=saving_notice,
        )
        try:
            await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                channel=row.parent_channel_id or row.channel_id,
                ts=row.posted_message_id,
                text=card_notification_text(card),
                blocks=build_card_blocks(card, token=row.token if state == "requested" else None),
            )
        except SlackApiError as err:
            log.warning(
                "posted_card.edit_failed",
                state=state,
                kind=row.kind,
                error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
            )
