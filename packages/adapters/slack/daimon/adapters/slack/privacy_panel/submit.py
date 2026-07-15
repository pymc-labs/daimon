"""Privacy delete view_submission handler for Slack (D-05).

Two responsibilities:

1. `evaluate_delete_submission` (PURE, synchronous):
   Validates the typed username against the expected name from private_metadata.
   Returns a DeleteDecision carrying the response_action payload to ack with and
   whether to proceed. Called synchronously before the 3-second Socket Mode ack
   deadline so no I/O runs on the critical path.

2. `run_purge_and_update` (async, background):
   Called AFTER the ack is sent. Purges the account via `purge_account`, emits a
   COUNTS-ONLY operator log (no platform_user_id / user_name — T-82-17), then
   updates the open modal to the post-delete status view.

Port of packages/adapters/discord/daimon/adapters/discord/privacy_panel/modal.py:50-98.
Slack-specific: response_action lives in SocketModeResponse.payload (RESEARCH §Pattern 2),
not an HTTP body. Catch site follows S3 (DaimonError | anthropic.APIError | SlackApiError).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

import anthropic
import structlog
from daimon.adapters.slack.privacy_panel.read import resolve_privacy_account
from daimon.adapters.slack.privacy_panel.views import build_deleting_view, build_post_delete_view
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.purge import purge_account
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class DeleteDecision:
    """Result of evaluate_delete_submission.

    response_payload: dict to pass as SocketModeResponse(payload=...) to ack the submission.
    proceed:          True on username match; False on mismatch or invalid metadata.
    account_id:       Parsed account UUID (only set when proceed=True).
    view_id:          View to update after purge (only set when proceed=True).
    """

    response_payload: dict[str, Any]
    proceed: bool
    account_id: uuid.UUID | None
    view_id: str | None


def evaluate_delete_submission(payload: dict[str, Any]) -> DeleteDecision:
    """Pure: validate the view_submission payload for the privacy delete modal.

    Reads private_metadata (account_id, user_name, view_id) and the submitted
    `confirm_name` value from the view state. Returns:
      - proceed=False with response_action=errors if the typed name does not match
        (mirror modal.py:53-60 — NO purge, no state change)
      - proceed=True with response_action=update to a "Deleting…" view on match

    This function has NO I/O; the caller acks the Socket Mode envelope with the
    returned payload synchronously, then calls run_purge_and_update as a background
    task (D-05, Pitfall 2).
    """
    view: dict[str, Any] = payload.get("view") or {}
    raw_meta: str = view.get("private_metadata") or ""

    try:
        meta: dict[str, Any] = json.loads(raw_meta) if raw_meta else {}
    except (json.JSONDecodeError, ValueError):
        meta = {}

    expected_name: str = str(meta.get("user_name") or "")
    account_id_str: str = str(meta.get("account_id") or "")
    stored_view_id: str = str(meta.get("view_id") or "")

    # The submitted view_id is always available in the payload; prefer it.
    view_id: str = str(view.get("id") or stored_view_id)

    view_state: dict[str, Any] = view.get("state") or {}
    state: dict[str, Any] = view_state.get("values") or {}
    confirm_block: dict[str, Any] = state.get("confirm_name_block") or {}
    confirm_el: dict[str, Any] = confirm_block.get("confirm_name") or {}
    typed: str = str(confirm_el.get("value") or "").strip()

    # Username mismatch → re-display with an error, NO purge (T-82-14 / D-05).
    if not expected_name or typed != expected_name:
        return DeleteDecision(
            response_payload={
                "response_action": "errors",
                "errors": {
                    "confirm_name_block": "That doesn't match your username.",
                },
            },
            proceed=False,
            account_id=None,
            view_id=None,
        )

    # Parse account_id — malformed metadata is treated as a mismatch.
    try:
        account_id = uuid.UUID(account_id_str)
    except (ValueError, AttributeError):
        return DeleteDecision(
            response_payload={
                "response_action": "errors",
                "errors": {
                    "confirm_name_block": ("Invalid session — please re-open the privacy panel."),
                },
            },
            proceed=False,
            account_id=None,
            view_id=None,
        )

    # Match → ack to "Deleting…" view; purge runs in the background after ack.
    return DeleteDecision(
        response_payload={
            "response_action": "update",
            "view": build_deleting_view(),
        },
        proceed=True,
        account_id=account_id,
        view_id=view_id,
    )


async def run_purge_and_update(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    view_id: str,
) -> None:
    """Purge the account and update the modal to the post-delete status view (D-05).

    Runs AFTER the Socket Mode ack (response_action=update / "Deleting…") so the
    ~3s deadline is not at risk (Pitfall 2). Catches DaimonError | anthropic.APIError |
    SlackApiError at the listener boundary (S3). The operator log is COUNTS ONLY —
    no platform_user_id, user_name, or guild_id (T-82-17 / modal.py:82-94 analog).

    Ownership guard: `account_id` arrived via the modal's private_metadata; before
    running an irreversible purge we RE-RESOLVE the account from the authenticated
    submitter (tenant_id from the envelope team, platform_user_id from the actor)
    and refuse if it does not match. This means the destructive key is anchored to
    the submitter's identity, not to a client-supplied metadata blob.
    """
    try:
        async with runtime.sessionmaker() as s:
            resolved = await resolve_privacy_account(
                s, tenant_id=tenant_id, platform_user_id=platform_user_id
            )
        if resolved != account_id:
            log.warning(
                "privacy.purge.account_mismatch",
                requested_account_id=str(account_id),
                resolved_account_id=str(resolved) if resolved is not None else None,
            )
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Privacy"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    "Could not verify your account — nothing was "
                                    "deleted. Please re-open `/privacy`."
                                ),
                            },
                        }
                    ],
                },
            )
            return
        result = await purge_account(
            sm=runtime.sessionmaker,
            account_id=account_id,
            anthropic=runtime.anthropic,
        )
        # COUNTS-ONLY operator log (T-82-17 / modal.py:82-94) — no PII fields.
        log.info(
            "privacy.delete.completed",
            account_id=str(account_id),
            principals=result.db.cli_principals + result.db.platform_principals,
            routines=result.db.routines,
            links=result.db.principal_links,
            user_skills=result.db.user_skills,
            github_credentials=result.db.github_credentials,
            oauth_states=result.db.github_oauth_states,
            sessions_deleted=result.sessions.deleted,
            sessions_failed=result.sessions.failed,
        )
        await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=build_post_delete_view(result),
        )
    except (DaimonError, anthropic.APIError, SlackApiError) as exc:
        log.error(
            "privacy.purge.failed",
            account_id=str(account_id),
            exc_info=exc,
        )
