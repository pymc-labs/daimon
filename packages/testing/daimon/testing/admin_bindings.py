"""Offline admin steps over product services and a disposable test database.

Runner dispatch, deadlines and outcome credit belong to the runner. A receipt
records an executed setup operation, never a scenario PASS. Unsupported edges
remain typed pending before mutation; commands are parsed, never run in a shell.
"""

from __future__ import annotations

import os
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

import httpx
from anthropic import AsyncAnthropic
from daimon.core.authz import Subject
from daimon.core.channel_admins import normalize_channel_admin_ids
from daimon.core.channel_backend import current_backend, set_channel_backend
from daimon.core.channel_budget import parse_budget_spec
from daimon.core.channel_rules import set_channel_rule
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import (
    accounts,
    channel_admins,
    channel_budgets,
    routines,
    tenant_ledger,
    tenants,
)
from daimon.core.stores.access_policy import load_access_policy
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.isolated_sdk import build_async_anthropic
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ChannelRef, Provider, Scope
from mux.errors import ScopeViolation
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROFILES: dict[Provider, str] = {
    "anthropic": "anthropic.managed_agents",
    "openai": "openai.persistent_workspace",
    "gemini": "gemini.inline_reuse",
}
MODELS: dict[Provider, str | None] = {
    "anthropic": None,
    "openai": "gpt-6-luna",
    "gemini": "gemini-3.8-flash",
}
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z0-9_]+|:[A-Za-z0-9_]+)*)\}")


def _require_isolated_sdk_environment() -> None:
    # Presence only: do not read, log or erase operator values. Include future
    # Anthropic selectors rather than guessing which SDK release consumes them.
    if any(
        name.startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "GENAI_"))
        for name in os.environ
    ):
        raise ValueError("offline admin bindings refuse ambient provider SDK configuration")


class AdminReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["executed", "refused", "pending"]
    backend: Provider
    operation: str
    code: str | None = None
    observations: dict[str, JsonValue] = {}
    # Explicit fixture values derived from real API results (e.g. fork.name).
    values: dict[str, str] = {}


@dataclass(frozen=True)
class AdminCommand:
    operation: str
    platform: str
    workspace_id: str
    channel_id: str | None = None
    args: tuple[str, ...] = ()
    options: Mapping[str, str] = field(default_factory=dict[str, str])


ResourceEdge = Callable[[AdminCommand], Awaitable[AdminReceipt]]


class UnboundAdmin(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _expand(value: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise UnboundAdmin("ADMIN_PLACEHOLDER_UNBOUND")
        return values[key]

    return PLACEHOLDER.sub(replace, value)


def _flags(tokens: list[str], allowed: set[str]) -> tuple[tuple[str, ...], dict[str, str]]:
    args: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            if token not in allowed or token in options:
                raise UnboundAdmin("ADMIN_UNKNOWN_OR_REPEATED_OPTION")
            if token == "--yes":
                options[token] = "true"
            else:
                index += 1
                if index >= len(tokens) or tokens[index].startswith("--"):
                    raise UnboundAdmin("ADMIN_OPTION_VALUE_UNBOUND")
                options[token] = tokens[index]
        else:
            args.append(token)
        index += 1
    return tuple(args), options


def parse_admin(source: Mapping[str, Any], values: Mapping[str, str]) -> AdminCommand:
    """Closed translations of frozen source operations; no guessed CLI syntax."""
    if source.get("do") != "admin" or set(source) - {
        "do",
        "tool",
        "args",
        "allow_fail",
        "allow_fail_pattern",
        "allow_fail_exit_codes",
    }:
        raise UnboundAdmin("ADMIN_SOURCE_UNSUPPORTED")
    tool, raw = source.get("tool"), source.get("args")
    if not isinstance(tool, str):
        raise UnboundAdmin("ADMIN_TOOL_UNBOUND")
    if tool in {"archive_channel_forks", "delete_routines"}:
        if (
            not isinstance(raw, dict)
            or set(cast(dict[str, object], raw)) != {"channel"}
            or not isinstance(raw["channel"], str)
        ):
            raise UnboundAdmin("ADMIN_ARGUMENTS_UNBOUND")
        return AdminCommand(
            str(tool),
            values.get("platform", "discord"),
            values.get("guild_id", ""),
            _expand(raw["channel"], values),
        )
    if tool != "cli" or not isinstance(raw, str):
        raise UnboundAdmin(
            "ADMIN_PLATFORM_EDGE_UNBOUND" if tool == "discord_overwrite" else "ADMIN_TOOL_UNBOUND"
        )
    # Expand after tokenization: fixture data cannot introduce flags or commands.
    try:
        words = [_expand(word, values) for word in shlex.split(raw)]
    except ValueError as error:
        if isinstance(error, UnboundAdmin):
            raise
        raise UnboundAdmin("ADMIN_CLI_SYNTAX_UNBOUND") from error
    if words[:2] == ["daimon", "channels"] and len(words) >= 7:
        area, verb, platform, workspace, channel = words[2:7]
        allowed: set[str] | None = {
            "rule": {"--readers", "--writers", "--copy-from"},
            "admins": {"--user"},
            "budget": {"--window"},
            "skills": set[str](),
        }.get(area)
        if allowed is None:
            raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")
        args, options = _flags(words[7:], allowed)
        operation = f"channel_{area}_{verb}"
        shapes: dict[str, tuple[int, set[str]]] = {
            "channel_rule_set": (0, {"--readers", "--writers", "--copy-from"}),
            "channel_admins_set": (0, {"--user"}),
            "channel_budget_set": (1, {"--window"}),
            "channel_budget_clear": (0, set[str]()),
            "channel_skills_add": (1, set[str]()),
            "channel_skills_remove": (1, set[str]()),
        }
        shape = shapes.get(operation)
        if shape is None or len(args) != shape[0] or set(options) - shape[1]:
            raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")
        return AdminCommand(operation, platform, workspace, channel, args, options)
    if words[:2] == ["daimon", "tenants"] and len(words) >= 6:
        verb, platform, workspace = words[2:5]
        args, options = _flags(words[5:], {"--note", "--id"} if verb == "credit" else set())
        if verb not in {"turn-cap", "credit"} or len(args) != 1:
            raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")
        return AdminCommand(f"tenant_{verb}", platform, workspace, args=args, options=options)
    if words[:2] in (["daimon", "skills"], ["daimon", "agents"]) and len(words) >= 6:
        if words[2] != "--guild":
            raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")
        args, options = _flags(words[5:], {"--agent", "--yes"})
        if len(args) != 1 or words[4] not in (
            {"create", "archive", "get"} if words[1] == "agents" else {"add", "delete"}
        ):
            raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")
        return AdminCommand(
            f"{words[1]}_{words[4]}", "discord", words[3], args=args, options=options
        )
    raise UnboundAdmin("ADMIN_CLI_OPERATION_UNBOUND")


@dataclass
class AdminBindings:
    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    backend: Provider
    platform: str
    workspace_id: str
    # Only channels explicitly created/claimed by this disposable run.
    channels: frozenset[str]
    trace: ScriptedTransport
    anthropic: AsyncAnthropic = field(init=False, repr=False)
    default: DeploymentDefault = field(default_factory=DeploymentDefault)
    resource_edge: ResourceEdge | None = None

    def __post_init__(self) -> None:
        _require_isolated_sdk_environment()
        self.anthropic = build_async_anthropic(
            api_key="offline-fixture",
            base_url="https://anthropic.invalid",
            transport=httpx.MockTransport(self.trace.dispatch),
        )

    async def close(self) -> None:
        await self.anthropic.close()

    async def authorize_command(self, command: AdminCommand) -> None:
        """Recheck a resource callback against the owned lane DB and destinations."""
        await self._authorize(command)

    async def _authorize(self, command: AdminCommand | None = None) -> None:
        _require_isolated_sdk_environment()
        require_owned_sessionmaker(self.sessions)
        if (
            self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or not (self.scope.principal_id.strip() and self.scope.authorization_id.strip())
        ):
            raise ScopeViolation("qa-admin", "explicit tenant authorization required")
        tenant_id, account_id = uuid.UUID(self.scope.tenant_id), uuid.UUID(self.scope.account_id)
        async with self.sessions() as session:
            tenant = await tenants.get_tenant(session, tenant_id)
            account = await accounts.get_account(session, account_id)
        if (
            tenant is None
            or account is None
            or account.tenant_id != tenant_id
            or (tenant.platform != self.platform or tenant.external_id != self.workspace_id)
        ):
            raise ScopeViolation("qa-admin", "run account or tenant does not match")
        if command is not None and (
            command.platform != self.platform
            or command.workspace_id != self.workspace_id
            or command.channel_id is not None
            and command.channel_id not in self.channels
        ):
            raise ScopeViolation("qa-admin", "operation is outside the owned run")

    async def configure_channel(self, channel_id: str) -> AdminReceipt:
        """Persist the selected provider through the product config API."""
        command = AdminCommand("channel_backend_set", self.platform, self.workspace_id, channel_id)
        await self._authorize(command)
        channel = ChannelRef(
            tenant_id=self.scope.tenant_id, platform=self.platform, channel_id=channel_id
        )
        async with self.sessions() as session, session.begin():
            written = await set_channel_backend(
                session,
                channel,
                BackendConfig(
                    backend=self.backend,
                    profile=PROFILES[self.backend],
                    model=MODELS[self.backend],
                ),
            )
        async with self.sessions() as session:
            read = await current_backend(session, channel)
        if read != written:
            raise RuntimeError("admin backend readback differs from product write")
        return self.receipt(command, {"profile": written.profile, "revision": written.local})

    def receipt(self, command: AdminCommand, observations: dict[str, JsonValue]) -> AdminReceipt:
        return AdminReceipt(
            status="executed",
            backend=self.backend,
            operation=command.operation,
            observations=observations,
        )

    async def execute(self, source: Mapping[str, Any], values: Mapping[str, str]) -> AdminReceipt:
        try:
            return await self._execute(source, values)
        except UnboundAdmin as error:
            return AdminReceipt(
                status="pending", backend=self.backend, operation="admin", code=error.code
            )

    async def _execute(self, source: Mapping[str, Any], values: Mapping[str, str]) -> AdminReceipt:
        await self._authorize()
        try:
            command = parse_admin(
                source, {"platform": self.platform, "guild_id": self.workspace_id, **values}
            )
        except UnboundAdmin as error:
            return AdminReceipt(
                status="pending", backend=self.backend, operation="admin", code=error.code
            )
        await self._authorize(command)
        tenant_id, actor = uuid.UUID(self.scope.tenant_id), uuid.UUID(self.scope.account_id)
        channel_id = command.channel_id or ""
        if command.operation == "channel_admins_set":
            user = command.options.get("--user")
            if user is None:
                raise UnboundAdmin("ADMIN_GRANT_USER_UNBOUND")
            channel, roles, users = normalize_channel_admin_ids(
                self.platform, channel_id=channel_id, role_ids=(), user_ids=(user,)
            )
            async with self.sessions() as session, session.begin():
                await channel_admins.set_channel_admins(
                    session,
                    tenant_id=tenant_id,
                    platform=self.platform,
                    channel_id=channel,
                    role_ids=roles,
                    user_ids=users,
                    actor_account_id=actor,
                )
            async with self.sessions() as session:
                row = await channel_admins.get_channel_admins(
                    session, tenant_id=tenant_id, platform=self.platform, channel_id=channel
                )
            if row is None or row.user_ids != tuple(sorted(users)):
                raise RuntimeError("admin grant readback differs")
            return self.receipt(command, row.model_dump(mode="json"))
        if command.operation == "channel_rule_set" and "--copy-from" not in command.options:
            readers, writers = command.options.get("--readers"), command.options.get("--writers")
            from daimon.core.channel_rules import as_readers, as_writers

            if (
                (readers is not None and as_readers(readers) is None)
                or (writers is not None and as_writers(writers) is None)
                or readers is writers is None
            ):
                raise UnboundAdmin("ADMIN_RULE_VALUE_UNBOUND")
            if readers == "own" or writers == "own":
                return await self._resource(command)
            change = await set_channel_rule(
                self.anthropic,
                self.sessions,
                tenant_id=tenant_id,
                platform=self.platform,
                channel_id=channel_id,
                readers=as_readers(readers),
                writers=as_writers(writers),
                subject=Subject(is_admin=True),
                default=self.default,
                actor_account_id=actor,
            )
            async with self.sessions() as session:
                policy = await load_access_policy(session, tenant_id=tenant_id)
            if policy.channel_rules.get(channel_id, type(change.rule)()) != change.rule:
                raise RuntimeError("admin channel rule readback differs")
            return self.receipt(
                command,
                {
                    "readers": change.rule.readers,
                    "writers": change.rule.writers,
                    "changed": change.changed,
                },
            )
        if command.operation.startswith("channel_budget_"):
            # These source ids name new parent channels; no guessed thread remap.
            written = None
            async with self.sessions() as session, session.begin():
                if command.operation == "channel_budget_set":
                    spec = parse_budget_spec(
                        limit_usd=command.args[0], window=command.options.get("--window", "monthly")
                    )
                    written = await channel_budgets.set_channel_budget(
                        session,
                        tenant_id=tenant_id,
                        platform=self.platform,
                        channel_id=channel_id,
                        limit_usd=spec.limit_usd,
                        window=spec.window,
                        starts_at=spec.starts_at,
                        ends_at=spec.ends_at,
                        set_by_account_id=actor,
                    )
                else:
                    await channel_budgets.delete_channel_budget(
                        session, tenant_id=tenant_id, platform=self.platform, channel_id=channel_id
                    )
            async with self.sessions() as session:
                budget = await channel_budgets.get_channel_budget(
                    session, tenant_id=tenant_id, platform=self.platform, channel_id=channel_id
                )
            if budget != written:
                raise RuntimeError("admin channel budget readback differs")
            return self.receipt(
                command, {"budget": budget.model_dump(mode="json") if budget else None}
            )
        if command.operation == "tenant_turn-cap":
            value = command.args[0]
            if value != "default" and (
                not value.isdigit() or int(value) < 1 or str(int(value)) != value
            ):
                raise UnboundAdmin("ADMIN_CAP_VALUE_UNBOUND")
            cap = None if value == "default" else int(value)
            async with self.sessions() as session, session.begin():
                await tenants.set_turn_cap(session, tenant_id=tenant_id, cap=cap)
            async with self.sessions() as session:
                read = await tenants.get_tenant(session, tenant_id)
            if read is None or read.turn_cap != cap:
                raise RuntimeError("admin tenant cap readback differs")
            return self.receipt(command, {"turn_cap": read.turn_cap})
        if command.operation == "tenant_credit":
            # Match tenants_credit's literal positive amount, note slug and
            # idempotency identity. Frozen -<balance> cannot mean a ledger debit.
            try:
                amount = Decimal(command.args[0])
            except (InvalidOperation, ValueError):
                amount = Decimal("NaN")
            exponent = amount.as_tuple().exponent
            note = command.options.get("--note", "")
            note_slug = re.sub(r"[_\W]+", "-", note.strip().lower()).strip("-")[:64].rstrip("-")
            if (
                not amount.is_finite()
                or amount <= 0
                or isinstance(exponent, int)
                and exponent < -2
                or not note_slug
            ):
                return AdminReceipt(
                    status="pending",
                    backend=self.backend,
                    operation=command.operation,
                    code="ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE",
                )
            request_id = command.options.get("--id") or str(uuid.uuid4())
            key = f"manual:credit:{tenant_id}:{note_slug}:usd{amount:.2f}:{request_id}"
            async with self.sessions() as session, session.begin():
                inserted = await tenant_ledger.insert_entry(
                    session,
                    tenant_id=tenant_id,
                    delta_usd=amount,
                    reason="manual_credit",
                    idempotency_key=key,
                )
            async with self.sessions() as session:
                rows = await tenant_ledger.list_for_tenant(session, tenant_id=tenant_id)
                balance = await tenant_ledger.get_balance(session, tenant_id=tenant_id)
            matching = [row for row in rows if row.idempotency_key == key]
            if (
                len(matching) != 1
                or matching[0].tenant_id != tenant_id
                or matching[0].delta_usd != amount
                or matching[0].reason != "manual_credit"
                or matching[0].channel_id is not None
                or matching[0].payment_event_id is not None
                or matching[0].payment_intent is not None
                or balance != sum((row.delta_usd for row in rows), Decimal("0"))
            ):
                raise RuntimeError("admin tenant credit readback differs")
            return self.receipt(
                command,
                {
                    "credit_id": request_id,
                    "inserted": inserted,
                    "ledger_entry": matching[0].model_dump(mode="json"),
                    "balance_usd": str(balance),
                },
            )
        if command.operation == "delete_routines":
            async with self.sessions() as session, session.begin():
                rows = await routines.list_routines_for_tenant(session, tenant_id=tenant_id)
                ids = [row.id for row in rows if row.channel_id == channel_id]
                for id_ in ids:
                    await routines.delete_routine(session, id_, tenant_id=tenant_id)
            async with self.sessions() as session:
                remaining = await routines.list_routines_for_tenant(session, tenant_id=tenant_id)
            if any(row.channel_id == channel_id for row in remaining):
                raise RuntimeError("admin routine teardown readback differs")
            return self.receipt(command, {"deleted": [str(id_) for id_ in ids]})
        return await self._resource(command)

    async def _resource(self, command: AdminCommand) -> AdminReceipt:
        if self.resource_edge is None:
            return AdminReceipt(
                status="pending",
                backend=self.backend,
                operation=command.operation,
                code="ADMIN_RESOURCE_EDGE_UNBOUND",
            )
        result = await self.resource_edge(command)
        if result.backend != self.backend or result.operation != command.operation:
            raise ValueError("admin resource receipt differs from bound operation")
        return result


async def provision_admin_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    backend: Provider,
    channels: frozenset[str],
    trace: ScriptedTransport,
) -> AdminBindings:
    """Actual product provisioning; no ORM/model/fabricated state insertion."""
    _require_isolated_sdk_environment()
    require_owned_sessionmaker(sessions)
    provisioned = await provision_tenant(sessions, platform="discord", workspace_id=workspace_id)
    return AdminBindings(
        sessions=sessions,
        scope=Scope(
            tenant_id=str(provisioned.tenant_id),
            account_id=str(provisioned.account_id),
            principal_id=f"qa-admin:{workspace_id}",
            authorization_id=f"qa-offline:{workspace_id}",
        ),
        backend=backend,
        platform="discord",
        workspace_id=workspace_id,
        channels=channels,
        trace=trace,
    )
