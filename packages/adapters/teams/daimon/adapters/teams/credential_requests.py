"""The private dialogs behind a posted credential-request card.

Mirrors Slack's click and submit checks, in order: unknown token, wrong
organisation or conversation, wrong requester, expired, already used. The
submit re-runs them all, and the atomic consume precedes every write, so one
request yields one write however often its dialog is submitted. The secret
lives only in the submit payload: never logged, echoed, prefilled or put on a
card. Env values and `.env` contents take a multi-line field (a dialog has no
file input, so the file is pasted), MCP and GitHub tokens a password field;
MCP OAuth hands out a private sign-in link. A GitHub token is checked against
its repo before the consume: no private reply can reach the requester once
the dialog has closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from daimon.adapters.teams.channel_admin_groups import channel_admin_caller
from daimon.adapters.teams.credential_repos import attach_imported_skills, store_agent_pat
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.output_delivery import Spawn
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.agent_pins import (
    FormPinRefused,
    agent_pin_names,
    consume_form_unless_pinned,
    request_pin_refusal,
)
from daimon.core.agent_reach import load_target_facts
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.continuity.continuation import record_input_continuation
from daimon.core.continuity.messages import ConfigurationChange, render_env_import_rejected
from daimon.core.credential_requests import (
    CredentialRequestOutcome,
    availability_for_request,
    split_skill_repo_target,
)
from daimon.core.credential_submit import (
    apply_env_file_submit,
    apply_env_submit,
    begin_oauth_submit,
    consume_credential_submit,
    credential_card_edit,
    env_name_refusal,
    guard_credential_save,
    note_credential_save_failure,
    prepare_env_submit,
    settle_credential_submit,
    write_repo_submit,
    write_skill_repo_submit,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.defaults.report import Action
from daimon.core.env_file import (
    MAX_ENV_FILE_BYTES,
    EnvEntry,
    EnvFileRejected,
    decode_env_bytes,
    env_collision_line,
    env_name_problem,
    parse_env_file,
)
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import pat_can_access_repo
from daimon.core.mcp_attach import (
    McpConnectDecision,
    McpServerReplaceRefusedError,
    decide_mcp_connect,
)
from daimon.core.mcp_oauth import INVITE_BUTTON_LABEL, invite_copy, start_url
from daimon.core.mcp_token_check import is_token_rejected
from daimon.core.mcp_token_connect import (
    McpAgentGoneError,
    McpAttachFailedError,
    McpTokenWriteFailedError,
    connect_mcp_server_with_token,
)
from daimon.core.operation_policy import (
    OperationKind,
    TargetFacts,
    decide_operation,
    needs_reachability_read,
)
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
    teams_wording,
)
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.skills.sync import summarize_failed_imports
from daimon.core.stores import credential_requests as store
from daimon.core.stores.agent_files import (
    AgentEnvEncryptionRequiredError,
    agent_env_writes_allowed,
)
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import CredentialRequestRow, RepoAccessProof
from daimon.core.stores.scoped_config_read import is_agent_shared_for_key_changes
from daimon.core.teams_threads import conversation_of
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

_FORM_KINDS = ("env", "env_file", "mcp", "repo", "skill_repo")
_REPO_KINDS = ("repo", "skill_repo")
#: Slack's caps: a GitHub token is short; whole-file errors name at most three keys.
_MAX_GITHUB_TOKEN_CHARS = 255
_COLLISION_LINES_SHOWN = 3
_SHARED_AGENT = (
    "This agent answers for other people here, so changing its repo or its keys needs an "
    "admin. Ask me and I'll write the request for them, or ask me to make you a new agent "
    "of your own."
)
_SHARED_AGENT_SKILLS = (
    "This agent answers for other people here, so adding skills to it needs an admin. Ask "
    "me and I'll write the request for them, or ask me to fork it and add them to the fork."
)
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


def _secret_input(kind: str) -> TextInput:
    """The one input a kind collects, sized as on Slack."""
    if kind == "env":
        return TextInput(
            id="secret",
            label="Value",
            placeholder="paste the value",
            is_multiline=True,
            is_required=True,
            max_length=MAX_SECRET_VALUE_BYTES,
        )
    if kind == "env_file":
        return TextInput(
            id="secret",
            label=".env file contents",
            placeholder="KEY=VALUE",
            is_multiline=True,
            is_required=True,
            max_length=MAX_ENV_FILE_BYTES,
        )
    github = kind in _REPO_KINDS
    return TextInput(
        id="secret",
        label="Token",
        placeholder="github_pat_…" if github else None,
        style="Password",
        is_required=True,
        max_length=_MAX_GITHUB_TOKEN_CHARS if github else MAX_SECRET_VALUE_BYTES,
    )


def _title(row: CredentialRequestRow) -> str:
    if row.kind == "env":
        return f"{row.target} for {row.target_name or 'the agent'}"
    if row.kind == "mcp":
        return f"{row.target} token"
    return "Keys from a file" if row.kind == "env_file" else "Your GitHub token"


def credential_form(row: CredentialRequestRow, error: str | None = None) -> TaskModuleResponse:
    """The private form for one request: the card's facts and one input. Never prefilled."""
    # One block per line: a TextBlock does not reliably break on a single newline.
    body: list[CardElement] = [error_text(line) for line in (error or "").splitlines()]
    body.append(_secret_input(row.kind))
    facts = teams_wording(card_for_request(row, state="requested")).facts
    if row.kind == "skill_repo":
        facts = ("Skill repo only — the working repo does not change.", *facts)
    if row.kind in _REPO_KINDS:
        facts = (*facts, "A fine-grained token with read access to the repo.")
    body += [TextBlock(text=fact, is_subtle=True, size="Small", wrap=True) for fact in facts]
    save = SubmitAction(title="Save", data=SubmitData(SUBMIT, {"token": row.token}))
    title = _title(row)
    return dialog(title, AdaptiveCard(body=body, actions=[save], fallback_text=title))


def _collision_lines(collisions: Sequence[EnvEntry], held: frozenset[str]) -> tuple[str, ...]:
    """The keys a whole-file import would replace: names and line numbers, no values."""
    lines = [env_collision_line(entry, held) for entry in collisions[:_COLLISION_LINES_SHOWN]]
    if len(collisions) > _COLLISION_LINES_SHOWN:
        lines.append(f"…and {len(collisions) - _COLLISION_LINES_SHOWN} more.")
    return tuple(lines)


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
        # Someone from another organisation may only sign in with their own account.
        actor = await card_actor(self._runtime, activity, allow_external=True)
        token = fields.get("token")
        if actor is None or not isinstance(token, str) or not token:
            return actor, None, "invalid"
        async with self._runtime.sessionmaker() as session:
            row = await store.peek_credential_request(session, token=token)
        if row is None or (actor.is_external and row.kind != "mcp_oauth"):
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
        if row.kind == "repo":
            # Gated at the click too, as on Slack: a member is not asked for a
            # token a shared agent would refuse.
            agent = await find_agent_by_derived_uuid(
                self._runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
            )
            if agent is None:
                return dialog_message(_AGENT_GONE)
            if await self._attachment_refused(row, agent, "repo_bind", is_admin=actor.is_admin):
                return dialog_message(_SHARED_AGENT)
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
        try:
            consumed, flow = await begin_oauth_submit(
                self._runtime.sessionmaker, row=row, agent=agent, app_root_url=root, now=now
            )
        except FormPinRefused as refused:
            return dialog_message(refused.refusal)
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
        if row.kind == "env_file" and len(secret.encode()) > MAX_ENV_FILE_BYTES:
            return credential_form(row, f"That is too big. Max {MAX_ENV_FILE_BYTES // 1024} KB.")
        if row.kind != "env_file" and len(secret.encode()) > MAX_SECRET_VALUE_BYTES:
            return credential_form(row, f"Value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes.")
        mcp = self._runtime.settings.mcp
        if row.kind == "mcp" and (mcp.public_url is None or mcp.jwt_secret is None):
            return dialog_message(_UNCONFIGURED)
        if row.kind in _REPO_KINDS and self._runtime.turn_deps.fernet is None:
            return dialog_message(_UNCONFIGURED)
        if row.kind == "env":
            # The name is re-checked against the submitter's live role.
            problem = env_name_problem(row.target, is_admin=actor.is_admin)
            if problem is not None:
                return dialog_message(env_name_refusal(row.target, problem))
        entries: tuple[EnvEntry, ...] = ()
        if row.kind in ("env", "env_file"):
            async with self._runtime.sessionmaker() as session:
                if not agent_env_writes_allowed(session):
                    log.error("teams.credential.env_refused_no_crypto_keys")
                    return dialog_message(str(AgentEnvEncryptionRequiredError()))
        if row.kind == "env_file":
            # Parsed before the consume, as Slack parses the upload: a file that
            # cannot be read costs nothing, and the form stays open to fix it.
            try:
                text = decode_env_bytes(secret.encode())
                entries = parse_env_file(text, member_writable_only=not actor.is_admin)
            except EnvFileRejected as err:
                log.info(
                    "teams.credential.env_file_rejected",
                    rejection=err.rejection,
                    lines=[problem.line for problem in err.problems],
                )
                agent_name = row.target_name or "the agent"
                rejected = render_env_import_rejected(
                    err.rejection, err.problems, target_name=agent_name, pasted=True
                )
                return credential_form(row, rejected)
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
        if row.kind in _REPO_KINDS:
            refused = await self._repo_refusal(row, agent, secret.strip(), actor, url)
            if refused is not None:
                return refused
        is_admin = actor.is_admin
        work: Awaitable[None]
        if row.kind == "env":
            work = self._save_env(row, agent, secret, is_admin=is_admin, service_url=url)
        elif row.kind == "env_file":
            work = self._save_env_file(row, agent, entries, service_url=url)
        elif row.kind == "mcp":
            work = self._save_mcp(row, agent, secret, is_admin=is_admin, service_url=url)
        elif row.kind == "repo":
            work = self._save_repo(row, agent, secret.strip(), service_url=url)
        else:
            work = self._save_skill_repo(
                row, agent, secret.strip(), is_admin=is_admin, service_url=url
            )
        self._spawn(self._background(work, kind=row.kind), name="teams.cred.save")
        return TaskModuleResponse()

    async def _repo_refusal(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        pat: str,
        actor: Actor,
        service_url: str | None,
    ) -> TaskModuleResponse | None:
        """The admin gate, then the token's access to the repo; None when both pass.

        The gate is decided again here against the live submitter, before the
        consume, as on Slack. A refused card stops offering the form.
        """
        skills = row.kind == "skill_repo"
        operation: OperationKind = "skill_repo_connect" if skills else "repo_bind"
        if await self._attachment_refused(row, agent, operation, is_admin=actor.is_admin):
            async with self._runtime.sessionmaker.begin() as session:
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome="write_failed"
                )
            edit = self._edit(row, "refused", service_url, reason="admin_required")
            self._spawn(edit, name="teams.cred.edit")
            return dialog_message(_SHARED_AGENT_SKILLS if skills else _SHARED_AGENT)
        # Verified before it is stored: a token that cannot read the repo is not
        # a credential for it, and a stored one would shadow a working token.
        owner_repo = normalize_owner_repo(split_skill_repo_target(row.target)[0])
        http = self._runtime.http_client
        if not await pat_can_access_repo(http, owner_repo=owner_repo, pat=pat):
            async with self._runtime.sessionmaker.begin() as session:
                await store.set_credential_request_outcome(
                    session, token=row.token, outcome="token_rejected"
                )
            await self._edit(
                row,
                "requested",
                service_url,
                retry_reason=(
                    "That token was rejected: it cannot read the requested repo. Try again."
                ),
            )
            return credential_form(
                row,
                f"That token cannot read {owner_repo} (or the repo does not exist). "
                "Paste one that can. Nothing was saved.",
            )
        return None

    async def _attachment_refused(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        operation: OperationKind,
        *,
        is_admin: bool,
    ) -> bool:
        """Decide a repo bind or skill import for whoever submitted (Slack's rule)."""
        managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        async with self._runtime.sessionmaker() as session:
            facts = await load_target_facts(
                session,
                operation,
                tenant_id=row.tenant_id,
                platform="teams",
                agent_names=agent_pin_names(agent.name, agent.metadata),
                ma_agent_id=str(agent.id),
                default=self._runtime.deployment_default,
                # Only the requester may submit, so they are the caller.
                caller=await channel_admin_caller(
                    self._runtime,
                    tenant_id=row.tenant_id,
                    user_id=row.requester_platform_user_id,
                    is_admin=is_admin,
                ),
                is_daimon_managed=managed,
                # Slack leaves the caller's own sessions out of a skill import's
                # sharing read, not a repo bind's.
                caller_account_id=row.account_id if operation == "skill_repo_connect" else None,
                caller_platform_user_id=row.requester_platform_user_id,
            )
        return decide_operation(operation, is_admin=is_admin, target=facts) != "allow"

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
        refusal_lines: Sequence[str] = (),
        retry_reason: str | None = None,
        saving_notice: str | None = None,
    ) -> None:
        """Edit the posted card. Best effort: the outcome is already recorded."""
        async with credential_card_edit(row, state):
            if state == "partial":
                note_credential_save_failure(row, outcome)
            if row.posted_message_id is None:
                return
            card = card_for_request(
                row,
                state=state,
                outcome=outcome,
                refusal=reason,
                replaces=replaces,
                refusal_lines=refusal_lines,
                retry_reason=retry_reason,
                saving_notice=saving_notice,
            )
            attachment = Attachment(
                content_type=ADAPTIVE_CARD_TYPE,
                content=build_adaptive_card(
                    card, token=row.token if state == "requested" else None
                ),
            )
            edit = MessageActivityInput(id=row.posted_message_id).add_attachments(attachment)
            try:
                conversation_id = conversation_of(row.channel_id)
                await self._sender.send(conversation_id, edit, service_url=service_url)
            except TEAMS_SEND_ERRORS as err:
                log.warning(
                    "teams.credential.edit_failed", state=state, err_type=type(err).__name__
                )

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

        async def replacement_refused() -> bool:
            return await self._replacement_refused(row, agent, is_admin=is_admin)

        plan = await prepare_env_submit(
            self._runtime.sessionmaker, row=row, replacement_refused=replacement_refused
        )
        shadowed = plan.shadowed
        try:
            result = await apply_env_submit(
                self._runtime.sessionmaker,
                row=row,
                agent=agent,
                platform="teams",
                value=secret,
                plan=plan,
                now=datetime.now(UTC),
            )
            consumed, state, queued = result.consumed, result.state, result.queued
        except FormPinRefused:
            # Decided with the consume: rolled back, the form stays live. The
            # dialog showed the earlier refusal for a pin already set.
            log.info("teams.credential.pin_refused", kind="env")
            return
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

    async def _apply_env_file(
        self, row: CredentialRequestRow, agent: BetaManagedAgentsAgent, entries: Sequence[EnvEntry]
    ) -> tuple[CredentialRequestRow | None, tuple[EnvEntry, ...], bool, frozenset[str]]:
        """Consume and write every entry in one transaction, as Slack does.

        Returns the consumed row (None when already spent), the entries that
        would replace a held key (then nothing is written and the request is
        `stale_replacement`), whether the continuation was queued, and the held
        names the refusal lines need.
        """
        return await apply_env_file_submit(
            self._runtime.sessionmaker,
            row=row,
            agent=agent,
            platform="teams",
            entries=entries,
            now=datetime.now(UTC),
        )

    async def _save_env_file(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        entries: Sequence[EnvEntry],
        *,
        service_url: str | None,
    ) -> None:
        """Whole-file, as on Slack: a key already held, by name or alias, writes nothing."""
        try:
            consumed, collisions, queued, held = await self._apply_env_file(row, agent, entries)
        except FormPinRefused:
            log.info("teams.credential.pin_refused", kind="env_file")
            return
        if consumed is None:
            log.info("teams.credential.already_used", kind="env_file")
            return
        await self._edit(consumed, "received", service_url)
        if collisions:
            responder = consumed.responder_name or "Daimon"
            lines = (
                *_collision_lines(collisions, held),
                f"Nothing was changed. Ask {responder} to replace a key you already have.",
            )
            log.info(
                "teams.credential.env_file_refused",
                key_count=len(entries),
                collision_count=len(collisions),
            )
            await self._edit(
                consumed, "refused", service_url, reason="env_file_invalid", refusal_lines=lines
            )
            return
        # Key names only, never a value.
        log.info("teams.credential.env_file_saved", keys=[entry.name for entry in entries])
        change = ConfigurationChange(
            target_name=consumed.target_name or "this agent",
            kind="keys_bulk",
            availability=availability_for_request(consumed),
            count=len(entries),
        )
        await self._edit(consumed, "applied", service_url, outcome=change)
        if queued:
            await self._resume(consumed, service_url)

    async def _consume(
        self, row: CredentialRequestRow, agent: BetaManagedAgentsAgent
    ) -> CredentialRequestRow | None:
        """Spend the form, the pin decided in the same transaction. None when it is not spent."""
        try:
            consumed = await consume_credential_submit(
                self._runtime.sessionmaker, row=row, agent=agent, now=datetime.now(UTC)
            )
        except FormPinRefused:
            log.info("teams.credential.pin_refused", kind=row.kind)
            return None
        if consumed is None:
            log.info("teams.credential.already_used", kind=row.kind)
        return consumed

    async def _save_repo(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        pat: str,
        *,
        service_url: str | None,
    ) -> None:
        """Consume, store the token as the agent's own, bind the working repo.

        The branch comes from the request, not the form.
        """
        consumed = await self._consume(row, agent)
        if consumed is None:
            return
        async with guard_credential_save(
            self._runtime.sessionmaker,
            row=consumed,
            edit_retry=lambda retry, reason: self._edit(
                retry, "requested", service_url, retry_reason=reason
            ),
            edit_pending=lambda pending, notice: self._edit(
                pending, "received", service_url, saving_notice=notice
            ),
        ):
            await self._edit(consumed, "received", service_url)
            repo_url, branch, _path = split_skill_repo_target(consumed.target)
            try:
                ref = await store_agent_pat(self._runtime, agent_id=consumed.agent_id, pat=pat)
                proof = RepoAccessProof(
                    kind="pat", at=datetime.now(UTC), account_id=consumed.account_id
                )
                async with self._runtime.sessionmaker.begin() as session:
                    await write_repo_submit(
                        session, row=consumed, ma_secret_ref=ref, proof=proof, write=set_binding
                    )
                    await store.set_credential_request_outcome(
                        session, token=row.token, outcome="applied"
                    )
                    queued = await record_input_continuation(session, consumed, platform="teams")
            except Exception as err:
                # Type only: a failed write can quote the token; the card says it did not land.
                log.warning("teams.credential.repo_write_failed", err_type=type(err).__name__)
                return await self._refuse(consumed, "target_unavailable", service_url)
            log.info("teams.credential.repo_bound", repo_url=repo_url, branch=branch)
            change = ConfigurationChange(
                target_name=consumed.target_name or "this agent",
                kind="repo",
                repo=normalize_owner_repo(repo_url),
                branch=branch,
                availability="next_message",
            )
            await self._edit(consumed, "applied", service_url, outcome=change)
            if queued:
                await self._resume(consumed, service_url)

    async def _save_skill_repo(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        pat: str,
        *,
        is_admin: bool,
        service_url: str | None,
    ) -> None:
        """Consume, store the token for the skill repo, import, attach (Slack's runner).

        The working repo binding is never touched.
        """
        consumed = await self._consume(row, agent)
        if consumed is None:
            return
        async with guard_credential_save(
            self._runtime.sessionmaker,
            row=consumed,
            edit_retry=lambda retry, reason: self._edit(
                retry, "requested", service_url, retry_reason=reason
            ),
            edit_pending=lambda pending, notice: self._edit(
                pending, "received", service_url, saving_notice=notice
            ),
        ):
            await self._edit(consumed, "received", service_url)
            url, branch, path = split_skill_repo_target(consumed.target)
            owner_repo = normalize_owner_repo(url)
            log.info("teams.credential.skill_repo_saved", repo_url=url, branch=branch, path=path)
            stored = False
            try:
                ref = await store_agent_pat(self._runtime, agent_id=consumed.agent_id, pat=pat)
                stored = True
                proof = RepoAccessProof(
                    kind="pat", at=datetime.now(UTC), account_id=consumed.account_id
                )
                async with self._runtime.sessionmaker.begin() as session:
                    seeded = await write_skill_repo_submit(
                        session, row=consumed, ma_secret_ref=ref, proof=proof
                    )
                outcomes = await run_skill_sync(
                    self._runtime.anthropic,
                    self._runtime.http_client,
                    url=url,
                    branch=branch,
                    path=path,
                    tenant_id=consumed.tenant_id,
                    seeded_skill_names=seeded,
                    is_admin=is_admin,
                    token=pat,
                )
            except Exception as err:
                # Upstream details stay in operator logs, never on the card.
                log.warning("teams.credential.skill_repo_sync_failed", err_type=type(err).__name__)
                if not stored:
                    return await self._refuse(consumed, "target_unavailable", service_url)
                return await self._skills_partial(consumed, owner_repo, None, service_url)
            imported = [o for o in outcomes if o.action in (Action.CREATED, Action.UPDATED)]
            failure_detail = summarize_failed_imports(outcomes)
            if not imported:
                # Nothing reached the library, which an applied card must not claim.
                return await self._skills_partial(consumed, owner_repo, failure_detail, service_url)
            attach = await attach_imported_skills(
                self._runtime,
                tenant_id=consumed.tenant_id,
                agent_id=consumed.agent_id,
                outcomes=imported,
            )
            log.info(
                "teams.credential.skill_repo_imported",
                imported=len(imported),
                attached=attach.attached,
                note=attach.note,
            )
            queued = await settle_credential_submit(
                self._runtime.sessionmaker,
                row=consumed,
                platform="teams",
                outcome="applied" if attach.attached else "write_failed",
                carries_work=attach.attached,
            )
            detail = [failure_detail] if attach.attached else [attach.note, failure_detail]
            change = ConfigurationChange(
                target_name=consumed.target_name or attach.agent_name or "the agent",
                kind="skills_bulk",
                availability="next_message" if attach.attached else "saved",
                repo=owner_repo,
                count=len(imported),
                detail="\n".join(line for line in detail if line) or None,
            )
            state: CardState = "applied" if attach.attached else "partial"
            await self._edit(consumed, state, service_url, outcome=change)
            if attach.attached and queued:
                await self._resume(consumed, service_url)

    async def _skills_partial(
        self,
        row: CredentialRequestRow,
        repo: str,
        detail: str | None,
        service_url: str | None,
    ) -> None:
        """The token is stored but no skill reached the agent: audited, no turn promised."""
        await settle_credential_submit(
            self._runtime.sessionmaker,
            row=row,
            platform="teams",
            outcome="write_failed",
            carries_work=False,
        )
        # `preparation_failed` names no count, but the change model requires one.
        change = ConfigurationChange(
            target_name=row.target_name or "the agent",
            kind="skills_bulk",
            availability="preparation_failed",
            repo=repo,
            count=1,
            detail=detail,
        )
        await self._edit(row, "partial", service_url, outcome=change)

    async def _refuse(
        self,
        row: CredentialRequestRow,
        reason: RefusalReason,
        service_url: str | None,
    ) -> None:
        """Close a spent request that wrote nothing; no work resumes on it."""
        outcome: CredentialRequestOutcome = (
            "token_rejected"
            if reason == "token_rejected"
            else "policy_refused"
            if reason == "mcp_permission_required"
            else "write_failed"
        )
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
                platform="teams",
                # Only the requester may submit, so they are the caller.
                caller=await channel_admin_caller(
                    self._runtime,
                    tenant_id=row.tenant_id,
                    user_id=row.requester_platform_user_id,
                    is_admin=is_admin,
                ),
                default=self._runtime.deployment_default,
                shares_token=True,
            )
            if row.mcp_server_url is not None
            else McpConnectDecision(replaces=False, replace_allowed=False)
        )
        now = datetime.now(UTC)
        try:
            async with self._runtime.sessionmaker.begin() as session:
                consumed = await consume_form_unless_pinned(session, row=row, agent=agent, now=now)
        except FormPinRefused:
            log.info("teams.credential.pin_refused", kind="mcp")
            return
        if consumed is None:
            log.info("teams.credential.already_used", kind="mcp")
            return
        async with guard_credential_save(
            self._runtime.sessionmaker,
            row=consumed,
            edit_retry=lambda retry, reason: self._edit(
                retry, "requested", service_url, retry_reason=reason
            ),
            edit_pending=lambda pending, notice: self._edit(
                pending, "received", service_url, saving_notice=notice
            ),
        ) as attempt:
            await self._edit(consumed, "received", service_url)
            url = consumed.mcp_server_url
            if url is None:
                attempt.retry_allowed = False
                log.error("teams.credential.mcp_missing_server_url")
                return await self._refuse(consumed, "target_unavailable", service_url)
            if connect.refused:
                attempt.retry_allowed = False
                return await self._refuse(
                    consumed,
                    "replacement_admin_required" if connect.replaces else "mcp_permission_required",
                    service_url,
                )
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
                attempt.retry_allowed = False
                return await self._refuse(consumed, "replacement_admin_required", service_url)
            except (McpAgentGoneError, McpAttachFailedError) as err:
                attempt.retry_allowed = not isinstance(err, McpAgentGoneError)
                log.warning(
                    "teams.credential.mcp_attach_failed",
                    err_type=type(err.__cause__ or err).__name__,
                )
                return await self._refuse(consumed, "target_unavailable", service_url)
            except McpTokenWriteFailedError as err:
                log.warning(
                    "teams.credential.mcp_write_failed",
                    err_type=type(err.__cause__ or err).__name__,
                )
                attached = False
            queued = await settle_credential_submit(
                self._runtime.sessionmaker,
                row=consumed,
                platform="teams",
                outcome="applied" if attached else "write_failed",
                carries_work=attached,
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
