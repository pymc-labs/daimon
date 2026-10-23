"""The operator CLI's OpenAI path as a catalog admin edge, under an opt-in operator profile.

Frozen catalog admin commands for an OpenAI column run through the real CLI
commands (`daimon channels rule set …`, `daimon agents … get …`), not a direct
core call. The profile is what an operator opts into: a resource-host factory
over the run's OpenAI driver, and a deployment manifest authorizing exactly
the run's channel revision for the run's account. Everything it reports is
labelled with `OPT_IN_OPERATOR_PROFILE`. Any other command goes to the given
fallback edge unchanged. A receipt records an executed operation, never a
scenario verdict.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt, ResourceEdge
from pydantic import BaseModel, ConfigDict, JsonValue

if TYPE_CHECKING:
    from daimon.adapters.cli.runtime import CliRuntime
    from daimon.core.neutral_resources import NeutralResourceHost
    from mux.contracts.ports import ManagedAgents

OPT_IN_OPERATOR_PROFILE = "opt-in-openai-operator"
_DEPLOYMENT_FILE = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
_OPENAI_PROFILE = "openai.persistent_workspace"


class CliCheckReceipt(BaseModel):
    """One read-only CLI command, as run, and what it printed."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    profile: str
    command: str
    output: str
    error: str | None = None


@dataclass
class _HostFactory:
    """The operator's factory: one neutral host over the run's driver, per request."""

    backend: ManagedAgents
    sessions: Any
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
        from daimon.core.neutral_resources import NeutralResourceHost
        from mux.contracts.ids import Scope

        return NeutralResourceHost(
            self.backend,
            self.sessions,
            scope=Scope(
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                principal_id=principal_id,
                authorization_id=authorization_id,
            ),
            account_scope_id=self.account_scope_id,
            operation_key=operation_key,
        )


@dataclass
class CliResourceEdge:
    """The real operator CLI for an OpenAI column's channel copies and agent readback."""

    binding: AdminBindings
    backend: ManagedAgents
    workspace: str
    manifest: Path
    fallback: ResourceEdge | None = None
    checks: list[CliCheckReceipt] = field(default_factory=list[CliCheckReceipt])

    def runtime(self) -> CliRuntime:
        from daimon.adapters.cli.runtime import CliRuntime
        from daimon.core.ma_resolver import new_resolver_cache

        settings = SimpleNamespace(
            cli=SimpleNamespace(local_user="qa-operator"),
            discord=None,  # the copy is named from the channel id; no platform call
            mcp=SimpleNamespace(public_url=None),
            crypto=SimpleNamespace(keys=()),
        )
        return CliRuntime(
            settings=cast(Any, settings),
            anthropic=self.binding.anthropic,
            sessionmaker=self.binding.sessions,
            deployment_default=self.binding.default,
            resolver_cache=new_resolver_cache(),
            resource_hosts=_HostFactory(self.backend, self.binding.sessions, self.workspace),
        )

    async def authorize(self, channel_id: str) -> None:
        """Write the opt-in manifest for exactly this channel's current revision."""
        from daimon.core.channel_backend import channel_ref, current_backend

        tenant_id = uuid.UUID(self.binding.scope.tenant_id)
        async with self.binding.sessions() as session:
            revision = await current_backend(
                session, channel_ref(tenant_id, self.binding.platform, channel_id)
            )
        if revision is None or revision.profile != _OPENAI_PROFILE:
            raise ValueError("the opt-in profile authorizes only an OpenAI-configured channel")
        self.manifest.write_text(
            json.dumps(
                [
                    {
                        "profile": _OPENAI_PROFILE,
                        "tenant_id": str(tenant_id),
                        "platform": self.binding.platform,
                        "channel_id": channel_id,
                        "account_id": self.binding.scope.account_id,
                        "config_digest": revision.digest,
                        "project": self.workspace,
                        "agent_id": "qa-unused-agent",
                        "environment_id": "qa-unused-environment",
                        "api_key_env": "QA_UNSET_OPERATOR_KEY",
                    }
                ]
            )
        )

    async def __call__(self, command: AdminCommand) -> AdminReceipt:
        if (
            self.binding.backend == "openai"
            and command.operation == "channel_rule_set"
            and "--copy-from" in command.options
        ):
            return await self._copy(command)
        if self.fallback is None:
            return AdminReceipt(
                status="pending",
                backend=self.binding.backend,
                operation=command.operation,
                code="ADMIN_RESOURCE_OPERATION_UNBOUND",
            )
        return await self.fallback(command)

    async def _copy(self, command: AdminCommand) -> AdminReceipt:
        from daimon.adapters.cli.commands.channels import channels_rule_set
        from daimon.core.stores.access_policy import load_access_policy
        from rich.console import Console

        await self.binding.authorize_command(command)
        channel = command.channel_id or ""
        await self.authorize(channel)
        console = Console(file=StringIO(), force_terminal=False, highlight=False, width=200)
        await channels_rule_set(
            rt=self.runtime(),
            console=console,
            platform=self.binding.platform,
            workspace_id=self.binding.workspace_id,
            channel_id=channel,
            readers=command.options.get("--readers"),
            writers=command.options.get("--writers"),
            copy_from=command.options["--copy-from"],
            discord_transport=None,
        )
        tenant_id = uuid.UUID(self.binding.scope.tenant_id)
        async with self.binding.sessions() as session:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        names = [name for name, rule in policy.agent_rules.items() if rule.runs_in == (channel,)]
        values: dict[str, str] = {}
        observations: dict[str, JsonValue] = {
            "profile": OPT_IN_OPERATOR_PROFILE,
            "output": cast(StringIO, console.file).getvalue(),
        }
        if len(names) == 1:
            values["fork.name"] = names[0]
        receipt = self.binding.receipt(command, observations)
        return receipt.model_copy(update={"values": values})

    async def cli_check(self, command: str) -> CliCheckReceipt:
        """Run one frozen read-only `daimon agents --guild G get NAME` through the real CLI."""
        from daimon.adapters.cli.commands.agents import agents_get
        from daimon.adapters.cli.tenant import TenantSelector
        from daimon.core.errors import DaimonError
        from rich.console import Console

        words = shlex.split(command)
        if len(words) != 6 or words[:3] != ["daimon", "agents", "--guild"] or words[4] != "get":
            raise ValueError("cli_check runs only `daimon agents --guild G get NAME`")
        if words[3] != self.binding.workspace_id:
            raise ValueError("cli_check reads only the run's own workspace")
        if self.binding.backend != "openai":
            raise ValueError("this edge reads back only an OpenAI column's agents")
        # The same owned-database and SDK-environment preflight as the copy path.
        await self.binding.authorize_command(
            AdminCommand(
                "agents_get", self.binding.platform, self.binding.workspace_id, args=(words[5],)
            )
        )
        console = Console(file=StringIO(), force_terminal=False, highlight=False, width=200)
        error = None
        try:
            await agents_get(
                rt=self.runtime(),
                console=console,
                name=words[5],
                as_json=False,
                selector=TenantSelector(guild_id=words[3]),
            )
        except DaimonError as exc:
            error = type(exc).__name__
        receipt = CliCheckReceipt(
            profile=OPT_IN_OPERATOR_PROFILE,
            command=command,
            output=cast(StringIO, console.file).getvalue(),
            error=error,
        )
        self.checks.append(receipt)
        return receipt


def cli_check_holds(
    assertion: Mapping[str, JsonValue], receipt: CliCheckReceipt, values: Mapping[str, object]
) -> bool:
    """The catalog's `cli_check`: `expect`/`expect_absent` patterns, `expect_all_of` items.

    `values` supplies the placeholders the assertion names (a list for
    `expect_all_of`); an unbound placeholder raises rather than passing.
    """
    if receipt.error is not None:
        return False

    def bound(text: object) -> object:
        key = str(text)
        match = re.fullmatch(r"\{([a-z][a-z0-9_.]*)\}", key)
        if match is None:
            return key
        if match.group(1) not in values:
            raise ValueError(f"unbound placeholder {key}")
        return values[match.group(1)]

    out = receipt.output
    if "expect" in assertion and not re.search(str(bound(assertion["expect"])), out):
        return False
    if "expect_absent" in assertion and re.search(str(bound(assertion["expect_absent"])), out):
        return False
    if "expect_all_of" in assertion:
        items = bound(assertion["expect_all_of"])
        wanted = (
            [str(i) for i in cast(list[object], items)] if isinstance(items, list) else [str(items)]
        )
        if not all(item in out for item in wanted):
            return False
    return True


@contextmanager
def _manifest_env(path: Path) -> Iterator[None]:
    previous = os.environ.get(_DEPLOYMENT_FILE)
    os.environ[_DEPLOYMENT_FILE] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_DEPLOYMENT_FILE, None)
        else:
            os.environ[_DEPLOYMENT_FILE] = previous


@asynccontextmanager
async def opt_in_operator_profile(
    binding: AdminBindings,
    backend: ManagedAgents,
    *,
    workspace: str,
    root: Path,
    fallback: ResourceEdge | None = None,
) -> AsyncIterator[CliResourceEdge]:
    """Opt the run into the operator's OpenAI capability for its duration.

    Installs the CLI edge as the binding's resource edge and points the
    deployment manifest at this run's own file; both are restored on exit.
    """
    edge = CliResourceEdge(
        binding, backend, workspace, root / "operator-deployments.json", fallback=fallback
    )
    previous = binding.resource_edge
    binding.resource_edge = edge
    try:
        with _manifest_env(edge.manifest):
            yield edge
    finally:
        binding.resource_edge = previous
