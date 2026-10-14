"""Gemini C6 facts through the operator CLI, explicitly opted into native A1.

Shares the OpenAI consumer's copy execution and frozen assertion semantics.
Independent catalog/native reads remain separate from the CLI's printed table.
Unsupported fixture conversion, edits, turns and cleanup remain typed pending.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

from daimon.adapters.cli.commands.agents import agents_get
from daimon.adapters.cli.runtime import CliRuntime, GeminiResourceGrant
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.errors import DaimonError
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt, ResourceEdge
from daimon.testing.cli_resources import CliCheckReceipt, CliResourceEdge
from mux.contracts.ids import Scope
from mux.drivers.gemini import GeminiManagedAgents
from mux.errors import ScopeViolation
from rich.console import Console

GEMINI_OPERATOR_PROFILE = "opt-in-gemini-operator"
GEMINI_PROFILE = "gemini.inline_reuse"


@dataclass
class GeminiHostFactory:
    backend: GeminiManagedAgents
    binding: AdminBindings
    account_scope_id: str

    def __call__(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        principal_id: str,
        authorization_id: str,
        operation_key: str,
    ) -> NeutralResourceHost:
        if (
            str(tenant_id) != self.binding.scope.tenant_id
            or str(account_id) != self.binding.scope.account_id
        ):
            raise ScopeViolation("gemini-cli", "operator selection differs from the owned run")
        return NeutralResourceHost(
            self.backend,
            self.binding.sessions,
            scope=Scope(
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                principal_id=principal_id,
                authorization_id=authorization_id,
            ),
            account_scope_id=self.account_scope_id,
            operation_key=operation_key,
            resource_catalog_enabled=True,
            catalog_adapter=self.backend.catalog_adapter,
        )


@dataclass
class GeminiCliResourceEdge(CliResourceEdge):
    """Opt-in Gemini execution; does not change the default OpenAI binding."""

    grants: tuple[GeminiResourceGrant, ...] = ()

    @property
    def driver(self) -> GeminiManagedAgents:
        if not isinstance(self.backend, GeminiManagedAgents):
            raise ScopeViolation("gemini-cli", "actual Gemini driver required")
        return self.backend

    def runtime(self) -> CliRuntime:
        return replace(
            super().runtime(),
            resource_hosts=None,
            gemini_resource_hosts=GeminiHostFactory(self.driver, self.binding, self.workspace),
            gemini_resource_grants=self.grants,
        )

    async def authorize(self, channel_id: str) -> None:
        await self.binding.authorize_command(
            AdminCommand(
                "channel_rule_set",
                self.binding.platform,
                self.binding.workspace_id,
                channel_id,
            )
        )
        if self.binding.backend != "gemini" or self.driver.catalog_adapter is None:
            raise ScopeViolation("gemini-cli", "native Gemini opt-in collaborators required")
        async with self.binding.sessions() as session:
            revision = await current_backend(
                session,
                channel_ref(
                    uuid.UUID(self.binding.scope.tenant_id), self.binding.platform, channel_id
                ),
            )
        if revision is None or revision.profile != GEMINI_PROFILE:
            raise ScopeViolation("gemini-cli", "the channel is not configured for Gemini")
        self.grants = (
            GeminiResourceGrant(
                tenant_id=uuid.UUID(self.binding.scope.tenant_id),
                account_id=uuid.UUID(self.binding.scope.account_id),
                platform=self.binding.platform,
                channel_id=channel_id,
                config_digest=revision.digest,
                workspace=self.workspace,
            ),
        )

    async def __call__(self, command: AdminCommand) -> AdminReceipt:
        await self.binding.authorize_command(command)
        if self.binding.backend != "gemini":
            raise ScopeViolation("gemini-cli", "Gemini column required")
        if command.operation == "channel_rule_set" and "--copy-from" in command.options:
            receipt = await self._copy(command)
            return receipt.model_copy(
                update={
                    "observations": {
                        **receipt.observations,
                        "profile": GEMINI_OPERATOR_PROFILE,
                    }
                }
            )
        if self.fallback is not None:
            return await self.fallback(command)
        return AdminReceipt(
            status="pending",
            backend="gemini",
            operation=command.operation,
            code="ADMIN_RESOURCE_OPERATION_UNBOUND",
        )

    async def cli_check(self, command: str) -> CliCheckReceipt:
        words = shlex.split(command)
        if len(words) != 6 or words[:3] != ["daimon", "agents", "--guild"] or words[4] != "get":
            raise ValueError("cli_check runs only `daimon agents --guild G get NAME`")
        if words[3] != self.binding.workspace_id or self.binding.backend != "gemini":
            raise ScopeViolation("gemini-cli", "readback requires the owned Gemini workspace")
        await self.binding.authorize_command(
            AdminCommand(
                "agents_get", self.binding.platform, self.binding.workspace_id, args=(words[5],)
            )
        )
        output = StringIO()
        error = None
        try:
            await agents_get(
                rt=self.runtime(),
                console=Console(file=output, width=200, force_terminal=False, highlight=False),
                name=words[5],
                as_json=False,
                selector=TenantSelector(guild_id=words[3]),
            )
        except DaimonError as exc:
            error = type(exc).__name__
        receipt = CliCheckReceipt(
            profile=GEMINI_OPERATOR_PROFILE, command=command, output=output.getvalue(), error=error
        )
        self.checks.append(receipt)
        return receipt


@asynccontextmanager
async def opt_in_gemini_operator_profile(
    binding: AdminBindings,
    backend: GeminiManagedAgents,
    *,
    workspace: str,
    root: Path,
    fallback: ResourceEdge | None = None,
) -> AsyncIterator[GeminiCliResourceEdge]:
    """Install only for this explicit context and restore the resource edge on exit."""
    await binding.authorize_command(
        AdminCommand("resource_edge_install", binding.platform, binding.workspace_id)
    )
    if binding.backend != "gemini" or backend.catalog_adapter is None:
        raise ScopeViolation("gemini-cli", "enabled native Gemini adapter required")
    edge = GeminiCliResourceEdge(
        binding, backend, workspace, root / "gemini-operator-deployments.json", fallback=fallback
    )
    previous_edge = binding.resource_edge
    binding.resource_edge = edge
    try:
        yield edge
    finally:
        binding.resource_edge = previous_edge
