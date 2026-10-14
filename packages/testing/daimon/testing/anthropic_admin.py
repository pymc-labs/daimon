"""Explicit offline Anthropic CLI and member-edit bindings; facts, never a verdict.

Product functions, permission checks, provider revisions and emitted CLI JSON
are observed unchanged. The caller supplies only an owned fixture manifest and
an isolated CliRuntime sharing the run's sessions/client. No runtime discovery.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from daimon.adapters.cli.commands.agents import agents_archive, agents_create, agents_get
from daimon.adapters.cli.commands.channels import channels_rule_set
from daimon.adapters.cli.runtime import CliRuntime
from daimon.adapters.cli.tenant import TenantSelector
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _attach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.authz import Subject
from daimon.core.channel_copies import archive_channel_copy
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_CHANNEL_COPY
from daimon.core.errors import DaimonError, StoreError
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import PlatformPrincipalRow, Role
from daimon.core.stores.identity import get_principal_by_id
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt, parse_admin
from fastmcp.exceptions import ToolError
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from pydantic import BaseModel, ConfigDict, JsonValue
from rich.console import Console


@dataclass(frozen=True)
class AgentFixture:
    """Explicit fixture file, admitted by exact content hash, never a shell path."""

    path: Path
    sha256: str


class AgentReadback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str
    name: str
    native_id: str
    version: int
    payload: dict[str, JsonValue]
    cli_json: str
    cli_text: str


@dataclass(frozen=True)
class MemberAgentEdit:
    scope: Scope
    name: str
    description: str = "qa probe"
    server_name: str = "deepwiki"
    server_url: str = "https://mcp.deepwiki.com/mcp"


@dataclass
class AnthropicAdminBindings:
    binding: AdminBindings
    runtime: CliRuntime
    fixture_root: Path
    fixtures: Mapping[str, AgentFixture] = field(default_factory=dict[str, AgentFixture])

    async def authorize(self, command: AdminCommand | None = None) -> None:
        await self.binding.authorize_command(
            command
            if command is not None
            else AdminCommand(
                "resource_edge_readback", self.binding.platform, self.binding.workspace_id
            )
        )
        if (
            self.runtime.sessionmaker is not self.binding.sessions
            or self.runtime.anthropic is not self.binding.anthropic
            or self.runtime.deployment_default != self.binding.default
            or self.runtime.settings.discord is not None
            or self.runtime.settings.slack is not None
            or self.runtime.settings.mcp.public_url is not None
        ):
            raise ScopeViolation("anthropic-admin", "explicit isolated run collaborators required")

    def pending(self, command: AdminCommand, code: str) -> AdminReceipt:
        return AdminReceipt(
            status="pending", backend=self.binding.backend, operation=command.operation, code=code
        )

    async def __call__(self, command: AdminCommand) -> AdminReceipt:
        await self.authorize(command)
        if self.binding.backend != "anthropic":
            return self.pending(command, "ADMIN_ANTHROPIC_C6_ONLY")
        if command.operation == "agents_get" and len(command.args) == 1 and not command.options:
            read = await self.read_agent(command.args[0])
            return self.binding.receipt(command, read.model_dump(mode="json"))
        if command.operation == "agents_create" and len(command.args) == 1 and not command.options:
            fixture = self.fixtures.get(command.args[0])
            if fixture is None:
                return self.pending(command, "ADMIN_AGENT_SPEC_FIXTURE_UNBOUND")
            path = fixture.path.resolve(strict=True)
            if (
                fixture.path.is_symlink()
                or not path.is_relative_to(self.fixture_root.resolve())
                or not path.is_file()
                or path.stat().st_size > 65536
                or hashlib.sha256(path.read_bytes()).hexdigest() != fixture.sha256
            ):
                raise ScopeViolation(
                    "anthropic-admin", "fixture differs from the explicit manifest"
                )
            from daimon.core.specs import load_agent_spec

            name = load_agent_spec(path).name
            output = StringIO()
            await agents_create(
                rt=self.runtime,
                console=Console(file=output),
                path=path,
                selector=TenantSelector(guild_id=self.binding.workspace_id),
            )
            read = await self.read_agent(name)
            return self.binding.receipt(command, read.model_dump(mode="json")).model_copy(
                update={"values": {"created.name": name, "created.id": read.native_id}}
            )
        if (
            command.operation == "agents_archive"
            and len(command.args) == 1
            and dict(command.options) == {"--yes": "true"}
        ):
            name = command.args[0]
            before = await self.read_agent(name)
            await agents_archive(
                rt=self.runtime,
                console=Console(file=StringIO()),
                name=name,
                yes=True,
                selector=TenantSelector(guild_id=self.binding.workspace_id),
            )
            remaining = await find_agent_by_daimon_tag(
                self.binding.anthropic, tenant_id=uuid.UUID(self.binding.scope.tenant_id), name=name
            )
            if remaining is not None:
                raise StoreError("archived agent remains in the independent active listing")
            return self.binding.receipt(command, {"native_id": before.native_id, "archived": True})
        if (
            command.operation == "channel_rule_set"
            and set(command.options) == {"--readers", "--copy-from"}
            and command.options["--readers"] == "own"
            and not command.args
        ):
            return await self._fork(command)
        if (
            command.operation == "archive_channel_forks"
            and not command.args
            and not command.options
        ):
            return await self._archive_fork(command)
        return self.pending(command, "ADMIN_ANTHROPIC_C6_SHAPE_UNBOUND")

    async def read_agent(self, name: str) -> AgentReadback:
        """Actual CLI JSON, followed by a separate SDK GET of the selected identity."""
        await self.authorize()
        if self.binding.backend != "anthropic":
            raise ScopeViolation("anthropic-admin", "Anthropic readback only")
        display = StringIO()
        await agents_get(
            rt=self.runtime,
            console=Console(file=display, width=4096),
            name=name,
            as_json=False,
            selector=TenantSelector(guild_id=self.binding.workspace_id),
        )
        output = StringIO()
        await agents_get(
            rt=self.runtime,
            console=Console(file=output, width=4096),
            name=name,
            as_json=True,
            selector=TenantSelector(guild_id=self.binding.workspace_id),
        )
        raw: JsonValue = json.loads(output.getvalue())
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise StoreError("CLI agent readback did not yield one native record")
        id_ = raw[0].get("id")
        if not isinstance(id_, str) or not id_:
            raise StoreError("CLI agent readback has no native identity")
        native = await retrieve_agent(
            self.binding.anthropic,
            id_,
            scope=resource_scope(
                tenant_id=self.binding.scope.tenant_id, account_id=self.binding.scope.account_id
            ),
        )
        payload = native.model_dump(mode="json")
        if payload != raw[0]:
            raise StoreError("agent changed between independent CLI and SDK reads")
        return AgentReadback(
            tenant_id=self.binding.scope.tenant_id,
            name=name,
            native_id=native.id,
            version=native.version,
            payload=payload,
            cli_json=output.getvalue(),
            cli_text=display.getvalue(),
        )

    async def readback_cli(self, command: str, values: dict[str, str]) -> AgentReadback:
        parsed = parse_admin(
            {"do": "admin", "tool": "cli", "args": command},
            {"guild_id": self.binding.workspace_id, **values},
        )
        await self.authorize(parsed)
        if parsed.operation != "agents_get" or len(parsed.args) != 1 or parsed.options:
            raise ScopeViolation("anthropic-admin", "only literal agent get readback is bound")
        return await self.read_agent(parsed.args[0])

    async def _fork(self, command: AdminCommand) -> AdminReceipt:
        channel = command.channel_id or ""
        source = await self.read_agent(command.options["--copy-from"])
        await channels_rule_set(
            rt=self.runtime,
            console=Console(file=StringIO()),
            platform=self.binding.platform,
            workspace_id=self.binding.workspace_id,
            channel_id=channel,
            readers="own",
            copy_from=source.name,
        )
        async with self.binding.sessions() as session:
            policy = await load_access_policy(session, tenant_id=uuid.UUID(source.tenant_id))
            _, rows = await list_propagations_for_tenant(
                session, tenant_id=uuid.UUID(source.tenant_id)
            )
        copies = [row for row in rows if row.channel_id == channel]
        if len(copies) != 1:
            raise StoreError("fork has no unique independent channel propagation")
        name = copies[0].agent_name
        if name is None:
            raise StoreError("fork propagation has no agent name")
        copied = await self.read_agent(name)
        fresh_source = await self.read_agent(source.name)
        return self.binding.receipt(
            command,
            {
                "source": source.model_dump(mode="json"),
                "copy": copied.model_dump(mode="json"),
                "source_unchanged": source.payload == fresh_source.payload,
                "readers": policy.channel_rules[channel].readers,
            },
        ).model_copy(update={"values": {"fork.name": copied.name, "fork.id": copied.native_id}})

    async def _archive_fork(self, command: AdminCommand) -> AdminReceipt:
        channel = command.channel_id or ""
        async with self.binding.sessions() as session:
            _, rows = await list_propagations_for_tenant(
                session, tenant_id=uuid.UUID(self.binding.scope.tenant_id)
            )
        names = [
            row.agent_name
            for row in rows
            if row.channel_id == channel and row.agent_name is not None
        ]
        archived: list[JsonValue] = []
        for name in names:
            before = await self.read_agent(name)
            metadata = before.payload.get("metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get(MA_METADATA_KEY_CHANNEL_COPY) != channel
            ):
                raise ScopeViolation("anthropic-admin", "owned channel copy marker required")
            await archive_channel_copy(
                self.binding.anthropic,
                self.binding.sessions,
                tenant_id=uuid.UUID(self.binding.scope.tenant_id),
                name=name,
                closing_channel_id=channel,
                subject=Subject(is_admin=True),
                default=self.binding.default,
                expected_ma_agent_id=before.native_id,
            )
            remaining_agent = await find_agent_by_daimon_tag(
                self.binding.anthropic,
                tenant_id=uuid.UUID(self.binding.scope.tenant_id),
                name=name,
            )
            if remaining_agent is not None:
                raise StoreError("archived copy remains in the independent active listing")
            archived.append(before.native_id)
        async with self.binding.sessions() as session:
            _, remaining = await list_propagations_for_tenant(
                session, tenant_id=uuid.UUID(self.binding.scope.tenant_id)
            )
        if any(row.channel_id == channel for row in remaining):
            raise StoreError("channel copy teardown remains in service readback")
        return self.binding.receipt(command, {"archived": archived})

    async def attempt_member_edit(self, command: MemberAgentEdit) -> AdminReceipt:
        """Execute real MCP update with the independently selected expected native id.

        No operator update, missing-identity refusal or policy substitution.
        Before/after revisions and full payloads report mutation even on refusal.
        """
        await self.authorize()
        scope = command.scope
        if (
            self.binding.backend != "anthropic"
            or scope.is_platform
            or scope.is_legacy_host_authorized
            or scope.tenant_id != self.binding.scope.tenant_id
            or not scope.principal_id.strip()
            or not scope.authorization_id.strip()
        ):
            raise ScopeViolation("anthropic-admin", "explicit owned member scope required")
        async with self.binding.sessions() as session:
            identity = await get_account_with_tenant(
                session, account_id=uuid.UUID(scope.account_id)
            )
            principal = await get_principal_by_id(
                session, principal_id=uuid.UUID(scope.principal_id), kind="platform"
            )
        if (
            identity is None
            or not isinstance(principal, PlatformPrincipalRow)
            or str(principal.account_id) != scope.account_id
            or principal.platform != self.binding.platform
            or principal.external_id != identity.platform_user_id
            or str(identity.tenant_id) != scope.tenant_id
            or identity.role != Role.USER
            or identity.is_external
            or identity.platform_user_id is None
        ):
            raise ScopeViolation("anthropic-admin", "real owned local member account required")
        if (
            command.description != "qa probe"
            or command.server_name != "deepwiki"
            or command.server_url != "https://mcp.deepwiki.com/mcp"
            or re.fullmatch(r"qa-unbound-[a-z0-9-]{1,32}", command.name) is None
        ):
            raise ScopeViolation("anthropic-admin", "only the authored D5 edit probe is bound")
        before = await self.read_agent(command.name)
        auth = AuthIdentity(
            account_id=identity.account_id,
            tenant_id=identity.tenant_id,
            role=identity.role,
            platform=identity.platform,
            external_id=identity.external_id,
            platform_user_id=identity.platform_user_id,
            platform_role_ids=identity.platform_role_ids,
        )
        runtime = McpRuntime(
            session_factory=self.binding.sessions,
            client=self.binding.anthropic,
            settings=self.runtime.settings,
            deployment_default=self.binding.default,
        )
        errors: list[JsonValue] = []
        try:
            await _update_agent_impl(
                runtime,
                auth,
                command.name,
                model=None,
                description=command.description,
                system=None,
                tools=None,
                mcp_servers=None,
                skills=None,
                expected_ma_agent_id=before.native_id,
                origin_context_id=None,
            )
        except (ToolError, DaimonError) as refused:
            errors.append({"operation": "update_agent", "error": str(refused)})
        try:
            await _attach_mcp_server_impl(
                runtime,
                auth,
                agent_name=command.name,
                server_name=command.server_name,
                url=command.server_url,
                expected_ma_agent_id=before.native_id,
                origin_context_id=None,
            )
        except (ToolError, DaimonError) as refused:
            errors.append({"operation": "attach_mcp_server", "error": str(refused)})
        after = await self.read_agent(command.name)
        return AdminReceipt(
            status="refused" if errors else "executed",
            backend="anthropic",
            operation="member_agent_edit",
            code="ADMIN_MEMBER_EDIT_REFUSED" if errors else None,
            observations={
                "before": before.model_dump(mode="json"),
                "after": after.model_dump(mode="json"),
                "unchanged": before.payload == after.payload,
                "revision_unchanged": before.version == after.version,
                "errors": errors,
            },
        )
