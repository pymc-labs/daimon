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
from typing import Literal

from pydantic import BaseModel, ConfigDict

# NOTE: Adding a platform requires updating this Literal AND the DB column
# (currently untyped Text). If mismatched, Pydantic model_validate raises
# ValidationError on read — keep in sync.
Platform = Literal["discord", "cli", "slack"]


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
    watermark_message_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


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
    created_at: datetime
    updated_at: datetime


class PendingFileDeleteRow(BaseModel):
    """Pydantic row for PendingFileDelete."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    file_id: str
    delete_after: datetime
    created_at: datetime


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
