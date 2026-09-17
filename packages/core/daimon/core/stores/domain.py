"""Pydantic domain types returned at the store boundary.

Callers of stores never see SQLAlchemy ORM instances — stores convert rows to
these models before returning (`Row.model_validate(orm, from_attributes=True)`).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from daimon.core.session_snapshot import SessionSnapshot
from pydantic import BaseModel, ConfigDict

# NOTE: Adding a platform requires updating this Literal AND the DB column
# (currently untyped Text). If mismatched, Pydantic model_validate raises
# ValidationError on read — keep in sync.
Platform = Literal["discord", "cli", "slack", "teams"]

# Session-continuity vocabularies. Same contract as `Platform` above: the
# columns are untyped Text, so a value outside the Literal raises on read.
# `transfer_kind` says how much of a task survived a session replacement;
# a preparation's `stage` is its resume point.
TransferKind = Literal["full", "transcript", "history"]
PreparationStage = Literal["decided", "checkpointed", "uploaded", "created", "completed", "failed"]
# What the caller answered about uncommitted repository changes: carry them
# into the successor's working files, or leave them in the old checkout.
UnsavedWorkChoice = Literal["copy", "leave"]
ContinuationReason = Literal["task_handoff", "private_input_applied"]
ContinuationStatus = Literal["pending", "claimed", "delivered", "skipped"]


class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class AccountRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    created_at: datetime


class AccountIdentityRow(BaseModel):
    """Full account identity from a three-table JOIN (accounts→tenants→platform_principals).

    Not from_attributes — this is a column-tuple result, not a single ORM instance.
    """

    model_config = ConfigDict(frozen=True)  # no from_attributes — column-tuple JOIN

    account_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    platform: str
    external_id: str  # tenant.external_id — guild snowflake for discord
    platform_user_id: (
        str | None
    )  # platform_principals.external_id (LEFT JOIN on tenant's platform; null when none)


class CliPrincipalRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    os_user: str
    account_id: uuid.UUID
    created_at: datetime


class PlatformPrincipalRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: Platform
    external_id: str
    account_id: uuid.UUID
    created_at: datetime
    active_agent_name: str | None = None


class PrincipalLinkRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    cli_principal_id: uuid.UUID
    platform_principal_id: uuid.UUID
    linked_at: datetime


class TenantRow(BaseModel):
    """Canonical per-tenant identity + lifecycle row. Returned by stores.tenants.get_tenant.

    platform/external_id carry the folded workspace identity; provision_status/archived_at
    carry the tenant lifecycle.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    platform: str  # "discord" | "cli"
    external_id: str  # = folded workspace_id
    provision_status: str  # "ready" | "pending" | "failed"
    last_reconcile_error: str | None = None
    archived_at: datetime | None = None
    registered_at: datetime
    created_at: datetime


class UserConfigRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    account_id: uuid.UUID
    agent_name: str | None
    environment_name: str | None


@dataclass(frozen=True)
class TenantDependentCounts:
    """Per-table dependent-row counts for a single tenant.

    Used by delete_tenant to give callers a blast-radius preview before
    confirming a cascade delete.
    """

    routines: int
    usage_events: int
    payment_events: int
    tenant_ledger: int
    tenant_user_caps: int
    agent_files: int
    agent_repo_binding: int
    tenant_config: int
    channel_config: int

    @property
    def total(self) -> int:
        return (
            self.routines
            + self.usage_events
            + self.payment_events
            + self.tenant_ledger
            + self.tenant_user_caps
            + self.agent_files
            + self.agent_repo_binding
            + self.tenant_config
            + self.channel_config
        )


class RoutineRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by_user_id: str | None
    agent_id: str
    agent_name: str
    cron_expr: str
    timezone: str
    trigger_message: str
    enabled: bool
    next_fire_at: datetime | None
    last_fired_at: datetime | None
    last_error: str | None
    last_result_tail: str | None
    created_at: datetime
    updated_at: datetime


class ThreadSessionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    thread_id: str
    account_id: uuid.UUID | None
    ma_session_id: str
    ma_agent_id: str | None = None
    watermark_message_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    active_turn_message_id: str | None = None
    active_turn_started_at: datetime | None = None
    active_turn_channel_id: str | None = None
    # The configuration the MA session froze at create time. NULL on rows
    # written before continuity existed — callers read that as "unknown".
    effective_config: SessionSnapshot | None = None
    identity_fingerprint: str | None = None
    mutable_fingerprint: str | None = None
    predecessor_id: uuid.UUID | None = None
    replaced_by_id: uuid.UUID | None = None
    transfer_file_id: str | None = None
    transfer_kind: TransferKind | None = None
    fresh_start_requested_at: datetime | None = None
    # The caller's answer to the uncommitted-work question, waiting for the
    # replacement it governs. None means unanswered, which reads as the
    # default: capture the changes.
    pending_unsaved_work: UnsavedWorkChoice | None = None


class SessionPreparationRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    mapping_id: uuid.UUID
    target_fingerprint: str
    stage: PreparationStage
    transfer_file_id: str | None
    transfer_kind: TransferKind | None
    new_mapping_id: uuid.UUID | None
    failure_reason: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class TaskContinuationRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    thread_id: str
    parent_channel_id: str
    requester_account_id: uuid.UUID
    requester_external_user_id: str
    target_ma_agent_id: str
    target_name: str
    requested_work: str | None
    reason: ContinuationReason
    status: ContinuationStatus
    skip_reason: str | None
    idempotency_key: uuid.UUID
    created_at: datetime
    claimed_at: datetime | None
    delivered_at: datetime | None


class GitHubOauthStateRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    state: uuid.UUID
    platform: str
    platform_user_id: str
    scopes: tuple[str, ...]
    created_at: datetime
    consumed_at: datetime | None
    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None = None


class GitHubCredentialRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    principal_id: uuid.UUID
    github_login: str
    encrypted_token: bytes
    scopes: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class AgentMcpCredentialRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    mcp_server_url: str
    encrypted_token: bytes
    created_at: datetime
    updated_at: datetime


class SlackBotTokenRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    team_id: str
    encrypted_token: bytes
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    refresh_token: bytes | None = None


class SlackUserTokenRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    team_id: str
    slack_user_id: str
    encrypted_token: bytes
    scopes: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    encrypted_refresh_token: bytes | None = None


class SlackTurnContextRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    channel_id: str
    thread_ts: str
    started_at: datetime


class AgentGithubBindingRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    agent_id: uuid.UUID
    principal_id: uuid.UUID


class AgentGoogleBindingRow(BaseModel):
    """Pydantic row for AgentGoogleBinding.

    `scopes` is a tuple (not list) so the frozen model stays hashable.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    agent_id: uuid.UUID
    email: str
    scopes: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class UsageEventRow(BaseModel):
    """Per-turn usage event row. BILL-01."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    occurred_at: datetime
    platform_user_id: str | None
    managed_session_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    event_id: str


class TenantUserCapRow(BaseModel):
    """Per-(tenant, user) cap row. NULL platform_user_id = tenant default. BILL-01."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform_user_id: str | None
    cap_usd: Decimal
    updated_at: datetime


class PaymentEventRow(BaseModel):
    """Stripe webhook dedup row. id = stripe event id (text). BILL-01."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    tenant_id: uuid.UUID
    amount_usd: Decimal
    source: str
    credited_at: datetime | None
    occurred_at: datetime


class TenantLedgerRow(BaseModel):
    """Append-only ledger row. Balance = SUM(delta_usd). TOPUP-01."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    delta_usd: Decimal
    reason: str
    idempotency_key: str
    payment_event_id: str | None
    payment_intent: str | None
    occurred_at: datetime


class SeededSkillRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    name: str
    content_hash: str
    anthropic_id: str
    updated_at: datetime


class UserSkillRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    agent_name: str
    name: str
    source_repo_url: str
    source_repo_branch: str
    source_path: str
    content_hash: str
    anthropic_id: str | None
    anthropic_latest_version: str | None
    updated_at: datetime


class AgentFileRow(BaseModel):
    """Pydantic row for AgentFile."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    content: str
    created_by_account_id: uuid.UUID | None = None
    last_set_by_account_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class PendingFileDeleteRow(BaseModel):
    """Pydantic row for PendingFileDelete."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    file_id: str
    delete_after: datetime
    created_at: datetime


RepoProofKind = Literal["public", "pat"]


class RepoAccessProof(BaseModel):
    """Bind-time evidence that the binder could read a specific repo.

    `at` is caller-supplied, not read from a clock inside this model or any
    store/pure function that constructs one — the shell that just performed
    the probe (a public-repo check or a PAT read) is the only place a
    timestamp should come from. `account_id` is nullable because a
    system-initiated proof (e.g. a one-shot operator backfill confirming a
    repo is public) has no acting person to attribute it to.
    """

    model_config = ConfigDict(frozen=True)

    kind: RepoProofKind
    at: datetime
    account_id: uuid.UUID | None


class AgentRepoBindingRow(BaseModel):
    """Pydantic row for AgentRepoBinding."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    repo_url: str
    default_branch: str
    ma_secret_ref: str
    last_sync_at: datetime | None = None
    last_sync_error: str | None = None
    proof_kind: RepoProofKind | None = None
    proof_at: datetime | None = None
    proof_account_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AgentSkillRepoCredentialRow(BaseModel):
    """Pydantic row for AgentSkillRepoCredential — a per-repo skill-import token.

    Separate from `AgentRepoBindingRow` for the reason its ORM docstring
    gives: one working repo per agent, any number of skill repos, and
    enrolling one must never move the other.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    repo_url: str
    default_branch: str
    path: str
    ma_secret_ref: str
    proof_kind: RepoProofKind | None = None
    proof_at: datetime | None = None
    proof_account_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AgentMemoryStoreRow(BaseModel):
    """Pydantic row for AgentMemoryStore (agent memory feature)."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    memory_store_id: str
    created_at: datetime


class GitHubAppInstallationRow(BaseModel):
    """Pydantic row for GitHubAppInstallation."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    installation_id: int
    account_login: str
    repo_full_names: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class CredentialRequestRow(BaseModel):
    """Pydantic row for CredentialRequest — the credential-button handshake."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    token: str
    kind: str
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID
    target: str
    mcp_server_url: str | None
    requester_platform_user_id: str
    channel_id: str
    platform: str | None = None
    parent_channel_id: str | None = None
    origin_thread_id: str | None = None
    posted_message_id: str | None = None
    idempotency_key: uuid.UUID
    requested_work: str | None = None
    target_ma_agent_id: str | None = None
    target_name: str | None = None
    responder_name: str | None = None
    replaces_updated_at: datetime | None = None
    outcome: str | None = None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None


class McpOAuthFlowRow(BaseModel):
    """Pydantic row for McpOAuthFlow — one in-flight MCP OAuth authorization."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    state: str
    request_token: str
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    server_name: str
    mcp_server_url: str
    redirect_uri: str
    code_verifier: str
    client_id: str | None
    client_secret_encrypted: str | None
    token_endpoint_auth_method: str | None
    token_endpoint: str | None
    authorization_endpoint: str | None
    resource: str | None
    scope: str | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    completed_at: datetime | None


class McpOAuthGrantRow(BaseModel):
    """One person's finished OAuth sign-in to an MCP server URL on one agent.

    The completed-flow projection: a `mcp_oauth_flows` row whose
    `completed_at` is set is the durable record that this account's grant was
    stored in its (account, agent) vault — not merely that it opened the
    link, which `used_at` records for anyone who reached the callback. Keyed
    by URL, which is what MA authenticates against; the server's name is
    per agent and not part of it. Erasing the platform user deletes the flow
    row, and with it the grant this row reports.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    agent_id: uuid.UUID
    account_id: uuid.UUID
    mcp_server_url: str


class MessageFeedbackRow(BaseModel):
    """Pydantic row for MessageFeedback — one thumbs-up/down reaction vote.

    `vote` is a bare `str` at the store boundary, the same narrowing
    precedent `CredentialRequestRow.kind` already sets — the `Vote` literal
    lives in `daimon.core.message_feedback`, not here.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    platform: str
    message_id: str
    channel_id: str
    platform_user_id: str
    ma_session_id: str | None
    vote: str
    feedback_text: str | None
    created_at: datetime
    updated_at: datetime


class RecordVoteResult(BaseModel):
    """Return of `record_vote`: the upserted row plus the voter's prior vote.

    `previous_vote` is the value the voter had recorded for this message
    before this call, and is `None` when this is their first vote on it.
    """

    model_config = ConfigDict(frozen=True)

    row: MessageFeedbackRow
    previous_vote: str | None


class WizardSessionRow(BaseModel):
    """Pydantic row for WizardSession — one multi-step wizard form's durable state."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    requester_platform_user_id: str
    channel_id: str
    message_id: str
    spec: dict[str, Any]
    answers: dict[str, list[str]]
    current_step: int
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class McpTokenRow(BaseModel):
    """Pydantic row for McpToken.

    Represents a minted agent-scoped MCP JWT registered in the `mcp_tokens`
    table. The verifier reads `revoked_at` to reject revoked tokens without
    rotating the shared HS256 secret.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    jti: uuid.UUID
    account_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None


class FileUploadRow(BaseModel):
    """Pydantic row for FileUpload — a staged chat attachment.

    `content` is None between the mint and the sandbox's PUT; a consumer that
    reads a row with no content is looking at an upload that never landed, not
    at an empty file.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: str
    tenant_id: uuid.UUID
    title: str
    display_filename: str
    content_type: str
    content: bytes | None
    created_at: datetime


class SupportEscalationRow(BaseModel):
    """Pydantic row for SupportEscalation — one human-support request."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    platform: str
    platform_user_id: str
    channel_id: str
    message_id: str
    ma_session_id: str | None
    note: str
    delivered_at: datetime | None
    created_at: datetime


class ThreadAutoResponseRow(BaseModel):
    """One reply the agent posted in a thread unprompted (the rate-limit ledger)."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    thread_id: str
    message_id: str
    created_at: datetime


class ThreadAgentBindingRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    parent_channel_id: str
    thread_id: str
    kind: Literal["setup", "handoff"] = "setup"
    responder_ma_agent_id: str
    responder_name: str
    configuration_target_ma_agent_id: str | None
    configuration_target_name: str | None
    creator_account_id: uuid.UUID | None
    archived: bool
    locked: bool
    deleted: bool
    created_at: datetime
    updated_at: datetime


class TurnOriginRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    parent_channel_id: str
    thread_id: str
    responder_ma_agent_id: str
    responder_name: str
    configuration_target_ma_agent_id: str | None
    configuration_target_name: str | None
    is_setup: bool = False
    role: Role
    created_at: datetime
    expires_at: datetime
