"""Slack credential-request submissions — one runner per request kind.

The post-ack half of the credential-request surface: everything that runs
after Slack's view_submission has been acked, once
`evaluate_credential_submission` (in `credential_forms.py`) has said the form
carries a usable value.

Authorization mirrors the Discord modals exactly: requester-only for every
kind, expiry and single-use re-checked here rather than trusted from the
click, and — for the `repo` kind only — a shared-agent admin gate run again
immediately before the consume. There is deliberately NO admin gate for the
env/mcp/skill_repo kinds; see `tools/credential_requests.py` in the MCP
adapter for the documented trade.

The atomic single-use consume runs BEFORE every write, so a request can only
ever produce one write no matter how many times its modal is (re)submitted —
the loser of a race, or any resubmission, gets `None` back and writes
nothing.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import (
    AGENT_GONE_MESSAGE,
    gather_target_facts,
    refuse_unless_allowed,
)
from daimon.adapters.slack.agent_setup.write import (
    load_agent_inline_pat,
    store_inline_pat,
)
from daimon.adapters.slack.channel_admin_groups import channel_admin_caller
from daimon.adapters.slack.credential_forms import refusal_text
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.posted_controls import edit_posted_card
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_pins import (
    FormPinRefused,
    agent_pin_names,
    request_pin_refusal,
)
from daimon.core.continuity.continuation import record_input_continuation
from daimon.core.continuity.messages import ConfigurationChange, render_env_import_rejected
from daimon.core.credential_requests import (
    CredentialRequestKind,
    availability_for_request,
    split_skill_repo_target,
)
from daimon.core.credential_submit import (
    apply_env_file_submit,
    apply_env_submit,
    consume_credential_submit,
    prepare_env_submit,
    prepare_mcp_submit,
    settle_credential_submit,
    write_mcp_submit,
    write_repo_submit,
    write_skill_repo_submit,
)
from daimon.core.credential_submit import env_name_refusal as _env_name_refusal
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, find_attach_mount_collision
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.env_file import (
    EnvEntry,
    EnvFileRejected,
    decode_env_bytes,
    env_collision_line,
    env_name_problem,
    parse_env_file,
)
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import is_public_repo, pat_can_access_repo
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.mcp_attach import (
    McpConnectDecision,
    McpServerReplaceRefusedError,
    decide_mcp_connect,
)
from daimon.core.mcp_token_check import is_token_rejected, rejected_token_message
from daimon.core.mcp_token_connect import (
    McpAgentGoneError,
    McpAttachFailedError,
    McpTokenWriteFailedError,
    connect_mcp_server_with_token,
)
from daimon.core.observability import capture_exception_with_scope
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.posted_controls import (
    NO_LONGER_VALID_MESSAGE,
)
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.skills.sync import in_library, library_skill_ids, summarize_failed_imports
from daimon.core.slack_files import fetch_slack_file
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores.agent_files import (
    AgentEnvEncryptionRequiredError,
)
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import CredentialRequestRow, RepoAccessProof
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

__all__ = [
    "ContinuationTrigger",
    "post_ephemeral",
    "refuse_if_shared_and_not_admin_for_request",
    "run_env_credential_submission",
    "run_env_file_credential_submission",
    "run_mcp_credential_submission",
    "run_repo_bind_credential_submission",
    "run_skill_repo_credential_submission",
]

log = structlog.get_logger()

#: What a runner calls once its write is durable: run whatever continuation
#: the origin thread now has pending. Injected rather than resolved here —
#: the per-thread guard, the turn lifecycle and the platform client all live
#: in the bot process, and this module must not reach back into it.
ContinuationTrigger = Callable[[], Awaitable[None]]

# How many colliding key names a refusal names before it summarises the rest,
# matching `render_env_import_rejected`'s own line budget.
_COLLISION_LINES_SHOWN: Final[int] = 3


async def post_ephemeral(
    client: AsyncWebClient,
    *,
    channel_id: str,
    user_id: str,
    text: str,
    thread_ts: str | None = None,
) -> None:
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id, user=user_id, text=text, thread_ts=thread_ts
    )


async def refuse_if_shared_and_not_admin_for_request(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    channel_id: str,
    user_id: str,
    thread_ts: str | None = None,
) -> bool:
    """Click/submit-time re-check for the chat-initiated repo-bind write.

    A thin wrapper over the shared gate in `daimon.adapters.slack.agent_policy`,
    which owns the order this used to spell out: a live workspace admin is
    allowed before any MA or DB read (what keeps an admin able to bind a repo
    to the workspace's built-in agent); a derived uuid that no longer resolves
    to a live MA agent fails closed; then `decide_operation` settles the rest
    of the attachment family, reading reachability only when the outcome turns
    on it.

    Returns True when the caller must return immediately (refused).
    """
    return await refuse_unless_allowed(
        runtime,
        client,
        operation="repo_bind",
        tenant_id=tenant_id,
        agent_id=agent_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
    )


async def _mark_button_consumed(client: AsyncWebClient, *, row: CredentialRequestRow) -> None:
    """Re-render the request's card in the `received` state, in place.

    Kind-agnostic and about the SUBMISSION rather than the write, exactly as
    on Discord: this runs the moment the consume commits, before the
    vault/binding/import after it is known to have worked, and one of those
    failing still leaves the button dead — leaving it looking live invites a
    click that cannot succeed.

    The card keeps its headline and facts and loses only the button, so the
    person who just submitted still sees what they submitted to.
    """
    await edit_posted_card(client, row=row, state="received")


async def _consume(
    runtime: SlackRuntime,
    *,
    request: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    now: datetime,
) -> CredentialRequestRow | None:
    """Spend the form, deciding the pin rule in the same transaction (`FormPinRefused`)."""
    return await consume_credential_submit(runtime.sessionmaker, row=request, agent=agent, now=now)


async def _dispatch_pending(trigger: ContinuationTrigger, *, kind: str) -> None:
    """Run the origin thread's pending continuations. Never fails the save.

    The write has already committed by the time this runs, so a dispatch that
    cannot start is a delay and not a loss: the next completed turn in that
    thread reaches the same dispatcher and picks the row up. Raising here
    would only turn a delivered save into a logged exception.
    """
    try:
        await trigger()
    except (DaimonError, anthropic.APIError, SlackApiError) as err:
        log.warning(
            "credential_request.continuation_dispatch_failed",
            kind=kind,
            err_type=type(err).__name__,
        )


async def _replacement_refused_at_submit(
    runtime: SlackRuntime, client: AsyncWebClient, *, row: CredentialRequestRow, user_id: str
) -> bool:
    """Re-decide a replacement's role check at submit time. True when refused.

    A replacement overwrites a value other people are already using, so it is
    a `key_replace` attachment write rather than the posted-token contribution
    the other kinds are. The mint checked the role; this checks it again
    against the person who actually submitted, whose admin status may have
    changed while the form sat open.

    Facts are read only while the policy still needs them — the attachment
    family answers `allow` for an admin before anything else is consulted — and
    a target that can no longer be resolved fails closed.
    """
    is_admin = await resolve_is_admin(client, user_id=user_id)
    facts = TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=False)
    if not is_admin:
        agent = await find_agent_by_derived_uuid(
            runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
        if agent is None:
            log.warning(
                "credential_request.replacement_agent_gone",
                tenant_id=str(row.tenant_id),
                agent_id=str(row.agent_id),
            )
            return True
        facts = await gather_target_facts(
            runtime,
            operation="key_replace",
            tenant_id=row.tenant_id,
            agent_names=agent_pin_names(agent.name, agent.metadata),
            ma_agent_id=str(agent.id),
            is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            caller=await channel_admin_caller(
                runtime, client, tenant_id=row.tenant_id, user_id=user_id, is_admin=False
            ),
            caller_account_id=row.account_id,
        )
    outcome = decide_operation("key_replace", is_admin=is_admin, target=facts)
    return outcome != "allow"


async def _decide_mcp_connect_at_submit(
    runtime: SlackRuntime, client: AsyncWebClient, *, row: CredentialRequestRow, user_id: str
) -> McpConnectDecision:
    """Re-decide an MCP token submission against the person submitting it.

    Repointing a server the agent already has, or setting the agent-wide token
    for a URL it already uses, is an `mcp_replace` attachment write: on a
    shared agent it needs a live workspace admin. Decided before the consume.
    A target that cannot be resolved is left to the agent-gone path, and
    nothing may be replaced for it.
    """
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
    )
    if agent is None or row.mcp_server_url is None:
        return McpConnectDecision(replaces=False, replace_allowed=False)
    return await decide_mcp_connect(
        runtime.sessionmaker,
        tenant_id=row.tenant_id,
        agent=agent,
        agent_id=row.agent_id,
        server_name=row.target,
        url=row.mcp_server_url,
        platform="slack",
        caller=await channel_admin_caller(
            runtime,
            client,
            tenant_id=row.tenant_id,
            user_id=user_id,
            is_admin=await resolve_is_admin(client, user_id=user_id),
        ),
        default=runtime.deployment_default,
        shares_token=True,
    )


async def _validate_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    token: str,
    team_id: str,
    user_id: str,
    channel_id: str,
    kind: CredentialRequestKind,
) -> tuple[CredentialRequestRow, BetaManagedAgentsAgent] | None:
    """The live request and its target agent, or None once the submitter is told why not.

    The pin rule is decided here, before any confirmation, and again inside
    the consume transaction (`consume_form_unless_pinned`).
    """
    async with runtime.sessionmaker() as session:
        row = await credential_requests_store.peek_credential_request(session, token=token)
    if (
        row is None
        or row.tenant_id != derive_tenant_uuid(platform="slack", workspace_id=team_id)
        or row.platform not in (None, "slack")
        or row.requester_platform_user_id != user_id
        or row.kind != kind
    ):
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=NO_LONGER_VALID_MESSAGE
        )
        return None
    if row.used_at is not None or row.expires_at <= datetime.now(UTC):
        await post_ephemeral(
            client,
            channel_id=row.parent_channel_id or channel_id,
            thread_ts=row.origin_thread_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return None
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
    )
    if agent is None:
        await post_ephemeral(
            client,
            channel_id=row.parent_channel_id or channel_id,
            thread_ts=row.origin_thread_id,
            user_id=user_id,
            text=AGENT_GONE_MESSAGE,
        )
        return None
    async with runtime.sessionmaker() as session:
        pin_refusal = await request_pin_refusal(session, row=row, agent=agent)
    if pin_refusal is not None:
        await post_ephemeral(
            client,
            channel_id=row.parent_channel_id or channel_id,
            thread_ts=row.origin_thread_id,
            user_id=user_id,
            text=pin_refusal,
        )
        return None
    return row, agent


async def run_env_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
    dispatch_continuations: ContinuationTrigger,
) -> None:
    """Post-ack: atomic consume + agent_files write + continuation, as one.

    The three commit together, so the first point the row is durably spent is
    also the point the secret is durably stored and the turn that was waiting
    on it is durably queued.

    A replacement — one whose mint recorded the value it promised to overwrite
    — carries two extra obligations, and neither can be settled at mint time
    because the form was open in between: the role is re-decided against
    whoever actually submitted, and the write lands only while the stored
    value is still the one the card described. A precondition that no longer
    holds leaves the existing value exactly as it is.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    validated = await _validate_submission(
        runtime,
        client,
        token=token,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        kind="env",
    )
    if validated is None:
        return
    request, agent = validated
    channel_id = request.parent_channel_id or channel_id
    message_ts = request.posted_message_id or message_ts
    thread_ts = request.origin_thread_id

    async def replacement_refused() -> bool:
        return await _replacement_refused_at_submit(runtime, client, row=request, user_id=user_id)

    plan = await prepare_env_submit(
        runtime.sessionmaker, row=request, replacement_refused=replacement_refused
    )
    shadowed = plan.shadowed

    # The name a member may store is re-checked against the submitter's live
    # role. `key_add` needs no gate, but the NAME does: a tool-control or
    # redirect name is refused for a member even on a brand-new key.
    is_admin = await resolve_is_admin(client, user_id=user_id)
    name_problem = env_name_problem(request.target, is_admin=is_admin)
    if name_problem is not None:
        await post_ephemeral(
            client,
            thread_ts=request.origin_thread_id,
            channel_id=channel_id,
            user_id=user_id,
            text=_env_name_refusal(request.target, name_problem),
        )
        return

    try:
        result = await apply_env_submit(
            runtime.sessionmaker,
            row=request,
            agent=agent,
            platform="slack",
            value=value,
            plan=plan,
            now=datetime.now(UTC),
        )
        consumed, state, queued = result.consumed, result.state, result.queued
    except FormPinRefused as refused:
        # Decided with the consume: rolled back, nothing stored.
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
        )
        return
    except AgentEnvEncryptionRequiredError as err:
        # Rolled back with the consume: nothing stored, the request stays live.
        log.error("credential_request.env_write_refused_no_crypto_keys")
        await post_ephemeral(
            client, thread_ts=thread_ts, channel_id=channel_id, user_id=user_id, text=str(err)
        )
        return
    except Exception as exc:
        # Type only: the message of an error at a credential boundary can quote values.
        log.error(
            "credential_request.env_write_failed", key_present=True, error_type=type(exc).__name__
        )
        capture_exception_with_scope(exc)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text="Something went wrong — please try again.",
        )
        return

    if consumed is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return

    # Log the key NAME only — never the value.
    log.info("credential_request.env.submit", key=consumed.target, state=state)
    await _mark_button_consumed(client, row=consumed)

    if state == "refused":
        await edit_posted_card(
            client,
            row=consumed,
            state="refused",
            refusal="replacement_admin_required",
            replaces=shadowed,
        )
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refusal_text(
                consumed, state="refused", refusal="replacement_admin_required", replaces=shadowed
            ),
        )
        return
    if state == "superseded":
        await edit_posted_card(client, row=consumed, state="superseded")
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refusal_text(consumed, state="superseded"),
        )
        return

    # The card is the receipt; no ephemeral beside it, or the same save would
    # be announced twice in the same conversation.
    await edit_posted_card(
        client,
        row=consumed,
        state="applied",
        outcome=ConfigurationChange(
            target_name=consumed.target_name or "this agent",
            kind="key",
            detail=consumed.target,
            availability=availability_for_request(consumed),
        ),
    )
    if queued:
        await _dispatch_pending(dispatch_continuations, kind="env")


#: Uploaded files get their own client rather than the runtime's shared one:
#: the download is a CDN round trip unrelated to the API calls every turn
#: makes, and 30s is generous for a file the parser caps at 64 KB.
_FILE_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 30.0


def _download_client() -> httpx.AsyncClient:
    """The HTTP client used to pull one uploaded file from Slack."""
    return httpx.AsyncClient(timeout=_FILE_DOWNLOAD_TIMEOUT_SECONDS)


def _collision_lines(collisions: tuple[EnvEntry, ...], held: frozenset[str]) -> tuple[str, ...]:
    """Name the keys that already exist: names and line numbers, no values.

    Shaped like `render_env_import_rejected`'s line list, and capped the same
    way — a 200-key file that collides everywhere must not render 200 lines.
    """
    lines = [env_collision_line(entry, held) for entry in collisions[:_COLLISION_LINES_SHOWN]]
    remaining = len(collisions) - _COLLISION_LINES_SHOWN
    if remaining > 0:
        lines.append(f"…and {remaining} more.")
    return tuple(lines)


async def _apply_env_file_entries(
    runtime: SlackRuntime,
    *,
    request: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    entries: tuple[EnvEntry, ...],
    now: datetime,
) -> tuple[CredentialRequestRow | None, tuple[EnvEntry, ...], bool, frozenset[str]]:
    """Consume the request and write every entry, in one transaction.

    Returns `(consumed row, colliding entries, continuation queued, held key
    names)`; the held names let the refusal say which stored key an alias
    would have replaced. A `None`
    row means the request was already spent and nothing was written. A
    non-empty collision tuple means the request is now spent and recorded as
    `stale_replacement` and STILL nothing was written: the card promised these
    keys were new, so a whole-file import must not quietly replace a key
    someone is using. The continuation the request owes is queued in the same
    transaction as the keys, and only on the path that actually wrote them.
    """
    return await apply_env_file_submit(
        runtime.sessionmaker, row=request, agent=agent, platform="slack", entries=entries, now=now
    )


async def run_env_file_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    file_id: str,
    dispatch_continuations: ContinuationTrigger,
) -> None:
    """Post-ack: fetch the uploaded `.env`, then consume + write as one.

    The order is the design. The file is downloaded and parsed BEFORE the
    consume, so a file that cannot be read costs the person nothing — the
    request stays live and the card stays in `requested`, ready for the
    corrected upload. Only a file that parsed whole reaches the consume, and
    that runs in the same transaction as the writes: the first moment the
    request is durably spent is the moment every key in it is durably stored.

    The import is whole-file in both directions. A key that already exists is
    a refusal rather than a silent replacement — the card said these keys
    were new — and no entry lands unless all of them can.

    The uploaded file is never deleted. It is the person's own file in their
    own workspace, and a bot deleting it is a worse surprise than one that
    leaves it; what this stores is the keys, not the file.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    validated = await _validate_submission(
        runtime,
        client,
        token=token,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        kind="env_file",
    )
    if validated is None:
        return
    request, agent = validated
    channel_id = request.parent_channel_id or channel_id
    message_ts = request.posted_message_id or message_ts
    thread_ts = request.origin_thread_id
    agent_name = request.target_name or "the agent"
    responder_name = request.responder_name or "Daimon"

    try:
        async with _download_client() as http_client:
            body, _content_type, _name = await fetch_slack_file(
                http_client, bot_token=client.token or "", file_id=file_id
            )
    except httpx.HTTPError as err:
        # Exception class only: an upstream error string can carry the signed
        # download URL, which is a bearer credential for that file.
        log.warning("credential_request.env_file_download_failed", err_type=type(err).__name__)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"I could not read that upload, so nothing was saved for {agent_name}. "
                "Attach the file again."
            ),
        )
        return

    try:
        # `decode_env_bytes` re-measures the real bytes against the same cap
        # the submission checked the client's reported size against.
        is_admin = await resolve_is_admin(client, user_id=user_id)
        entries = parse_env_file(decode_env_bytes(body), member_writable_only=not is_admin)
    except EnvFileRejected as err:
        log.info(
            "credential_request.env_file_rejected",
            rejection=err.rejection,
            lines=[problem.line for problem in err.problems],
        )
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=render_env_import_rejected(err.rejection, err.problems, target_name=agent_name),
        )
        return

    try:
        consumed, collisions, queued, held = await _apply_env_file_entries(
            runtime, request=request, agent=agent, entries=entries, now=datetime.now(UTC)
        )
    except FormPinRefused as refused:
        # Decided with the consume: rolled back, nothing stored.
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
        )
        return
    except AgentEnvEncryptionRequiredError as err:
        # Rolled back with the consume: nothing stored, the request stays live.
        log.error("credential_request.env_file_refused_no_crypto_keys")
        await post_ephemeral(
            client, thread_ts=thread_ts, channel_id=channel_id, user_id=user_id, text=str(err)
        )
        return
    except Exception as exc:
        # Handled here, not in the spawned task, so the person gets a reply.
        # Rolled back with the consume, so the request stays live. The Sentry
        # capture carries id tags only; frame locals are never sent.
        # Type only: the message of an error at a credential boundary can quote values.
        log.error(
            "credential_request.env_file_write_failed",
            key_count=len(entries),
            error_type=type(exc).__name__,
        )
        capture_exception_with_scope(exc)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text="Something went wrong — please try again.",
        )
        return
    if consumed is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return

    if collisions:
        lines = (
            *_collision_lines(collisions, held),
            f"Nothing was changed. Ask {responder_name} to replace a key you already have.",
        )
        log.info(
            "credential_request.env_file_collision",
            key_count=len(entries),
            collision_count=len(collisions),
        )
        await edit_posted_card(
            client,
            row=consumed,
            state="refused",
            refusal="env_file_invalid",
            refusal_lines=lines,
        )
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text="\n".join((f"No keys were saved for {agent_name}.", *lines)),
        )
        return

    # Key NAMES only — never a value, as the single-key env submission does.
    log.info(
        "credential_request.env_file.submit",
        key_count=len(entries),
        keys=[entry.name for entry in entries],
    )
    # The card is the receipt; there is no ephemeral beside it, because the
    # two would say the same thing twice in the same conversation.
    await edit_posted_card(
        client,
        row=consumed,
        state="applied",
        outcome=ConfigurationChange(
            target_name=consumed.target_name or agent_name,
            kind="keys_bulk",
            availability=availability_for_request(consumed),
            count=len(entries),
        ),
    )
    if queued:
        await _dispatch_pending(dispatch_continuations, kind="env_file")


async def _refuse_for_unavailable_target(
    runtime: SlackRuntime, client: AsyncWebClient, *, row: CredentialRequestRow, token: str
) -> None:
    """Close a spent request whose target turned out to be unusable.

    The consume has already happened by the time any of these branches run, so
    leaving the card on `received` would have it say "Saving…" about a save
    that stopped. Nothing reached a store on this path, so the terminal state
    is `refused` rather than `partial`, and no continuation is queued: work
    waiting on this value must not resume on a value that never landed.
    """
    async with runtime.sessionmaker() as session, session.begin():
        await credential_requests_store.set_credential_request_outcome(
            session, token=token, outcome="write_failed"
        )
    await edit_posted_card(client, row=row, state="refused", refusal="target_unavailable")


async def _refuse_for_rejected_token(
    runtime: SlackRuntime, client: AsyncWebClient, *, row: CredentialRequestRow, token: str
) -> None:
    """Close a spent request whose token the server refused before any write."""
    async with runtime.sessionmaker() as session, session.begin():
        await credential_requests_store.set_credential_request_outcome(
            session, token=token, outcome="token_rejected"
        )
    await edit_posted_card(client, row=row, state="refused", refusal="token_rejected")


async def _refuse_mcp_replacement(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    row: CredentialRequestRow,
    token: str,
    thread_ts: str | None,
    channel_id: str,
    user_id: str,
) -> None:
    """Spend the request and write nothing: no vault token, no published token."""
    await settle_credential_submit(
        runtime.sessionmaker,
        row=row,
        platform="slack",
        outcome="write_failed",
        carries_work=False,
        record=record_input_continuation,
    )
    await edit_posted_card(client, row=row, state="refused", refusal="replacement_admin_required")
    await post_ephemeral(
        client,
        thread_ts=thread_ts,
        channel_id=channel_id,
        user_id=user_id,
        text=(
            f"{row.target_name or 'The agent'} already has `{row.target}` (or a token for "
            "that URL) and is shared here, so replacing it needs a workspace admin. "
            "Nothing was saved."
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
    dispatch_continuations: ContinuationTrigger,
) -> None:
    """Post-ack: consume, then the vault write and the agent attach.

    The configuration check precedes the consume — an unconfigured daimon-mcp
    must not spend the request. Partial states after the consume are reported
    truthfully: token stored but not attached is not success, so it lands on
    the card as `partial` and its continuation is recorded with no requested
    work — the click is in the audit trail, and no turn is promised for a
    connection that is not usable yet.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    validated = await _validate_submission(
        runtime,
        client,
        token=token,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        kind="mcp",
    )
    if validated is None:
        return
    request, agent = validated
    channel_id = request.parent_channel_id or channel_id
    message_ts = request.posted_message_id or message_ts
    thread_ts = request.origin_thread_id

    mcp = runtime.settings.mcp
    if mcp.public_url is None or mcp.jwt_secret is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "This deployment is not finished being set up. Ask the operator to finish "
                "setup, then try again. Nothing was saved."
            ),
        )
        return

    connect, now = await prepare_mcp_submit(
        decide=lambda: _decide_mcp_connect_at_submit(runtime, client, row=request, user_id=user_id),
        clock=lambda: datetime.now(UTC),
    )
    try:
        consumed = await _consume(runtime, request=request, agent=agent, now=now)
    except FormPinRefused as refused:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
        )
        return
    if consumed is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return

    await _mark_button_consumed(client, row=consumed)

    if connect.refused:
        await _refuse_mcp_replacement(
            runtime,
            client,
            row=consumed,
            token=token,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
        )
        return

    mcp_server_url = consumed.mcp_server_url
    if mcp_server_url is None:
        log.error("credential_request.mcp_missing_server_url", agent_id=str(consumed.agent_id))
        await _refuse_for_unavailable_target(runtime, client, row=consumed, token=token)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text="This request is missing its server URL — please ask again.",
        )
        return

    log.info(
        "credential_request.mcp.submit",
        mcp_server_url=mcp_server_url,
        token_present=bool(value),
    )
    # Ask the server first, as on Discord: a rejected token must not be
    # stored, mirrored and attached only to fail every later turn (#79).
    if await is_token_rejected(runtime.mcp_token_probe, mcp_server_url=mcp_server_url, token=value):
        await _refuse_for_rejected_token(runtime, client, row=consumed, token=token)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=rejected_token_message(mcp_server_url),
        )
        return
    try:
        await write_mcp_submit(
            runtime.anthropic,
            session_factory=runtime.sessionmaker,
            row=consumed,
            fernet=runtime.turn_deps.fernet,
            value=value,
            replace_allowed=connect.replace_allowed,
            jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
            public_url=str(mcp.public_url),
            now=now,
            connect=connect_mcp_server_with_token,
        )
    except McpServerReplaceRefusedError:
        # A server or token for this URL appeared after the pre-consume check.
        await _refuse_mcp_replacement(
            runtime,
            client,
            row=consumed,
            token=token,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
        )
        return
    except (McpAgentGoneError, McpAttachFailedError) as err:
        # Exception class name only: a stringified SDK error can carry the
        # request envelope. Nothing was stored.
        log.warning(
            "credential_request.mcp_attach_failed",
            mcp_server_url=mcp_server_url,
            err_type=type(err.__cause__ or err).__name__,
        )
        await _refuse_for_unavailable_target(runtime, client, row=consumed, token=token)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"The request was used up, but `{mcp_server_url}` could not be attached to "
                "the agent. Nothing was saved. Ask for a new private form to retry."
            ),
        )
        return
    except McpTokenWriteFailedError as err:
        log.warning(
            "credential_request.mcp_write_failed",
            mcp_server_url=mcp_server_url,
            err_type=type(err.__cause__ or err).__name__,
        )
        await settle_credential_submit(
            runtime.sessionmaker,
            row=consumed,
            platform="slack",
            outcome="write_failed",
            carries_work=False,
            record=record_input_continuation,
        )
        await edit_posted_card(
            client,
            row=consumed,
            state="partial",
            outcome=ConfigurationChange(
                target_name=consumed.target_name or "the agent",
                kind="mcp",
                detail=consumed.target,
                availability="preparation_failed",
            ),
        )
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                f"`{mcp_server_url}` is attached, but storing its token did not finish. "
                "Request a new private token form to retry."
            ),
        )
        return

    queued = await settle_credential_submit(
        runtime.sessionmaker,
        row=consumed,
        platform="slack",
        outcome="applied",
        record=record_input_continuation,
    )
    # The card is the receipt — no ephemeral beside it.
    await edit_posted_card(
        client,
        row=consumed,
        state="applied",
        outcome=ConfigurationChange(
            target_name=consumed.target_name or "the agent",
            kind="mcp",
            detail=consumed.target,
            availability="next_message",
        ),
    )
    if queued:
        await _dispatch_pending(dispatch_continuations, kind="mcp")


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


@dataclasses.dataclass(frozen=True, slots=True)
class SkillAttachOutcome:
    """Result of attaching the just-imported skills to the requested agent.

    `attached` is the one bit `run_skill_repo_credential_submission` needs to
    pick the confirmation copy's availability: `next_message` when the attach
    actually landed, `saved` when the skills reached the library but not the
    agent. `note` then closes the card with the reason.
    """

    note: str
    attached: bool
    agent_name: str | None
    skill_count: int


async def _attach_skills_to_requested_agent(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    outcomes: list[ResourceOutcome],
) -> SkillAttachOutcome:
    """Attach the just-imported skills to the agent this request named.

    Importing puts skills in the tenant's shared library; it does not put
    them on an agent. The request row already names the agent, so doing
    only the import leaves the user staring at an agent with no skills and
    no way to tell that anything worked.

    Returns a result rather than raising: the import has already succeeded by
    the time this runs, so a failure here is partial and both halves must
    be reported truthfully.
    """
    skill_ids = library_skill_ids(outcomes)
    if not skill_ids:
        return SkillAttachOutcome(
            note="Nothing new to attach.", attached=False, agent_name=None, skill_count=0
        )
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        return SkillAttachOutcome(
            note="That agent no longer exists.",
            attached=False,
            agent_name=None,
            skill_count=len(skill_ids),
        )
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        # Admins included: an attach never stamps the reconciler's spec hash,
        # so the seeded agent would drift for good.
        return SkillAttachOutcome(
            note=f"`{agent.name}` is a built-in agent. Fork it and add them to the fork.",
            attached=False,
            agent_name=agent.name,
            skill_count=len(skill_ids),
        )
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
        return SkillAttachOutcome(
            note="Attaching them did not finish. Ask again to retry.",
            attached=False,
            agent_name=agent.name,
            skill_count=len(skill_ids),
        )
    return SkillAttachOutcome(
        note=f"Attached {len(skill_ids)} to `{agent.name}`.",
        attached=True,
        agent_name=agent.name,
        skill_count=len(skill_ids),
    )


async def _report_skill_repo_failure(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    row: CredentialRequestRow,
    repo: str,
    is_token_stored: bool,
    channel_id: str,
    thread_ts: str | None,
    user_id: str,
) -> None:
    """Report a skill import that failed after the request was already spent.

    Two different failures, told apart by whether the token reached the store.
    One leaves a stored credential, so the card says so and names the import as
    the part to retry; the other leaves nothing, and a card claiming a save
    that did not happen is worse than no card at all.
    """
    if not is_token_stored:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "Skill setup failed before token storage could be confirmed. "
                "Ask again with a new private form to retry."
            ),
        )
        return
    await settle_credential_submit(
        runtime.sessionmaker,
        row=row,
        platform="slack",
        outcome="write_failed",
        carries_work=False,
        record=record_input_continuation,
    )
    await edit_posted_card(
        client,
        row=row,
        state="partial",
        outcome=ConfigurationChange(
            target_name=row.target_name or "the agent",
            kind="skills_bulk",
            availability="preparation_failed",
            repo=repo,
            # The renderer's `preparation_failed` copy names no count, but the
            # change model requires one; an import that never ran carried at
            # least the one skill somebody asked for.
            count=1,
        ),
    )
    await post_ephemeral(
        client,
        thread_ts=thread_ts,
        channel_id=channel_id,
        user_id=user_id,
        text="Token stored, but skill setup did not finish. Ask again to retry.",
    )


async def run_skill_repo_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
    dispatch_continuations: ContinuationTrigger,
) -> None:
    """Post-ack: consume, verify the token against the SKILL repo, store it,
    re-run the import, and attach the imported skills to the agent.

    The credential lands in the skill-repo store, NOT in the agent's working
    repo binding: somebody who offers a token so an agent can read skills out
    of a repo has not asked for that repo to become the agent's checkout, and
    the card they clicked said as much. The import attaches skills to the
    agent, so `skill_repo_connect` is decided again before the consume, as the
    repo bind does, and the attach never lands on a defaults-managed agent.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    validated = await _validate_submission(
        runtime,
        client,
        token=token,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        kind="skill_repo",
    )
    if validated is None:
        return
    request, agent = validated
    channel_id = request.parent_channel_id or channel_id
    message_ts = request.posted_message_id or message_ts
    thread_ts = request.origin_thread_id

    if await refuse_unless_allowed(
        runtime,
        client,
        operation="skill_repo_connect",
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        caller_account_id=request.account_id,
    ):
        # Same shape as the repo bind: nothing is spent, and the card stops
        # offering a form this submitter could never finish.
        async with runtime.sessionmaker() as session, session.begin():
            await credential_requests_store.set_credential_request_outcome(
                session, token=token, outcome="write_failed"
            )
        await edit_posted_card(client, row=request, state="refused", refusal="admin_required")
        return

    now = datetime.now(UTC)
    try:
        consumed = await _consume(runtime, request=request, agent=agent, now=now)
    except FormPinRefused as refused:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
        )
        return
    if consumed is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return

    await _mark_button_consumed(client, row=consumed)

    url, branch, path = split_skill_repo_target(consumed.target)
    owner_repo = normalize_owner_repo(url)
    log.info(
        "credential_request.skill_repo.submit",
        repo_url=url,
        branch=branch,
        path=path,
        pat_present=bool(value),
    )

    is_token_stored = False
    try:
        # Verify BEFORE storing: a token that cannot read this repo is not a
        # credential for it, and storing it would shadow a working one on the
        # next `get_pat` (the overlay is last-write-wins).
        if not await pat_can_access_repo(runtime.http_client, owner_repo=owner_repo, pat=value):
            await post_ephemeral(
                client,
                thread_ts=thread_ts,
                channel_id=channel_id,
                user_id=user_id,
                text=(
                    f"That token cannot read `{owner_repo}`. Nothing was "
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
        is_token_stored = True
        async with runtime.sessionmaker.begin() as session:
            seeded_skills = await write_skill_repo_submit(
                session, row=consumed, ma_secret_ref=ma_secret_ref, proof=proof
            )
        outcomes = await run_skill_sync(
            runtime.anthropic,
            runtime.http_client,
            url=url,
            branch=branch,
            path=path,
            tenant_id=consumed.tenant_id,
            seeded_skills=seeded_skills,
            is_admin=await resolve_is_admin(client, user_id=user_id),
            token=value,
        )
    except DaimonError as err:
        # Keep upstream details in operator logs, never in the receipt.
        log.warning("credential_request.skill_repo_sync_failed", err_type=type(err).__name__)
        await _report_skill_repo_failure(
            runtime,
            client,
            row=consumed,
            repo=owner_repo,
            is_token_stored=is_token_stored,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )
        return
    except Exception as err:
        log.exception(
            "credential_request.skill_repo_sync_failed",
            repo_url=url,
            err_type=type(err).__name__,
        )
        await _report_skill_repo_failure(
            runtime,
            client,
            row=consumed,
            repo=owner_repo,
            is_token_stored=is_token_stored,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )
        return

    imported = [o for o in outcomes if in_library(o)]
    failure_detail = summarize_failed_imports(outcomes)
    if not imported:
        # Nothing reached the library (an empty repo, or every skill refused
        # or failed), which an applied card must not claim.
        await settle_credential_submit(
            runtime.sessionmaker,
            row=consumed,
            platform="slack",
            outcome="write_failed",
            carries_work=False,
            record=record_input_continuation,
        )
        await edit_posted_card(
            client,
            row=consumed,
            state="partial",
            outcome=ConfigurationChange(
                target_name=consumed.target_name or "the agent",
                kind="skills_bulk",
                availability="preparation_failed",
                repo=owner_repo,
                # `preparation_failed` names no count but the model requires one.
                count=1,
                detail=failure_detail,
            ),
        )
        return

    attach = await _attach_skills_to_requested_agent(
        runtime, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id, outcomes=imported
    )
    log.info(
        "credential_request.skill_repo.attach",
        imported=len(imported),
        attached=attach.attached,
        note=attach.note,
    )
    queued = await settle_credential_submit(
        runtime.sessionmaker,
        row=consumed,
        platform="slack",
        outcome="applied" if attach.attached else "write_failed",
        carries_work=attach.attached,
        record=record_input_continuation,
    )
    # The card is the receipt; the import and the attach are one outcome to
    # the person who pasted the token, so they read as one line of copy.
    detail_lines = [failure_detail] if attach.attached else [attach.note, failure_detail]
    await edit_posted_card(
        client,
        row=consumed,
        state="applied" if attach.attached else "partial",
        outcome=ConfigurationChange(
            target_name=consumed.target_name or attach.agent_name or "the agent",
            kind="skills_bulk",
            availability="next_message" if attach.attached else "saved",
            repo=owner_repo,
            count=len(imported),
            detail="\n".join(line for line in detail_lines if line) or None,
        ),
    )
    if attach.attached and queued:
        await _dispatch_pending(dispatch_continuations, kind="skill_repo")


async def run_repo_bind_credential_submission(
    runtime: SlackRuntime,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
    token: str,
    value: str,
    dispatch_continuations: ContinuationTrigger,
) -> None:
    """Post-ack: gate, atomic consume, credential resolution, binding write.

    The branch is read from the request row's packed `target`, not from the
    form: the card named a branch when it was posted, and the form that
    follows it collects the token only.

    The shared-agent admin gate runs again here — immediately before the
    consume — rather than being trusted from the click-time pre-filter: a
    member who was an admin when the button was clicked may have lost it
    between click and submit. This call is the authorization boundary, and a
    refusal is written onto the card rather than left as an ephemeral beside a
    card still offering the form.
    """
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    validated = await _validate_submission(
        runtime,
        client,
        token=token,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        kind="repo",
    )
    if validated is None:
        return
    request, agent = validated
    channel_id = request.parent_channel_id or channel_id
    message_ts = request.posted_message_id or message_ts
    thread_ts = request.origin_thread_id

    if await refuse_if_shared_and_not_admin_for_request(
        runtime,
        client,
        tenant_id=request.tenant_id,
        agent_id=request.agent_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id=user_id,
    ):
        # The gate already told the person why. The request is left unspent —
        # an admin can still answer it — but the card stops offering a form
        # this submitter's write would never be allowed to finish.
        async with runtime.sessionmaker() as session, session.begin():
            await credential_requests_store.set_credential_request_outcome(
                session, token=token, outcome="write_failed"
            )
        await edit_posted_card(client, row=request, state="refused", refusal="admin_required")
        return

    now = datetime.now(UTC)
    try:
        consumed = await _consume(runtime, request=request, agent=agent, now=now)
    except FormPinRefused as refused:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=refused.refusal,
        )
        return
    if consumed is None:
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=NO_LONGER_VALID_MESSAGE,
        )
        return

    await _mark_button_consumed(client, row=consumed)

    repo_url, branch, _path = split_skill_repo_target(consumed.target)
    pat = value.strip()
    # Log the repo and branch, and the token ONLY as a masked tail when
    # present — never the plain value, never the (now-consumed) request token.
    log.info(
        "credential_request.repo.submit",
        repo_url=repo_url,
        branch=branch,
        pat_present=bool(pat),
    )

    try:
        ma_secret_ref, proof = await _resolve_repo_binding_credential(
            runtime,
            runtime.http_client,
            agent_id=consumed.agent_id,
            account_id=consumed.account_id,
            repo_url=repo_url,
            pasted_pat=pat or None,
            now=now,
        )
        async with runtime.sessionmaker.begin() as session:
            await write_repo_submit(
                session, row=consumed, ma_secret_ref=ma_secret_ref, proof=proof, write=set_binding
            )
    except DaimonError as err:
        log.warning("credential_request.repo_write_failed", err_type=type(err).__name__)
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "Repository access could not be saved. Ask again to retry with a new private form."
            ),
        )
        return
    except Exception as err:
        log.exception(
            "credential_request.repo_write_failed",
            repo_url=repo_url,
            err_type=type(err).__name__,
        )
        await post_ephemeral(
            client,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            text=(
                "The request was used up, but binding the working repo failed. "
                "Ask for a new private form to retry."
            ),
        )
        return

    queued = await settle_credential_submit(
        runtime.sessionmaker,
        row=consumed,
        platform="slack",
        outcome="applied",
        record=record_input_continuation,
    )
    # No `unsaved_work`: this bind copies nothing, and the copy line is a
    # promise only the panel's own flow is in a position to make.
    await edit_posted_card(
        client,
        row=consumed,
        state="applied",
        outcome=ConfigurationChange(
            target_name=consumed.target_name or "this agent",
            kind="repo",
            repo=normalize_owner_repo(repo_url),
            branch=branch,
            availability="next_message",
        ),
    )
    if queued:
        await _dispatch_pending(dispatch_continuations, kind="repo")
