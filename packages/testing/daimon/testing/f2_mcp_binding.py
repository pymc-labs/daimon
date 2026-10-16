"""F2 agent-scoped credential owner, invoked only after an acknowledged agent."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti, validate_agent_mcp_token
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from mux.contracts.ids import Provider, ResourceRef, Scope
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from daimon.testing.anthropic_live import LiveContext


@dataclass(frozen=True)
class AgentMCPAuthority:
    """Selected owner authority for one actual provider ACK and canonical source.

    `acknowledged` must verify the actual persisted ACK/readback AND its
    association with canonical_source, not accept an input flag. Foreign
    provider IDs never become Daimon agent UUIDs by string substitution.
    """

    provider: Provider
    scope: Scope
    account_scope_id: str
    output: Path
    deadline_s: int
    database_url: str | None = field(repr=False)
    mcp_signing_secret_file: Path | None = field(repr=False)
    acknowledged: Callable[[ResourceRef], bool] = field(repr=False)
    redact: Callable[[tuple[str, ...]], None] = field(repr=False)
    canonical_source: ResourceRef | None = None
    chat_execution: bool = False


class AgentMCPBinding:
    """One minted registered token for this attempt's actual created agent; no retries."""

    def __init__(
        self, context: LiveContext | None = None, *, authority: AgentMCPAuthority | None = None
    ) -> None:
        if (context is None) == (authority is None):
            raise ValueError("exactly one acknowledged-agent authority is required")
        self.context = context
        if context is not None:
            binding = context.admission.binding

            def redact(values: tuple[str, ...]) -> None:
                context.redaction_secrets += values

            authority = AgentMCPAuthority(
                provider="anthropic",
                scope=binding.scope,
                account_scope_id=binding.account_scope_id,
                output=context.admission.output,
                deadline_s=context.admission.deadline_s,
                database_url=binding.database_url,
                mcp_signing_secret_file=binding.mcp_signing_secret_file,
                acknowledged=lambda agent: (
                    ("agent", agent.id) in context.transport.authorization.created
                ),
                redact=redact,
            )
        assert authority is not None
        self.authority = authority
        self.engine: AsyncEngine | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None
        self.agent: UUID | None = None
        self.jti: UUID | None = None
        self.token: str | None = None
        self.started = False

    def receipt(self, name: str, value: dict[str, object]) -> None:
        path = self.authority.output / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def bind(self, agent: ResourceRef) -> str:
        from daimon.testing.anthropic_live import LiveRefused, explicit_secret

        binding = self.authority
        if self.started:
            raise LiveRefused(
                "F2 credential binding cannot be replayed or switched to another agent"
            )
        self.started = True
        if (
            agent.provider,
            agent.kind,
            agent.account_scope_id,
            agent.tenant_id,
            agent.account_id,
        ) != (
            binding.provider,
            "agent",
            binding.account_scope_id,
            binding.scope.tenant_id,
            binding.scope.account_id,
        ) or not binding.acknowledged(agent):
            raise LiveRefused("F2 credential requires this attempt's acknowledged agent")
        if binding.database_url is None or binding.mcp_signing_secret_file is None:
            raise LiveRefused("F2 requires explicit staging DB and MCP signing authority")
        canonical = binding.canonical_source or agent
        if (
            canonical.provider != "anthropic"
            or canonical.kind != "agent"
            or canonical.tenant_id != binding.scope.tenant_id
            or canonical.account_id not in (None, binding.scope.account_id)
            or (binding.provider == "anthropic" and canonical != agent)
            or (binding.provider != "anthropic" and binding.canonical_source is None)
        ):
            raise LiveRefused(
                "F2 credential requires the acknowledged canonical source association"
            )
        secret = explicit_secret(binding.mcp_signing_secret_file)
        binding.redact((secret, binding.database_url))
        self.agent = derive_agent_uuid(
            tenant_id=UUID(canonical.tenant_id), ma_agent_id=canonical.id
        )
        self.engine = create_async_engine(binding.database_url, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        account, tenant = UUID(agent.account_id), UUID(agent.tenant_id)
        async with self.sessions() as db:
            identity = await get_account_with_tenant(db, account_id=account)
            if identity is None or identity.tenant_id != tenant or identity.role.value != "user":
                raise LiveRefused("F2 token requires its actual isolated nonadmin account")
        now = datetime.now(UTC)
        ttl = min(900, binding.deadline_s)
        async with self.sessions.begin() as db:
            token = await mint_agent_mcp_token(
                db,
                account_id=account,
                tenant_id=tenant,
                agent_id=self.agent,
                label="F2-owned-session-boundary",
                secret=secret.encode(),
                now=now,
                chat_execution=binding.chat_execution,
                ttl_days=ttl / 86400,  # pyright: ignore[reportArgumentType]
            )
            binding.redact((token,))
            self.jti = token_jti(token)
            self.receipt(
                "f2-mcp-token-intent.json",
                {
                    "jti": str(self.jti),
                    "tenant_id": str(tenant),
                    "account_id": str(account),
                    "agent_id": str(self.agent),
                    "native_agent_id": agent.id,
                    "commit_outcome": "pending",
                    "token_exported": False,
                },
            )
        try:
            await validate_agent_mcp_token(
                self.sessions,
                token=token,
                secret=secret.encode(),
                scope=binding.scope,
                agent_id=self.agent,
                now=now,
                max_ttl_s=ttl,
                chat_execution=binding.chat_execution,
            )
        except ValueError:
            raise LiveRefused(
                "F2 agent-token registry readback differs from its ACK binding"
            ) from None
        self.token = token
        return token

    async def close(self) -> None:
        from daimon.testing.anthropic_live import LiveRefused

        try:
            if self.jti is not None:
                assert self.sessions is not None
                scope = self.authority.scope
                async with self.sessions.begin() as db:
                    row = await get_mcp_token(db, jti=self.jti)
                    if row is not None:
                        if (
                            str(row.account_id) != scope.account_id
                            or str(row.tenant_id) != scope.tenant_id
                            or row.agent_id != str(self.agent)
                            or row.label != "F2-owned-session-boundary"
                        ):
                            raise LiveRefused("refuse foreign F2 token cleanup")
                        await revoke_mcp_token(db, jti=self.jti, now=datetime.now(UTC))
                async with self.sessions() as db:
                    after = await get_mcp_token(db, jti=self.jti)
                    if after is not None and after.revoked_at is None:
                        raise LiveRefused("F2 token revocation readback failed")
                self.receipt(
                    "f2-mcp-token-cleanup.json",
                    {
                        "jti": str(self.jti),
                        "revoked_or_absent_verified": True,
                        "token_exported": False,
                    },
                )
        finally:
            self.token = None
            if self.engine is not None:
                await self.engine.dispose()
