"""The per-turn Daimon MCP credential of an agent-template turn (OpenAI and Gemini).

A template row with `template_capabilities: "mcp"` keeps the source agent's Daimon
MCP toolset without any operator credential. Each turn mints one registered caller
credential in memory: the requesting person's account with the agent's
chat-execution authority (no admin elevation; roles are re-read live by the
server), surface `provider_turn` (the server refuses it gated and publishing tools),
living at most min(turn deadline + 60 s, 900 s). It is checked against the
consented catalogue before use, and `finalize` revokes it and reads that back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

from daimon.core.turn.errors import AdmissionDenied
from mux.errors import ScopeViolation

if TYPE_CHECKING:
    from daimon.core.stores.domain import BackendAgent
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from mux.contracts.ids import Scope
    from mux.contracts.resources import AgentSpec
    from pydantic import JsonValue


def template_mcp_kept(target: AgentSpec | None) -> bool:
    """A template keeps MCP unless the agent's switch dropped it (then it has none)."""
    return target is None or "mcp" not in target.declared_drops


def consented_catalogue(source: BackendAgent, target: AgentSpec | None) -> frozenset[str]:
    """The Daimon MCP catalogue the switch consented to: allowed plus named-unavailable."""
    from daimon.core.turn.runtime_manifest import represented_source

    represented = represented_source(source, target)
    if represented is None:
        raise AdmissionDenied(reason="backend_unsupported")
    servers = represented.target.mcp_servers or ()
    if len(servers) != 1:
        raise AdmissionDenied(reason="backend_unsupported")
    connection = servers[0]
    allowed = connection.tool_policy.get("allowed_tools")
    if not isinstance(allowed, list):
        raise AdmissionDenied(reason="backend_unsupported")
    prefix = f"mcp_tool:{connection.name}:"
    return frozenset(
        [str(name) for name in cast("list[JsonValue]", allowed)]
        + [
            item.removeprefix(prefix)
            for item in represented.unavailable_mcp_tools
            if item.startswith(prefix)
        ]
    )


class TemplateMCPCredential:
    """One turn's caller credential for the template's Daimon MCP server.

    The host sets `catalogue` once the counterpart is verified (`consented_catalogue`)
    and clears it whenever verification restarts; nothing resolves without it.
    """

    def __init__(self, request: ProviderPreparationRequest, scope: Scope, *, label: str) -> None:
        self._request, self._scope, self._label = request, scope, label
        self.catalogue: frozenset[str] | None = None
        self._minted: tuple[str, UUID] | None = None

    async def resolve(self, authorized: Scope, reference: str, destination: str) -> str:
        from daimon.core.ma_identity import derive_agent_uuid
        from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti, validate_agent_mcp_token
        from daimon.core.mcp_oauth.discovery import list_mcp_tools
        from daimon.core.stores.accounts import get_account_with_tenant

        request, scope = self._request, self._scope
        agent_id = derive_agent_uuid(
            tenant_id=request.tenant_id, ma_agent_id=request.admission.agent.id
        )
        public_url = request.deps.mcp.public_url
        if (
            self.catalogue is None
            or authorized != scope
            or scope.is_platform
            or scope.is_legacy_host_authorized
            or reference != f"caller-mcp:{scope.tenant_id}:{scope.account_id}:{agent_id}"
            or public_url is None
            or destination.rstrip("/") != str(public_url).rstrip("/")
            or request.deps.mcp.jwt_secret is None
        ):
            raise ScopeViolation("mcp-credential", "MCP credential has no verified source binding")
        if self._minted is not None:
            return self._minted[0]
        now = request.now()
        ttl_s = min(int(max((request.deadline - now).total_seconds(), 0.0)) + 60, 900)
        secret = request.deps.mcp.jwt_secret.get_secret_value().encode()
        tenant, account = UUID(scope.tenant_id), UUID(scope.account_id)
        async with request.deps.sessionmaker.begin() as db:
            identity = await get_account_with_tenant(db, account_id=account)
            if identity is None or identity.tenant_id != tenant:
                raise ScopeViolation("mcp-credential", "MCP caller is outside the admitted tenant")
            token = await mint_agent_mcp_token(
                db,
                account_id=account,
                tenant_id=tenant,
                agent_id=agent_id,
                label=self._label,
                secret=secret,
                now=now,
                ttl_days=ttl_s / 86400,  # pyright: ignore[reportArgumentType]
                chat_execution=True,
                surface="provider_turn",
            )
        self._minted = token, token_jti(token)
        try:
            await validate_agent_mcp_token(
                request.deps.sessionmaker,
                token=token,
                secret=secret,
                scope=scope,
                agent_id=agent_id,
                now=now,
                max_ttl_s=ttl_s,
                chat_execution=True,
            )
            if await list_mcp_tools(destination, token) != self.catalogue:
                raise ValueError("target MCP catalogue differs from captured source")
        except (OSError, ValueError):
            await self.finalize()
            raise ScopeViolation("mcp-credential", "invalid scoped MCP credential") from None
        return token

    async def finalize(self) -> None:
        """Revoke this turn's MCP credential, then read the revocation back."""
        if self._minted is None:
            return
        from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token

        request = self._request
        _, jti = self._minted
        async with request.deps.sessionmaker.begin() as db:
            await revoke_mcp_token(db, jti=jti, now=request.now())
        async with request.deps.sessionmaker() as db:
            row = await get_mcp_token(db, jti=jti)
        if row is not None and row.revoked_at is None:
            raise ScopeViolation("mcp-credential", "MCP credential revocation readback failed")
        self._minted = None
