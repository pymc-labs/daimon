"""Slack chat-initiated credential requests — the click gate.

The MCP process posts a message with a single button (`SLACK_ACTION_ID`,
token in the button's `value`); this module is the bot-process half that
dispatches the click. Slack's interaction model needs no loading-modal
dance: the click's `block_actions` payload carries a `trigger_id` that opens
the kind's modal directly, so the pre-open checks below run inline before
`views_open`.

The forms themselves live in `credential_forms.py` and the post-ack runners
in `credential_submissions.py`; this module re-exports both, so `app.py`,
the tests and the parity drivers keep importing every public name from here.

Authorization mirrors the Discord `CredentialRequestButton` exactly:
requester-only for every kind (the click's user must match the row's
`requester_platform_user_id`), expiry and single-use checked at click time,
and — for the `repo` kind — a shared-agent admin gate, run once as a
pre-filter before the modal opens and once more at submission before the
consume. The `skill_repo` kind is gated at submission only; the MCP mint
already refused it for a member on a shared agent. There is deliberately NO
admin gate for the env/mcp kinds; see `tools/credential_requests.py` in the
MCP adapter for the documented trade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import anthropic
from daimon.adapters.slack.credential_forms import (
    CRED_CALLBACK_PREFIX,
    CredentialSubmissionDecision,
    build_credential_modal,
    evaluate_credential_submission,
    expired_refusal,
)
from daimon.adapters.slack.credential_submissions import (
    ContinuationTrigger,
    post_ephemeral,
    refuse_if_shared_and_not_admin_for_request,
    run_env_credential_submission,
    run_env_file_credential_submission,
    run_mcp_credential_submission,
    run_repo_bind_credential_submission,
    run_skill_repo_credential_submission,
)
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.posted_controls import edit_posted_card
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_pins import (
    FormPinRefused,
    consume_form_unless_pinned,
    request_pin_refusal,
)
from daimon.core.credential_requests import (
    CredentialRequestKind,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mcp_oauth import INVITE_BUTTON_LABEL, begin_mcp_oauth_flow, invite_copy, start_url
from daimon.core.posted_controls import (
    ALREADY_USED_MESSAGE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
)
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores.domain import CredentialRequestRow
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "CRED_CALLBACK_PREFIX",
    "ContinuationTrigger",
    "CredentialSubmissionDecision",
    "build_credential_modal",
    "evaluate_credential_submission",
    "handle_credential_request_click",
    "run_env_credential_submission",
    "run_env_file_credential_submission",
    "run_mcp_credential_submission",
    "run_repo_bind_credential_submission",
    "run_skill_repo_credential_submission",
]

_WRONG_WORKSPACE = (
    "This request isn't for this workspace — ask again from the workspace it was posted in."
)


_UNCONFIGURED_OAUTH = (
    "This deployment cannot sign you in yet. Ask the operator to finish the daimon-mcp "
    "setup, then ask again. Nothing was saved."
)


async def start_mcp_oauth_from_click(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    row: CredentialRequestRow,
    channel_id: str,
    user_id: str,
) -> None:
    """Answer an `mcp_oauth` click with the requester's private sign-in link.

    The request row is spent atomically (a second click gets "already used")
    and the link rides an ephemeral only the requester sees; the card itself
    is edited by the mcp process once sign-in completes.
    """
    thread_ts = row.origin_thread_id
    mcp = runtime.settings.mcp
    app_root_url = mcp.app_root_url
    # A superset of the route mount's predicate: never hand out a dead link.
    if app_root_url is None or mcp.jwt_secret is None or runtime.turn_deps.fernet is None:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=_UNCONFIGURED_OAUTH,
            thread_ts=thread_ts,
        )
        return
    try:
        agent = await find_agent_by_derived_uuid(
            runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
    except anthropic.APIError:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="I couldn't verify this agent. Nothing was saved; please try again.",
            thread_ts=thread_ts,
        )
        return
    async with runtime.sessionmaker() as session:
        pin_refusal = await request_pin_refusal(session, row=row, agent=agent)
    if pin_refusal is not None:
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=pin_refusal, thread_ts=thread_ts
        )
        return
    now = datetime.now(UTC)
    try:
        async with runtime.sessionmaker() as session, session.begin():
            consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
            flow = (
                await begin_mcp_oauth_flow(
                    session, request=consumed, app_root_url=app_root_url, now=now
                )
                if consumed is not None
                else None
            )
    except FormPinRefused as refused:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
            thread_ts=thread_ts,
        )
        return
    if consumed is None or flow is None:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
            thread_ts=thread_ts,
        )
        return
    # Same as Discord: the spent row's card stops offering a live button.
    await edit_posted_card(client, row=consumed, state="received")
    text = invite_copy(server_name=consumed.target, agent_name=consumed.target_name or "the agent")
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        user=user_id,
        thread_ts=thread_ts,
        text=text,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": INVITE_BUTTON_LABEL},
                        "url": start_url(app_root_url, state=flow.state),
                        "action_id": "mcp_oauth_open",
                    }
                ],
            },
        ],
    )


async def handle_credential_request_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Dispatch a credential-request button click to the kind's modal.

    The same lifecycle checks Discord's `interaction_check` runs, in the same
    order: unknown token, wrong requester, expired, already used — each
    answered with an ephemeral, never a modal. The repo kind additionally
    runs the shared-agent admin gate as a pre-filter, so a member who was
    always going to be refused is never asked to paste a token into a form
    that gets thrown away. The submission re-runs every check that matters
    (the consume is atomic; the repo gate runs again) — this pre-filter is
    UX, not the authorization boundary.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    channel_id = str(channel_info.get("id") or "")
    message_ts = str(container.get("message_ts") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    token = str(actions[0].get("value") or "") if actions else ""

    if not (team_id and user_id and channel_id and message_ts and trigger_id and token):
        return

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    async with runtime.sessionmaker() as session:
        row = await credential_requests_store.peek_credential_request(session, token=token)

    refusal: str | None = None
    is_expired = False
    if row is None:
        refusal = NO_LONGER_VALID_MESSAGE
    elif derive_tenant_uuid(platform="slack", workspace_id=team_id) != row.tenant_id:
        # Defense in depth, as on Discord: the posted button lives in the
        # workspace the mint named, so a cross-workspace click stays
        # unreachable by construction rather than by luck.
        refusal = _WRONG_WORKSPACE
    elif row.platform is not None and row.platform != "slack":
        refusal = _WRONG_WORKSPACE
    elif (
        row.parent_channel_id is not None
        and row.parent_channel_id != channel_id
        or row.posted_message_id is not None
        and row.posted_message_id != message_ts
    ):
        refusal = NO_LONGER_VALID_MESSAGE
    elif user_id != row.requester_platform_user_id:
        refusal = WRONG_REQUESTER_MESSAGE
    elif row.expires_at < datetime.now(UTC):
        refusal = expired_refusal(row)
        is_expired = True
    elif row.used_at is not None:
        refusal = ALREADY_USED_MESSAGE
    if refusal is not None or row is None:
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=refusal or NO_LONGER_VALID_MESSAGE
        )
        if is_expired and row is not None:
            # Opportunistic, and the only sweep there is: nothing walks
            # expired rows, so the first late click is the one chance to stop
            # the card advertising a form that can no longer open. Only the
            # expiry branch edits — a wrong-requester click must not be able
            # to change what the requester's own card says.
            await edit_posted_card(client, row=row, state="expired")
        return

    if row.kind == "mcp_oauth":
        # No modal: the value is a browser sign-in. The requester gets a
        # private link and the mcp process finishes the rest.
        await start_mcp_oauth_from_click(
            runtime, client, row=row, channel_id=channel_id, user_id=user_id
        )
        return

    if row.kind == "repo" and await refuse_if_shared_and_not_admin_for_request(
        runtime,
        client,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        channel_id=channel_id,
        user_id=user_id,
    ):
        return

    await client.views_open(  # pyright: ignore[reportUnknownMemberType]
        trigger_id=trigger_id,
        view=build_credential_modal(
            kind=cast("CredentialRequestKind", row.kind),
            token=token,
            channel_id=channel_id,
            message_ts=message_ts,
            target=row.target,
            agent_name=row.target_name or "the agent",
            mcp_server_url=row.mcp_server_url,
        ),
    )
