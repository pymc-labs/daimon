"""Slack chat-initiated credential requests — click gate, modals, submissions.

The MCP process posts a message with a single button (`SLACK_ACTION_ID`,
token in the button's `value`); this module is the bot-process half that
dispatches the click. Slack's interaction model needs no loading-modal
dance: the click's `block_actions` payload carries a `trigger_id` that opens
the kind's modal directly, so the pre-open checks below run inline before
`views_open`.

Authorization mirrors the Discord `CredentialRequestButton` exactly:
requester-only for every kind (the click's user must match the row's
`requester_platform_user_id`), expiry and single-use checked at click time,
and — for the `repo` kind only — a shared-agent admin gate, run once as a
pre-filter before the modal opens and once more at submission before the
consume. There is deliberately NO admin gate for the env/mcp/skill_repo
kinds; see `tools/credential_requests.py` in the MCP adapter for the
documented trade.

Secret hygiene (the same structural guarantees the Discord modals and the
panel's paste form document): the submitted value exists only in the modal's
input state and the decision object's own field — it never enters a log
record (env logs the key name; mcp/repo log a masked tail), an `action_id`,
`private_metadata`, or any non-ephemeral message.

The atomic single-use consume runs BEFORE every write, so a request can only
ever produce one write no matter how many times its modal is (re)submitted —
the loser of a race, or any resubmission, gets `None` back and writes
nothing.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final, cast

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.slack.admin import (
    _dev_allow_all_admin,  # pyright: ignore[reportPrivateUsage]
    resolve_is_admin,
)
from daimon.adapters.slack.agent_setup.write import (
    load_agent_inline_pat,
    mask_tail,
    store_inline_pat,
)
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.credential_requests import (
    MAX_SLACK_BUTTON_LABEL_CHARS,
    CredentialRequestKind,
    build_button_label,
    split_skill_repo_target,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, find_attach_mount_collision
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import is_public_repo, pat_can_access_repo
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mcp_attach import attach_mcp_server_to_agent
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import CredentialRequestRow, RepoAccessProof
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "CRED_CALLBACK_PREFIX",
    "CredentialSubmissionDecision",
    "build_credential_modal",
    "evaluate_credential_submission",
    "handle_credential_request_click",
    "run_env_credential_submission",
    "run_mcp_credential_submission",
    "run_repo_bind_credential_submission",
    "run_skill_repo_credential_submission",
]

log = structlog.get_logger()

CRED_CALLBACK_PREFIX: Final[str] = "credential_request__"

_VALUE_BLOCK = "credential__value"
_BRANCH_BLOCK = "credential__branch"
_PAT_BLOCK = "credential__pat"

# Byte cap shared with the panel's paste form (`agent_setup/submit.py`'s
# _MAX_SECRET_VALUE_BYTES) and the Discord credential modals. Slack's own
# max_length on the input is a character cap; this is the byte boundary the
# store actually enforces.
_MAX_SECRET_VALUE_BYTES: Final[int] = 4096

# Character-identical to the Discord button's refusal strings on purpose —
# the two surfaces answer the same lifecycle states with the same words.
_NO_LONGER_VALID = "This request is no longer valid — ask again."
_WRONG_REQUESTER = "This request was for someone else — ask again in your own thread."
_EXPIRED = "This request expired — ask again."
_ALREADY_USED = "This request was already used — ask again."
_WRONG_WORKSPACE = (
    "This request isn't for this workspace — ask again from the workspace it was posted in."
)

# Matches the panel gate's `_SHARED_AGENT_MESSAGE` in spirit; the request row
# carries a derived agent uuid rather than a roster entry, so the gate below
# re-derives the panel's decision from primitives, as Discord's
# `credential_repo_bind` does.
_SHARED_AGENT_MESSAGE = (
    ":lock: This agent is shared — it is either the workspace's built-in agent or the "
    "current default for this workspace or a channel — so changing its repo or its "
    "environment variables needs workspace-admin permission. Fork it to get an "
    "editable copy you own; the fork starts with no environment variables of its own."
)

_AGENT_GONE_MESSAGE = "That agent no longer exists — ask again and a fresh request will be posted."

_MODAL_TITLE: Final[dict[CredentialRequestKind, str]] = {
    "env": "Add secret",
    "mcp": "Add MCP credential",
    "skill_repo": "Import skills",
    "repo": "Bind repo",
}

_VALUE_LABEL: Final[dict[CredentialRequestKind, str]] = {
    "env": "Secret value",
    "mcp": "Auth token",
    "skill_repo": "GitHub token",
}


def build_credential_modal(
    *,
    kind: CredentialRequestKind,
    token: str,
    channel_id: str,
    message_ts: str,
    target: str,
) -> dict[str, Any]:
    """The per-kind modal opened from a live request's button click.

    Every routing field (agent, key/server/repo name) is already fixed by the
    request row keyed by ``token``, so the secret kinds collect exactly one
    field — the value itself. The repo kind collects branch + optional token,
    the two writable parts of a binding. ``private_metadata`` carries only
    routing handles (token, channel, the button message's ts) — never a
    secret, and never the target.
    """
    if kind == "repo":
        blocks: list[dict[str, Any]] = [
            {
                "type": "input",
                "block_id": _BRANCH_BLOCK,
                "label": {"type": "plain_text", "text": "Branch"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _BRANCH_BLOCK,
                    "initial_value": "main",
                    "max_length": 255,
                },
            },
            {
                "type": "input",
                "block_id": _PAT_BLOCK,
                "optional": True,
                "label": {"type": "plain_text", "text": "GitHub token (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _PAT_BLOCK,
                    "max_length": 255,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Leave blank for a public repo",
                    },
                },
            },
        ]
    else:
        placeholder = "usable by everyone who talks to this agent"
        if kind == "skill_repo":
            url, _branch, _path = split_skill_repo_target(target)
            placeholder = f"Needs read access to {normalize_owner_repo(url)}"
        blocks = [
            {
                "type": "input",
                "block_id": _VALUE_BLOCK,
                "label": {"type": "plain_text", "text": _VALUE_LABEL[kind]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _VALUE_BLOCK,
                    "multiline": kind == "env",
                    "max_length": 4000 if kind == "env" else 255,
                    "placeholder": {
                        "type": "plain_text",
                        # Slack caps plain_text placeholders at 150 chars.
                        "text": placeholder[:150],
                    },
                },
            }
        ]
    return {
        "type": "modal",
        "callback_id": f"{CRED_CALLBACK_PREFIX}{kind}",
        "private_metadata": json.dumps(
            {"token": token, "channel_id": channel_id, "message_ts": message_ts},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": _MODAL_TITLE[kind]},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


@dataclasses.dataclass(frozen=True)
class CredentialSubmissionDecision:
    """Outcome of the pure pre-ack evaluation of a credential view_submission.

    ``response_payload`` is the ack body (``response_action: errors``) when
    the submission is rejected, or None for an empty ack that closes the
    modal. ``value`` is the secret and is carried in memory only — it must
    never be logged. ``branch`` is meaningful for the repo kind only.
    """

    proceed: bool
    response_payload: dict[str, Any] | None
    kind: CredentialRequestKind
    value: str
    branch: str
    token: str
    channel_id: str
    message_ts: str


def _input_value(values: dict[str, Any], block_id: str) -> str:
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(block_id) or {}
    return str(element.get("value") or "")


def evaluate_credential_submission(payload: dict[str, Any]) -> CredentialSubmissionDecision:
    """Pure (no I/O) evaluation of a credential_request__* view_submission.

    Rejects an empty or whitespace-only secret with a field error so the
    person can retype rather than lose the modal, and enforces the same byte
    cap the panel's paste form does — Slack's ``max_length`` is a character
    cap and multi-byte input can clear it while overflowing the store.
    The repo kind's secret field is optional (blank = public repo) and a
    blank branch defaults to main.
    """
    view: dict[str, Any] = payload.get("view") or {}
    callback_id = str(view.get("callback_id") or "")
    kind_str = callback_id.removeprefix(CRED_CALLBACK_PREFIX)
    kind: CredentialRequestKind = kind_str  # type: ignore[assignment]  # validated by the dispatch prefix match
    meta: dict[str, Any]
    try:
        meta = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        meta = {}
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    token = str(meta.get("token") or "")
    channel_id = str(meta.get("channel_id") or "")
    message_ts = str(meta.get("message_ts") or "")

    def _decision(
        *, proceed: bool, errors: dict[str, str] | None = None, value: str = "", branch: str = ""
    ) -> CredentialSubmissionDecision:
        return CredentialSubmissionDecision(
            proceed=proceed,
            response_payload=(
                {"response_action": "errors", "errors": errors} if errors is not None else None
            ),
            kind=kind,
            value=value,
            branch=branch,
            token=token,
            channel_id=channel_id,
            message_ts=message_ts,
        )

    if kind == "repo":
        branch = _input_value(values, _BRANCH_BLOCK).strip() or "main"
        pat = _input_value(values, _PAT_BLOCK).strip()
        if len(pat.encode()) > _MAX_SECRET_VALUE_BYTES:
            return _decision(
                proceed=False,
                errors={_PAT_BLOCK: f"Token is too large. Max {_MAX_SECRET_VALUE_BYTES} bytes."},
            )
        return _decision(proceed=True, value=pat, branch=branch)

    raw_value = _input_value(values, _VALUE_BLOCK)
    if not raw_value.strip():
        return _decision(
            proceed=False,
            errors={_VALUE_BLOCK: "Value cannot be empty — try again."},
        )
    if len(raw_value.encode()) > _MAX_SECRET_VALUE_BYTES:
        return _decision(
            proceed=False,
            errors={_VALUE_BLOCK: f"Value is too large. Max {_MAX_SECRET_VALUE_BYTES} bytes."},
        )
    return _decision(proceed=True, value=raw_value)


async def _post_ephemeral(
    client: AsyncWebClient, *, channel_id: str, user_id: str, text: str
) -> None:
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id, user=user_id, text=text
    )


async def _refuse_if_shared_and_not_admin_for_request(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    channel_id: str,
    user_id: str,
) -> bool:
    """Click/submit-time re-check for the chat-initiated repo-bind write.

    Re-derives the panel gate's (`agent_setup.gate.refuse_if_shared_and_not_admin`)
    decision from primitives — a request row carries a derived agent uuid, not
    the roster name the panel gate expects. The branch ORDER mirrors both the
    panel gate and Discord's `credential_repo_bind` twin exactly:

    1. A live workspace admin -> allow, before any MA or DB read — what keeps
       an admin able to bind a repo to the workspace's built-in agent.
    2. The row's derived uuid resolving to no live MA agent -> refuse, fail
       closed (archived or deleted since the mint).
    3. A defaults-managed agent -> refuse; every member shares it.
    4. Otherwise read reachability fresh and refuse when the agent currently
       resolves for the workspace or some channel.

    Returns True when the caller must return immediately (refused).
    """
    if await resolve_is_admin(client, user_id=user_id, dev_allow_all=_dev_allow_all_admin(runtime)):
        return False
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        log.warning(
            "credential_request.agent_gone",
            tenant_id=str(tenant_id),
            agent_id=str(agent_id),
        )
        await _post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=_AGENT_GONE_MESSAGE
        )
        return True
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        await _post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=_SHARED_AGENT_MESSAGE
        )
        return True
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=agent.name,
            default=runtime.deployment_default,
        )
    if reachable:
        await _post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=_SHARED_AGENT_MESSAGE
        )
        return True
    return False


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
    if row is None:
        refusal = _NO_LONGER_VALID
    elif derive_tenant_uuid(platform="slack", workspace_id=team_id) != row.tenant_id:
        # Defense in depth, as on Discord: the posted button lives in the
        # workspace the mint named, so a cross-workspace click stays
        # unreachable by construction rather than by luck.
        refusal = _WRONG_WORKSPACE
    elif user_id != row.requester_platform_user_id:
        refusal = _WRONG_REQUESTER
    elif row.expires_at < datetime.now(UTC):
        refusal = _EXPIRED
    elif row.used_at is not None:
        refusal = _ALREADY_USED
    if refusal is not None or row is None:
        await _post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=refusal or _NO_LONGER_VALID
        )
        return

    if row.kind == "repo" and await _refuse_if_shared_and_not_admin_for_request(
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
        ),
    )


async def _mark_button_consumed(
    client: AsyncWebClient,
    *,
    channel_id: str,
    message_ts: str,
    kind: CredentialRequestKind,
    target: str,
) -> None:
    """Swap the request message for a durable consumed marker, in place.

    Kind-agnostic and about the SUBMISSION rather than the write, exactly as
    on Discord: this runs the moment the consume commits, before the
    vault/binding/import after it is known to have worked, and one of those
    failing still leaves the button dead — leaving it looking live invites a
    click that cannot succeed. A failed edit is a downgrade in feedback, not
    in correctness, so it only logs.
    """
    label = build_button_label(kind, target, max_chars=MAX_SLACK_BUTTON_LABEL_CHARS)
    text = f"✓ Received — {label}"
    try:
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            ts=message_ts,
            text=text,
            blocks=[
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": text}],
                }
            ],
        )
    except Exception as err:
        log.warning(
            "credential_request.consumed_edit_failed",
            kind=kind,
            err_type=type(err).__name__,
        )


async def _consume(
    runtime: SlackRuntime, *, token: str, now: datetime
) -> CredentialRequestRow | None:
    async with runtime.sessionmaker() as session, session.begin():
        return await credential_requests_store.consume_credential_request(
            session, token=token, now=now
        )


async def run_env_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
) -> None:
    """Post-ack: atomic consume + agent_files write, in one transaction.

    The consume and the file write commit together, so the first point the
    row is durably spent is also the point the secret is durably stored.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    now = datetime.now(UTC)
    try:
        async with runtime.sessionmaker() as session, session.begin():
            consumed = await credential_requests_store.consume_credential_request(
                session, token=token, now=now
            )
            if consumed is None:
                await _post_ephemeral(
                    client, channel_id=channel_id, user_id=user_id, text=_NO_LONGER_VALID
                )
                return
            await put_agent_file(
                session,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                key=consumed.target,
                content=value,
            )
    except Exception:
        log.exception("credential_request.env_write_failed", key_present=True)
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="Something went wrong — please try again.",
        )
        return

    # Log the key NAME only — never the value.
    log.info("credential_request.env.submit", key=consumed.target)
    await _mark_button_consumed(
        client, channel_id=channel_id, message_ts=message_ts, kind="env", target=consumed.target
    )
    await _post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        text=(
            f"Added `{consumed.target}`. Takes effect on the next session — "
            "anyone who talks to this agent can use it."
        ),
    )


async def run_mcp_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
) -> None:
    """Post-ack: consume, then the vault write and the agent attach.

    The configuration check precedes the consume — an unconfigured daimon-mcp
    must not spend the request. Partial states after the consume are reported
    truthfully: token stored but not attached is not success.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    mcp = runtime.settings.mcp
    if mcp.public_url is None or mcp.jwt_secret is None:
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "daimon-mcp is not configured (public_url / jwt_secret missing) — "
                "credential not written."
            ),
        )
        return

    now = datetime.now(UTC)
    consumed = await _consume(runtime, token=token, now=now)
    if consumed is None:
        await _post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=_NO_LONGER_VALID)
        return

    await _mark_button_consumed(
        client, channel_id=channel_id, message_ts=message_ts, kind="mcp", target=consumed.target
    )

    mcp_server_url = consumed.mcp_server_url
    if mcp_server_url is None:
        log.error("credential_request.mcp_missing_server_url", token_tail=token[-4:])
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="This request is missing its server URL — please ask again.",
        )
        return

    log.info(
        "credential_request.mcp.submit",
        mcp_server_url=mcp_server_url,
        token_masked=mask_tail(value),
    )
    try:
        # Agent-scoped copy first: the server is attached to the AGENT, so
        # every caller's session needs this credential mirrored in at create
        # time. Without this row the server works only for whoever filled in
        # this modal.
        if runtime.turn_deps.fernet is not None:
            await save_agent_mcp_credential(
                sessionmaker=runtime.sessionmaker,
                fernet=runtime.turn_deps.fernet,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                mcp_server_url=mcp_server_url,
                plaintext_token=value,
            )
        else:
            log.warning(
                "credential_request.no_fernet_for_agent_scope",
                mcp_server_url=mcp_server_url,
            )
        await add_external_mcp_credential(
            runtime.anthropic,
            account_id=consumed.account_id,
            agent_id=consumed.agent_id,
            jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
            public_url=str(mcp.public_url),
            mcp_server_url=mcp_server_url,
            token=value,
            now=now,
            session_factory=runtime.sessionmaker,
        )
    except Exception as err:
        log.exception(
            "credential_request.mcp_write_failed",
            mcp_server_url=mcp_server_url,
            err_type=type(err).__name__,
        )
        # Exception class name only — a stringified SDK/network error can
        # carry the request envelope, which is a token-leak surface.
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "Credential request consumed, but storing the auth token failed "
                f"(`{type(err).__name__}`). Ask for a new request to retry."
            ),
        )
        return

    # The vault credential alone is inert: MA rejects an agent whose
    # mcp_servers are not each referenced by an mcp_toolset, so a token
    # stored against a server the agent never declares is unreachable. The
    # request tool is documented as the replacement for attach_mcp_server on
    # auth-required servers, so it owes the attach too.
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id
    )
    if agent is None:
        log.error("credential_request.mcp_agent_not_found", agent_id=str(consumed.agent_id))
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"Auth token stored, but the agent could not be found to attach "
                f"`{mcp_server_url}` to it. The server is not connected yet."
            ),
        )
        return
    try:
        await attach_mcp_server_to_agent(
            runtime.anthropic,
            agent.id,
            server_name=consumed.target,
            url=mcp_server_url,
        )
    except Exception as err:
        log.exception(
            "credential_request.mcp_attach_failed",
            mcp_server_url=mcp_server_url,
            err_type=type(err).__name__,
        )
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"Auth token stored, but attaching `{mcp_server_url}` to the agent "
                f"failed (`{type(err).__name__}`). The server is not connected yet — "
                "ask the agent to attach it, or request a new credential to retry."
            ),
        )
        return

    await _post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        text=(
            f"MCP credential added for `{mcp_server_url}` and attached as "
            f"`{consumed.target}`. Anyone who talks to this agent can use it."
        ),
    )


async def _resolve_repo_binding_credential(
    runtime: SlackRuntime,
    http_client: httpx.AsyncClient,
    *,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    repo_url: str,
    pasted_pat: str | None,
    now: datetime,
) -> tuple[str, RepoAccessProof]:
    """Resolve the clone credential for a chat-initiated repo bind.

    The Slack twin of Discord's `credential_repo_bind.resolve_repo_binding_credential`
    — same order, same messages, same precedence, driven by this adapter's
    own inline-PAT store helpers. There is deliberately no GitHub App tier:
    an App installation is keyed by the repo, not by the tenant doing this
    bind, so its coverage proves nothing about whether *this* binder may read
    the repo.

    Raises `DaimonError` — never a sentinel — before any write when the
    presented credential does not clear the repo it names.
    """
    owner_repo = normalize_owner_repo(repo_url)
    pat = (pasted_pat or "").strip()
    if pat:
        has_access = await pat_can_access_repo(http_client, owner_repo=owner_repo, pat=pat)
        if not has_access:
            raise DaimonError(
                "That token can't access this repo (or the repo doesn't "
                "exist). Paste a PAT that has access, or connect GitHub."
            )
        ma_secret_ref = await store_inline_pat(
            runtime, account_id=account_id, agent_id=agent_id, plaintext_pat=pat
        )
        return ma_secret_ref, RepoAccessProof(kind="pat", at=now, account_id=account_id)

    existing_pat = await load_agent_inline_pat(runtime, agent_id=agent_id)
    if existing_pat is not None:
        covers_new_repo = await pat_can_access_repo(
            http_client, owner_repo=owner_repo, pat=existing_pat
        )
        if not covers_new_repo:
            raise DaimonError(
                "This agent already has a stored GitHub token that can't "
                "access this repo. Paste a token that can, or clear the "
                "stored one, then bind again."
            )
        return f"inline-pat:{agent_id}", RepoAccessProof(kind="pat", at=now, account_id=account_id)

    public = await is_public_repo(http_client, owner_repo=owner_repo)
    if not public:
        raise DaimonError(
            "This repo isn't publicly readable (it's private, or it "
            "doesn't exist) — paste a GitHub token that can read it to "
            "bind it."
        )
    return "anon:", RepoAccessProof(kind="public", at=now, account_id=account_id)


async def _attach_skills_to_requested_agent(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    outcomes: list[ResourceOutcome],
) -> str:
    """Attach the just-imported skills to the agent this request named.

    Importing puts skills in the tenant's shared library; it does not put
    them on an agent. The request row already names the agent, so doing
    only the import leaves the user staring at an agent with no skills and
    no way to tell that anything worked.

    Returns prose rather than raising: the import has already succeeded by
    the time this runs, so a failure here is partial and both halves must
    be reported truthfully.
    """
    skill_ids = sorted(
        outcome.anthropic_id
        for outcome in outcomes
        if outcome.anthropic_id is not None and outcome.action in (Action.CREATED, Action.UPDATED)
    )
    if not skill_ids:
        return "Nothing new to attach."
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        return "Could not attach: that agent no longer exists. The skills are in the library."
    new_skills: list[BetaManagedAgentsSkillParams] = [
        {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
    ]

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        merged = merge_skills_with_ma(new_skills, fresh)
        collision = await find_attach_mount_collision(
            runtime.anthropic, tenant_id=tenant_id, skills=merged
        )
        if collision is not None:
            raise DaimonError(f"cannot attach: {collision}")
        return await runtime.anthropic.beta.agents.update(
            fresh.id, version=fresh.version, skills=merged
        )

    try:
        await update_agent_with_version_retry(runtime.anthropic, agent.id, _apply)
    except (DaimonError, anthropic.APIStatusError) as err:
        log.warning(
            "credential_request.skill_repo_attach_failed",
            agent_id=str(agent_id),
            err_type=type(err).__name__,
        )
        return f"Imported, but attaching to `{agent.name}` failed ({type(err).__name__})."
    return f"Attached {len(skill_ids)} to `{agent.name}`."


async def run_skill_repo_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
) -> None:
    """Post-ack: consume, verify the token against the SKILL repo, store,
    bind, re-run the import, and attach the imported skills to the agent.

    Mirrors Discord's `SkillRepoModal` — no `set_binding`-free variant here
    either: the skill-sync resolver finds a per-agent token by walking the
    tenant's `agent_repo_binding` rows for the repo, so storing the
    credential without binding leaves every later sync resolving no token.
    No admin gate, matching the env/mcp kinds — `sync_skills` itself gates
    imports at request time.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    now = datetime.now(UTC)
    consumed = await _consume(runtime, token=token, now=now)
    if consumed is None:
        await _post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=_NO_LONGER_VALID)
        return

    await _mark_button_consumed(
        client,
        channel_id=channel_id,
        message_ts=message_ts,
        kind="skill_repo",
        target=consumed.target,
    )

    url, branch, path = split_skill_repo_target(consumed.target)
    log.info(
        "credential_request.skill_repo.submit",
        repo_url=url,
        branch=branch,
        path=path,
        pat_masked=mask_tail(value),
    )

    try:
        # Verify BEFORE storing: a token that cannot read this repo is not a
        # credential for it, and storing it would shadow a working one on the
        # next `get_pat` (the overlay is last-write-wins).
        if not await pat_can_access_repo(
            runtime.http_client, owner_repo=normalize_owner_repo(url), pat=value
        ):
            await _post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=(
                    f"That token cannot read `{normalize_owner_repo(url)}`. Nothing was "
                    "stored, and the request was used up — ask again to retry."
                ),
            )
            return
        ma_secret_ref, proof = await _resolve_repo_binding_credential(
            runtime,
            runtime.http_client,
            agent_id=consumed.agent_id,
            account_id=consumed.account_id,
            repo_url=url,
            pasted_pat=value,
            now=now,
        )
        async with runtime.sessionmaker.begin() as session:
            await set_binding(
                session,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                repo_url=url,
                default_branch=branch,
                ma_secret_ref=ma_secret_ref,
                proof=proof,
            )
        outcomes = await run_skill_sync(
            runtime.anthropic,
            runtime.http_client,
            url=url,
            branch=branch,
            path=path,
            tenant_id=consumed.tenant_id,
            token=value,
        )
    except DaimonError as err:
        # Written for the user by the raiser — surface verbatim.
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"Token stored, but the import failed: {err} The request was used up — "
                "ask again to retry the import."
            ),
        )
        return
    except Exception as err:
        log.exception(
            "credential_request.skill_repo_sync_failed",
            repo_url=url,
            err_type=type(err).__name__,
        )
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"Token stored, but the import failed (`{type(err).__name__}`). "
                "Ask again to retry the import."
            ),
        )
        return

    attach_note = await _attach_skills_to_requested_agent(
        runtime, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id, outcomes=outcomes
    )
    await _post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        text=(
            f"Imported {len(outcomes)} skill(s) from `{normalize_owner_repo(url)}`. "
            f"{attach_note} The token is stored and the repo is bound, so future "
            "imports from it will not ask again."
        ),
    )


async def run_repo_bind_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
    branch: str,
) -> None:
    """Post-ack: gate, atomic consume, credential resolution, binding write.

    The shared-agent admin gate runs again here — immediately before the
    consume — rather than being trusted from the click-time pre-filter: a
    member who was an admin when the button was clicked may have lost it
    between click and submit. This call is the authorization boundary.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    now = datetime.now(UTC)
    async with runtime.sessionmaker() as session:
        row = await credential_requests_store.peek_credential_request(session, token=token)
    if row is None:
        await _post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=_NO_LONGER_VALID)
        return

    if await _refuse_if_shared_and_not_admin_for_request(
        runtime,
        client,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        channel_id=channel_id,
        user_id=user_id,
    ):
        return

    consumed = await _consume(runtime, token=token, now=now)
    if consumed is None:
        await _post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=_NO_LONGER_VALID)
        return

    await _mark_button_consumed(
        client, channel_id=channel_id, message_ts=message_ts, kind="repo", target=consumed.target
    )

    pat = value.strip()
    # Log the repo and branch, and the token ONLY as a masked tail when
    # present — never the plain value, never the (now-consumed) request token.
    log.info(
        "credential_request.repo.submit",
        repo_url=consumed.target,
        branch=branch,
        pat_masked=mask_tail(pat) if pat else None,
    )

    try:
        ma_secret_ref, proof = await _resolve_repo_binding_credential(
            runtime,
            runtime.http_client,
            agent_id=consumed.agent_id,
            account_id=consumed.account_id,
            repo_url=consumed.target,
            pasted_pat=pat or None,
            now=now,
        )
        async with runtime.sessionmaker.begin() as session:
            await set_binding(
                session,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                repo_url=consumed.target,
                default_branch=branch,
                ma_secret_ref=ma_secret_ref,
                proof=proof,
            )
    except DaimonError as err:
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=f"{err} The request was used up — ask again to retry.",
        )
        return
    except Exception as err:
        log.exception(
            "credential_request.repo_write_failed",
            repo_url=consumed.target,
            err_type=type(err).__name__,
        )
        await _post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "Credential request consumed, but binding the repo failed "
                f"(`{type(err).__name__}`). Ask for a new request to retry."
            ),
        )
        return

    await _post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        text=f"Bound `{consumed.target}` on `{branch}`. Takes effect on the next session.",
    )
