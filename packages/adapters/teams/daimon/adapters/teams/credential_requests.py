"""The private dialogs behind a posted credential-request card.

Mirrors Slack's click and submit checks, in order: unknown token, wrong
organisation or conversation, wrong requester, expired, already used. The
submit re-runs them all, and the atomic consume precedes every write, so one
request yields one write however often its dialog is submitted. The secret
lives only in the submit payload: never logged, echoed, prefilled or put on a
card. Env keys and MCP tokens take a password field; MCP OAuth hands out a
private sign-in link.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.teams.card_actions import (
    FAILED,
    SENDING_PANEL_ERRORS,
    Actor,
    card_actor,
    dialog,
    dialog_message,
    error_text,
    submitted_fields,
)
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.output_delivery import Spawn
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.agent_pins import request_pin_refusal
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.continuity.continuation import record_input_continuation
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import availability_for_request
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.env_file import (
    MEMBER_SECRET_SUFFIX_HINT,
    env_alias_shadowed,
    env_name_problem,
    env_related_held,
)
from daimon.core.mcp_attach import (
    McpConnectDecision,
    McpServerReplaceRefusedError,
    decide_mcp_connect,
)
from daimon.core.mcp_oauth import INVITE_BUTTON_LABEL, begin_mcp_oauth_flow, invite_copy, start_url
from daimon.core.mcp_token_check import is_token_rejected
from daimon.core.mcp_token_connect import (
    McpAgentGoneError,
    McpAttachFailedError,
    McpTokenWriteFailedError,
    connect_mcp_server_with_token,
)
from daimon.core.operation_policy import TargetFacts, decide_operation, needs_reachability_read
from daimon.core.posted_controls import (
    ALREADY_USED_MESSAGE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
    CardState,
    RefusalReason,
    card_text,
)
from daimon.core.posted_controls.teams_card import (
    ADAPTIVE_CARD_TYPE,
    build_adaptive_card,
    card_for_request,
)
from daimon.core.stores import credential_requests as store
from daimon.core.stores.agent_files import (
    AgentEnvEncryptionRequiredError,
    agent_env_writes_allowed,
    lock_agent_keys,
    put_agent_file_if_unchanged,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.scoped_config_read import is_agent_shared_for_key_changes
from daimon.core.teams_threads import conversation_of
from daimon.core.turn_keys import list_turn_key_names
from microsoft_teams.api import (
    Attachment,
    InvokeActivity,
    MessageActivityInput,
    TaskFetchInvokeActivity,
    TaskModuleInvokeResponse,
    TaskModuleResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    AdaptiveCard,
    CardElement,
    OpenUrlAction,
    SubmitAction,
    SubmitData,
    TextBlock,
    TextInput,
)

log = structlog.get_logger()

SUBMIT = "credential_submit"
#: What a saved input calls to run the continuation it queued (`TeamsApp.dispatch_after_input`).
Dispatch = Callable[[uuid.UUID, str, str | None], Awaitable[None]]
Refusal = Literal["invalid", "wrong_org", "wrong_requester", "expired", "used"]

_FORM_KINDS = ("env", "mcp")
_WRONG_ORG = "This request isn't for this organisation — ask again where it was posted."
_AGENT_GONE = "That agent no longer exists — ask again and a fresh request will be posted."
_UNCONFIGURED = (
    "This deployment is not finished being set up. Ask the operator to finish setup, "
    "then try again. Nothing was saved."
)
_UNCONFIGURED_OAUTH = (
    "This deployment cannot sign you in yet. Ask the operator to finish the daimon-mcp "
    "setup, then ask again. Nothing was saved."
)
_MESSAGES: dict[Refusal, str] = {
    "invalid": NO_LONGER_VALID_MESSAGE,
    "wrong_org": _WRONG_ORG,
    "wrong_requester": WRONG_REQUESTER_MESSAGE,
    "used": ALREADY_USED_MESSAGE,
}


def refusal(
    row: CredentialRequestRow,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    conversation_id: str,
    now: datetime,
) -> Refusal | None:
    """Why this clicker may not use `row` here; None when they may."""
    if row.kind not in (*_FORM_KINDS, "mcp_oauth"):
        return "invalid"
    if row.tenant_id != tenant_id or row.platform != "teams":
        return "wrong_org"
    # A channel thread's id carries `;messageid=…`; the card lives in that conversation.
    if conversation_of(row.channel_id).split(";")[0] != conversation_id.split(";")[0]:
        return "invalid"
    if row.requester_platform_user_id != user_id:
        return "wrong_requester"
    if row.expires_at <= now:
        return "expired"
    return "used" if row.used_at is not None else None


def env_name_refusal(name: str, problem: str) -> str:
    """One-line refusal for a key name the submitter may not store (Slack's copy)."""
    if problem == "bad_name":
        return f"{name} is not a valid key name (letters, digits, underscores; not leading digit)."
    if problem == "reserved_name":
        return f"{name} is reserved: it changes how the agent's tools run, so it cannot be a key."
    return (
        f"{name} is not a secret name a member can add. Use a name ending in "
        f"{MEMBER_SECRET_SUFFIX_HINT}. An admin can add identity, account, region "
        "and URL names."
    )


def credential_form(row: CredentialRequestRow, error: str | None = None) -> TaskModuleResponse:
    """The password form for an env key or MCP token. Never prefilled."""
    env = row.kind == "env"
    body: list[CardElement] = [error_text(error)] if error else []
    body.append(
        TextInput(
            id="secret",
            label="Value" if env else "Token",
            style="Password",
            is_required=True,
            max_length=MAX_SECRET_VALUE_BYTES,
        )
    )
    facts = card_for_request(row, state="requested").facts
    body += [TextBlock(text=fact, is_subtle=True, size="Small", wrap=True) for fact in facts]
    save = SubmitAction(title="Save", data=SubmitData(SUBMIT, {"token": row.token}))
    agent = row.target_name or "the agent"
    title = f"{row.target} for {agent}" if env else f"{row.target} token"
    return dialog(title, AdaptiveCard(body=body, actions=[save], fallback_text=title))


def oauthdialog(row: CredentialRequestRow, url: str) -> TaskModuleResponse:
    """The requester's own sign-in link, shown only in their dialog."""
    text = invite_copy(server_name=row.target, agent_name=row.target_name or "the agent")
    link = OpenUrlAction(title=INVITE_BUTTON_LABEL, url=url)
    card = AdaptiveCard(body=[TextBlock(text=text, wrap=True)], actions=[link], fallback_text=text)
    return dialog(f"Connect {row.target}", card)


async def _guarded(work: Awaitable[TaskModuleResponse]) -> TaskModuleResponse:
    """Invoke boundary. The failing frames hold the submitted value, so only the
    error's class name is recorded: no traceback, no Sentry event."""
    try:
        return await work
    except SENDING_PANEL_ERRORS as err:
        log.error("teams.credential.failed", err_type=type(err).__name__)
        return dialog_message(FAILED)


class TeamsCredentialRequests:
    """Opens the private form behind a card, and saves what it submits."""

    def __init__(
        self, *, runtime: TeamsRuntime, sender: TeamsSender, spawn: Spawn, dispatch: Dispatch
    ) -> None:
        self._runtime = runtime
        self._sender = sender
        self._spawn = spawn
        self._dispatch = dispatch

    async def on_dialog_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._open(ctx.activity))

    async def on_dialog_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleInvokeResponse:
        return await _guarded(self._submit(ctx.activity))

    async def _checked(
        self, activity: InvokeActivity, fields: Mapping[str, object]
    ) -> tuple[Actor | None, CredentialRequestRow | None, Refusal | None]:
        """The verified clicker, the request row, and why it is refused."""
        actor = await card_actor(self._runtime, activity)
        token = fields.get("token")
        if actor is None or not isinstance(token, str) or not token:
            return actor, None, "invalid"
        async with self._runtime.sessionmaker() as session:
            row = await store.peek_credential_request(session, token=token)
        if row is None:
            return actor, None, "invalid"
        reason = refusal(
            row,
            tenant_id=actor.tenant_id,
            user_id=actor.user_id,
            conversation_id=actor.conversation_id,
            now=datetime.now(UTC),
        )
        return actor, row, reason

    async def _open(self, activity: TaskFetchInvokeActivity) -> TaskModuleResponse:
        actor, row, reason = await self._checked(activity, submitted_fields(activity.value.data))
        if actor is None:
            return dialog_message(DENIED)
        if row is not None and reason == "expired":
            # The only sweep there is; it runs past the requester check, so a
            # stranger's click cannot change the requester's card.
            self._spawn(self._edit(row, "expired", activity.service_url), name="teams.cred.edit")
            return dialog_message(card_text(card_for_request(row, state="expired")))
        if row is None or reason is not None:
            return dialog_message(_MESSAGES[reason or "invalid"])
        if row.kind == "mcp_oauth":
            return await self._start_oauth(row, activity.service_url)
        return credential_form(row)

    async def _start_oauth(
        self, row: CredentialRequestRow, service_url: str | None
    ) -> TaskModuleResponse:
        mcp = self._runtime.settings.mcp
        root = mcp.app_root_url
        if root is None or mcp.jwt_secret is None or self._runtime.turn_deps.fernet is None:
            return dialog_message(_UNCONFIGURED_OAUTH)
        agent = await find_agent_by_derived_uuid(
            self._runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
        async with self._runtime.sessionmaker() as session:
            pin_refusal = await request_pin_refusal(session, row=row, agent=agent)
        if pin_refusal is not None:
            return dialog_message(pin_refusal)
        now = datetime.now(UTC)
        async with self._runtime.sessionmaker.begin() as session:
            consumed = await store.consume_credential_request(session, token=row.token, now=now)
            flow = (
                await begin_mcp_oauth_flow(session, request=consumed, app_root_url=root, now=now)
                if consumed is not None
                else None
            )
        if consumed is None or flow is None:
            return dialog_message(NO_LONGER_VALID_MESSAGE)
        # The mcp process edits the card again once sign-in completes.
        self._spawn(self._edit(consumed, "received", service_url), name="teams.cred.edit")
        return oauthdialog(consumed, start_url(root, state=flow.state))

    async def _submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleResponse:
        fields = submitted_fields(activity.value.data)
        actor, row, reason = await self._checked(activity, fields)
        if actor is None:
            return dialog_message(DENIED)
        if row is None or reason is not None or row.kind not in _FORM_KINDS:
            return dialog_message(NO_LONGER_VALID_MESSAGE)
        secret = str(fields.get("secret") or "")
        if not secret.strip():
            return credential_form(row, "Value cannot be empty — try again.")
        if len(secret.encode()) > MAX_SECRET_VALUE_BYTES:
            return credential_form(row, f"Value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes.")
        mcp = self._runtime.settings.mcp
        if row.kind == "mcp" and (mcp.public_url is None or mcp.jwt_secret is None):
            return dialog_message(_UNCONFIGURED)
        if row.kind == "env":
            # The name is re-checked against the submitter's live role.
            problem = env_name_problem(row.target, is_admin=actor.is_admin)
            if problem is not None:
                return dialog_message(env_name_refusal(row.target, problem))
            async with self._runtime.sessionmaker() as session:
                if not agent_env_writes_allowed(session):
                    log.error("teams.credential.env_refused_no_crypto_keys")
                    return dialog_message(str(AgentEnvEncryptionRequiredError()))
        agent = await find_agent_by_derived_uuid(
            self._runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
        if agent is None:
            return dialog_message(_AGENT_GONE)
        async with self._runtime.sessionmaker() as session:
            pin_refusal = await request_pin_refusal(session, row=row, agent=agent)
        if pin_refusal is not None:
            return dialog_message(pin_refusal)
        url = activity.service_url
        work = (
            self._save_env(row, agent, secret, is_admin=actor.is_admin, service_url=url)
            if row.kind == "env"
            else self._save_mcp(row, agent, secret, is_admin=actor.is_admin, service_url=url)
        )
        self._spawn(self._background(work, kind=row.kind), name="teams.cred.save")
        return TaskModuleResponse()

    async def _background(self, work: Awaitable[None], *, kind: str) -> None:
        """Runner boundary. A failed write can carry the value in its SQL
        parameters, so only the error's class name is recorded."""
        try:
            await work
        except Exception as err:
            log.error("teams.credential.save_failed", kind=kind, err_type=type(err).__name__)

    async def _edit(
        self,
        row: CredentialRequestRow,
        state: CardState,
        service_url: str | None,
        *,
        outcome: ConfigurationChange | None = None,
        reason: RefusalReason | None = None,
        replaces: str | None = None,
    ) -> None:
        """Edit the posted card. Best effort: the outcome is already recorded."""
        if row.posted_message_id is None:
            return
        card = card_for_request(
            row, state=state, outcome=outcome, refusal=reason, replaces=replaces
        )
        attachment = Attachment(content_type=ADAPTIVE_CARD_TYPE, content=build_adaptive_card(card))
        edit = MessageActivityInput(id=row.posted_message_id).add_attachments(attachment)
        try:
            conversation_id = conversation_of(row.channel_id)
            await self._sender.send(conversation_id, edit, service_url=service_url)
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.credential.edit_failed", state=state, err_type=type(err).__name__)

    async def _resume(self, row: CredentialRequestRow, service_url: str | None) -> None:
        if row.origin_thread_id is not None:
            await self._dispatch(row.tenant_id, row.origin_thread_id, service_url)

    async def _replacement_refused(
        self, row: CredentialRequestRow, agent: BetaManagedAgentsAgent, *, is_admin: bool
    ) -> bool:
        """Re-decide a replacement's role check against whoever submitted (Slack's rule)."""
        managed = reachable = False
        if not is_admin:
            managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
            if needs_reachability_read("key_replace", is_admin=False, is_daimon_managed=managed):
                async with self._runtime.sessionmaker() as session:
                    reachable = await is_agent_shared_for_key_changes(
                        session,
                        tenant_id=row.tenant_id,
                        agent_names=(
                            agent.name,
                            str(agent.metadata.get(MA_METADATA_KEY_NAME) or ""),
                        ),
                        ma_agent_id=str(agent.id),
                        default=self._runtime.deployment_default,
                        caller_account_id=row.account_id,
                        caller_platform_user_id=row.requester_platform_user_id,
                    )
        facts = TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=reachable)
        return decide_operation("key_replace", is_admin=is_admin, target=facts) != "allow"

    async def _save_env(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        secret: str,
        *,
        is_admin: bool,
        service_url: str | None,
    ) -> None:
        """Consume, write and queue the continuation in one transaction, as Slack does."""
        # A new name a tool reads as a key already held (GH_TOKEN beside
        # GITHUB_TOKEN) retargets it like an overwrite, so it takes the same
        # gate, decided for this submitter.
        #
        # Snapshot the related credentials (aliases and family members) before the
        # transaction, for every submit. Under the lock the write proceeds only if
        # that set is unchanged: a related key added or removed after the gate was
        # decided was never put to it. An unchanged set — rotating
        # AWS_SECRET_ACCESS_KEY beside a stored AWS_ACCESS_KEY_ID — is fine.
        async with self._runtime.sessionmaker() as session:
            held = await list_turn_key_names(
                session, tenant_id=row.tenant_id, agent_id=row.agent_id
            )
        related_before = env_related_held(row.target, held)
        shadowed = env_alias_shadowed(row.target, held) if row.replaces_updated_at is None else None
        refuse = (row.replaces_updated_at is not None or shadowed is not None) and (
            await self._replacement_refused(row, agent, is_admin=is_admin)
        )
        state: CardState = "applied"
        queued = False
        async with self._runtime.sessionmaker.begin() as session:
            consumed = await store.consume_credential_request(
                session, token=row.token, now=datetime.now(UTC)
            )
            # Re-read under the write, holding the agent's key-set lock: an
            # alias that appeared after the gate above was decided was never
            # put to it, and one a concurrent writer is adding waits.
            appeared = False
            if consumed is not None:
                await lock_agent_keys(
                    session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id
                )
                appeared = (
                    env_related_held(
                        consumed.target,
                        await list_turn_key_names(
                            session, tenant_id=consumed.tenant_id, agent_id=consumed.agent_id
                        ),
                    )
                    != related_before
                )
            if consumed is not None and refuse:
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome="write_failed"
                )
                state = "refused"
            elif consumed is not None and appeared:
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome="stale_replacement"
                )
                state = "superseded"
            elif consumed is not None:
                written = await put_agent_file_if_unchanged(
                    session,
                    tenant_id=consumed.tenant_id,
                    agent_id=consumed.agent_id,
                    key=consumed.target,
                    content=secret,
                    set_by_account_id=consumed.account_id,
                    expected_updated_at=consumed.replaces_updated_at,
                )
                outcome = "applied" if written is not None else "stale_replacement"
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome=outcome
                )
                if written is None:
                    state = "superseded"
                else:
                    queued = await record_input_continuation(session, consumed, platform="teams")
        if consumed is None:
            log.info("teams.credential.already_used", kind="env")
            return
        log.info("teams.credential.env", key=consumed.target, state=state)
        # First, as on Discord and Slack: if the outcome edit fails, the card
        # should not still offer a button that can only be refused.
        await self._edit(consumed, "received", service_url)
        change = ConfigurationChange(
            target_name=consumed.target_name or "this agent",
            kind="key",
            detail=consumed.target,
            availability=availability_for_request(consumed),
        )
        await self._edit(
            consumed,
            state,
            service_url,
            outcome=change if state == "applied" else None,
            reason="replacement_admin_required" if state == "refused" else None,
            replaces=shadowed if state == "refused" else None,
        )
        if queued:
            await self._resume(consumed, service_url)

    async def _refuse(
        self,
        row: CredentialRequestRow,
        reason: RefusalReason,
        service_url: str | None,
    ) -> None:
        """Close a spent request that wrote nothing; no work resumes on it."""
        outcome = "token_rejected" if reason == "token_rejected" else "write_failed"
        async with self._runtime.sessionmaker.begin() as session:
            await store.set_credential_request_outcome(session, token=row.token, outcome=outcome)
        await self._edit(row, "refused", service_url, reason=reason)

    async def _save_mcp(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        secret: str,
        *,
        is_admin: bool,
        service_url: str | None,
    ) -> None:
        """Decide, consume, probe, then attach-and-publish (see mcp_token_connect)."""
        mcp = self._runtime.settings.mcp
        if mcp.public_url is None or mcp.jwt_secret is None:
            return
        # Repointing a server or setting the agent-wide token for a URL the
        # agent already uses is `mcp_replace`: decided against the live
        # submitter before the consume, as on Discord and Slack.
        connect = (
            await decide_mcp_connect(
                self._runtime.sessionmaker,
                tenant_id=row.tenant_id,
                agent=agent,
                agent_id=row.agent_id,
                server_name=row.target,
                url=row.mcp_server_url,
                is_admin=is_admin,
                default=self._runtime.deployment_default,
                shares_token=True,
            )
            if row.mcp_server_url is not None
            else McpConnectDecision(replaces=False, replace_allowed=False)
        )
        now = datetime.now(UTC)
        async with self._runtime.sessionmaker.begin() as session:
            consumed = await store.consume_credential_request(session, token=row.token, now=now)
        if consumed is None:
            log.info("teams.credential.already_used", kind="mcp")
            return
        await self._edit(consumed, "received", service_url)
        url = consumed.mcp_server_url
        if url is None:
            log.error("teams.credential.mcp_missing_server_url")
            return await self._refuse(consumed, "target_unavailable", service_url)
        if connect.refused:
            return await self._refuse(consumed, "replacement_admin_required", service_url)
        log.info("teams.credential.mcp", mcp_server_url=url)
        probe = self._runtime.mcp_token_probe
        if await is_token_rejected(probe, mcp_server_url=url, token=secret):
            return await self._refuse(consumed, "token_rejected", service_url)
        # Attach first, publish the agent-wide token only after that authorized
        # attach, then the submitter's own vault copy: no other session may ever
        # mirror a token this submission is refused for.
        attached = True
        try:
            await connect_mcp_server_with_token(
                self._runtime.anthropic,
                sessionmaker=self._runtime.sessionmaker,
                fernet=self._runtime.turn_deps.fernet,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                account_id=consumed.account_id,
                server_name=consumed.target,
                mcp_server_url=url,
                token=secret,
                replace_allowed=connect.replace_allowed,
                jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
                public_url=str(mcp.public_url),
                now=now,
            )
        except McpServerReplaceRefusedError:
            return await self._refuse(consumed, "replacement_admin_required", service_url)
        except (McpAgentGoneError, McpAttachFailedError) as err:
            log.warning(
                "teams.credential.mcp_attach_failed", err_type=type(err.__cause__ or err).__name__
            )
            return await self._refuse(consumed, "target_unavailable", service_url)
        except McpTokenWriteFailedError as err:
            log.warning(
                "teams.credential.mcp_write_failed", err_type=type(err.__cause__ or err).__name__
            )
            attached = False
        async with self._runtime.sessionmaker.begin() as session:
            outcome = "applied" if attached else "write_failed"
            await store.set_credential_request_outcome(session, token=row.token, outcome=outcome)
            # Attached but the token not fully stored: audited, no turn promised.
            queued = await record_input_continuation(
                session, consumed, platform="teams", carries_work=attached
            )
        change = ConfigurationChange(
            target_name=consumed.target_name or agent.name,
            kind="mcp",
            detail=consumed.target,
            availability="next_message" if attached else "preparation_failed",
        )
        await self._edit(
            consumed, "applied" if attached else "partial", service_url, outcome=change
        )
        if queued and attached:
            await self._resume(consumed, service_url)
