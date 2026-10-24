"""QA-D14 and QA-D5 in the Gemini column, through the real operator CLI, under the opt-in profile.

The frozen scenario operations (the target53 native pack) run through
N6's admin bindings. Under `opt_in_gemini_operator_profile`, the channel copy runs the
real `daimon channels rule set` CLI command (the granted Gemini path) and each frozen
`cli_check` runs the real `daimon agents … get`. Everything else stays on N6's
resource edge. The fake provider is an offline stand-in for the Gemini agents
API; every result here is labelled "under opt-in profile".
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core.agent_identity import cancel_pending_agent_faces
from daimon.core.defaults.metadata import build_metadata
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.testing.admin_bindings import AdminBindings, provision_admin_run
from daimon.testing.admin_resources import ResourceAdminBindings
from daimon.testing.cli_resources import cli_check_holds
from daimon.testing.gemini_cli_resources import (
    GEMINI_OPERATOR_PROFILE as OPT_IN_OPERATOR_PROFILE,
)
from daimon.testing.gemini_cli_resources import (
    GeminiCliResourceEdge as CliResourceEdge,
)
from daimon.testing.gemini_cli_resources import (
    opt_in_gemini_operator_profile as opt_in_operator_profile,
)
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]

NATIVE = Path(__file__).resolve().parents[1] / "fixtures" / "target53" / "native"
WORKSPACE, CHANNEL = "123456789012345678", "223456789012345678"
DATA = bundle_from_markdown(
    "---\nname: qa-echo-skill\ndescription: Offline echo\n---\nEcho the requested text.\n"
)


def _scenario(scenario_id: str) -> dict[str, Any]:
    return json.loads((NATIVE / f"{scenario_id}.json").read_text())["scenario"]


class _GeminiServer:
    """Actual google-genai native agents HTTP boundary, never a model/turn stub."""

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.router = MARouter()
        self.router.add("POST", r"/v1beta/agents", self.create)
        self.router.add("GET", r"/v1beta/agents/(?P<id>[^/]+)", self.get)

    def create(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = json.loads(request.content)
        self.agents[raw["id"]] = raw
        return httpx.Response(200, json=raw)

    def get(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=self.agents[match["id"]])


class _Run:
    def __init__(
        self,
        binding: AdminBindings,
        resources: ResourceAdminBindings,
        driver: GeminiManagedAgents,
        trace: ScriptedTransport,
    ) -> None:
        self.binding, self.resources, self.driver, self.trace = binding, resources, driver, trace

    def values(self, **extra: str) -> dict[str, str]:
        return {
            "guild_id": WORKSPACE,
            "channel_id": CHANNEL,
            "env.qa_base_with_own_skill": "daimon",
            **extra,
        }


@pytest.fixture
async def run(
    db_preflight: object, db_session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[_Run]:
    """A Gemini column run: provisioned tenant, the `daimon` agent with its own skill."""
    binding = await provision_admin_run(
        db_session_factory,
        workspace_id=WORKSPACE,
        backend="gemini",
        channels=frozenset({CHANNEL}),
        trace=ScriptedTransport(),
    )
    binding.default = DeploymentDefault(agent_name="daimon")
    trace = ScriptedTransport(router=_GeminiServer().router)
    client = build_gemini_client(transport=httpx.MockTransport(trace.dispatch))
    driver = GeminiManagedAgents(
        FakeTransport(),
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id="project",
        resource_catalog_enabled=True,
        resource_catalog_transport=SDKAgentCatalogTransport(client),
    )
    tenant, account = binding.scope.tenant_id, binding.scope.account_id
    resources = ResourceAdminBindings(
        binding,
        backend=driver,
        resource_catalog_enabled=True,
        catalog_adapter=driver.catalog_adapter,
        provider_workspace="project",
        operation_key="qa:resource",
        bundles={"fixtures/SKILL.md": DATA},
        channel_labels={CHANNEL: "qa-room"},
    )
    binding.resource_edge = resources
    assert resources.host is not None
    await resources.host.publish(
        "daimon",
        AgentSpec(
            name="daimon",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            system="Help offline.",
            tools=(ToolSpec(name="code_execution", kind="builtin"),),
            skills=(),
            metadata=build_metadata(
                tenant_id=uuid.UUID(tenant), account_id=uuid.UUID(account), name="daimon"
            ),
        ),
        expected_revision=0,
        key="qa:seed",
    )
    await binding.configure_channel(CHANNEL)
    try:
        yield _Run(binding, resources, driver, trace)
    finally:
        # The real CLI may reach its face-render deadline while the task still
        # holds the owned tenant row lock. Await cancellation before DB cleanup.
        faces = [task for task in asyncio.all_tasks() if task.get_name() == "agent-face-generation"]
        cancel_pending_agent_faces()
        await asyncio.gather(*faces, return_exceptions=True)
        await binding.close()
        await client.aio.aclose()
        client.close()


async def test_d14_gemini_under_the_opt_in_profile(run: _Run, tmp_path: Path) -> None:
    scenario = _scenario("QA-D14-FORK-KEEPS-SKILLS")
    # The base agent's own skill, through N6's resource edge (as the column's setup does).
    added = await run.binding.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon skills --guild {guild_id} add fixtures/SKILL.md --agent daimon --yes",
        },
        run.values(),
    )
    assert added.status == "executed"
    assert run.resources.host is not None
    base = await run.resources.host.find("daimon")
    assert base is not None and base.revision.agent.spec.skills

    async with opt_in_operator_profile(
        run.binding,
        run.driver,
        workspace="project",
        root=tmp_path,
        fallback=run.resources,
    ) as edge:
        fork = await run.binding.execute(scenario["steps"][1], run.values())
        # Real CLI execution, under the opt-in profile.
        assert fork.status == "executed"
        assert fork.observations["profile"] == OPT_IN_OPERATOR_PROFILE
        assert "a copy of daimon" in str(fork.observations["output"])
        name = fork.values["fork.name"]

        # Independent readback: the copy is in the catalog, with the base's skill copied.
        copy = await run.resources.host.find(name)
        assert copy is not None and copy.revision.agent.spec.skills
        assert copy.revision.agent.spec.model.id == "gemini-3.8-flash"

        # The frozen cli_check assertions, through the real `daimon agents … get`.
        values: dict[str, object] = {
            "env.qa_base_with_own_skill.skills": ["qa-echo-skill"],
            "env.qa_base_with_own_skill.model": "gemini-3.8-flash",
        }
        verdicts: list[bool] = []
        for assertion in scenario["assert"][:2]:
            command = assertion["cmd"].replace("{guild_id}", WORKSPACE).replace("{fork.name}", name)
            receipt = await edge.cli_check(command)
            assert receipt.error is None and copy.id in receipt.output, "read back by the CLI"
            verdicts.append(cli_check_holds(assertion, receipt, values))
        # Under the opt-in profile the CLI's table readback (name, id, description,
        # created_at, as on Anthropic) shows neither the skill nor the model: both
        # frozen checks FAIL, although the catalog readback above holds both.
        assert verdicts == [False, False]

        teardown = await run.binding.execute(scenario["teardown"][0], run.values())
    # The native copy's archive is N6's typed pending edge: the cell cannot complete.
    assert (teardown.status, teardown.code) == ("pending", "ADMIN_GEMINI_AGENT_DELETE_UNSUPPORTED")
    assert isinstance(edge, CliResourceEdge) and len(edge.checks) == 2
    source = await run.resources.host.find("daimon")
    assert source is not None
    independent = await run.resources.host.read(copy.revision.catalog_id)
    source_pins = source.revision.agent.spec.skills or ()
    copied_pins = independent.agent.spec.skills or ()
    assert source_pins and copied_pins
    assert len(source_pins) == len(copied_pins) == 1
    assert source_pins[0].id != copied_pins[0].id
    assert (await run.resources.host.retained(source_pins[0])).bundle == (
        await run.resources.host.retained(copied_pins[0])
    ).bundle
    (tmp_path / "D14-gemini-result.json").write_text(
        json.dumps(
            {
                "profile": OPT_IN_OPERATOR_PROFILE,
                "admin": fork.model_dump(mode="json"),
                "cli_checks": [r.model_dump(mode="json") for r in edge.checks],
                "frozen_cli_assertions": verdicts,
                "result": "FAIL",
                "whole_cell": "FAIL",
                "remaining": ["native cleanup", "authored log capture", "global assertions"],
                "cleanup": teardown.model_dump(mode="json"),
                "native_readback": asdict(independent),
                "sdk_requests": [r.to_dict() for r in run.trace.requests],
            },
            default=str,
        )
    )


async def test_d5_gemini_create_stays_pending_under_the_opt_in_profile(
    run: _Run, tmp_path: Path
) -> None:
    scenario = _scenario("QA-D5-UNBOUND-AGENT-EDIT-REFUSED")
    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ):
        created = await run.binding.execute(scenario["setup"][0], run.values(nonce="qa"))
    assert run.resources.host is not None
    assert await run.resources.host.find("qa-unbound-qa") is None
    (tmp_path / "D5-gemini-result.json").write_text(
        json.dumps(
            {
                "profile": OPT_IN_OPERATOR_PROFILE,
                "result": "PENDING",
                "setup": created.model_dump(mode="json"),
                "readback": "no authored unbound agent exists; no member edit or turn executed",
            }
        )
    )
    # No YAML → neutral spec edge exists, and the fixture's Claude model is never translated.
    assert (created.status, created.code) == ("pending", "ADMIN_AGENT_SPEC_FIXTURE_UNBOUND")


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "account_id",
        "principal_id",
        "authorization_id",
        "platform_reason",
        "legacy_call_site",
        "workspace",
        "disabled_adapter",
        "foreign_port",
    ],
)
async def test_gemini_returned_host_is_validated_before_native_io(
    run: _Run,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from dataclasses import replace

    from daimon.adapters.cli.runtime import ResourceHostMismatch, request_gemini_resource_host
    from daimon.core.neutral_resources import NeutralResourceHost
    from mux.contracts.ids import Scope

    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ) as edge:
        await edge.authorize(CHANNEL)
        rt = edge.runtime()
        factory = rt.gemini_resource_hosts
        assert factory is not None
        host = factory(
            tenant_id=uuid.UUID(run.binding.scope.tenant_id),
            account_id=uuid.UUID(run.binding.scope.account_id),
            principal_id="cli:qa-operator",
            authorization_id="cli-deployment-operator",
            operation_key="negative",
        )
        if field == "workspace":
            host.workspace = "foreign"
        elif field == "disabled_adapter":
            assert run.driver.catalog_adapter is not None
            monkeypatch.setattr(run.driver.catalog_adapter, "enabled", False)
        elif field == "foreign_port":
            monkeypatch.setattr(run.driver, "agents", object())
        else:
            scope_data = host.scope.model_dump()
            scope_data[field] = (
                str(uuid.uuid4()) if field in ("tenant_id", "account_id") else "foreign"
            )
            host.scope = Scope.model_validate(scope_data)

        class ReturnedHost:
            account_scope_id = "project"

            def __call__(self, **kwargs: Any) -> NeutralResourceHost:
                return host

        before = len(run.trace.requests)
        hostile = replace(rt, gemini_resource_hosts=ReturnedHost())
        with pytest.raises(ResourceHostMismatch):
            request_gemini_resource_host(hostile, edge.grants[0])
        assert len(run.trace.requests) == before


@pytest.mark.parametrize("read", [False, True])
async def test_gemini_stale_grant_refuses_cli_before_native_io(
    run: _Run, tmp_path: Path, read: bool
) -> None:
    from dataclasses import replace
    from io import StringIO

    from click.exceptions import Exit
    from daimon.adapters.cli.commands.agents import agents_get
    from daimon.adapters.cli.commands.channels import channels_rule_set
    from daimon.adapters.cli.runtime import ResourceHostMismatch
    from daimon.adapters.cli.tenant import TenantSelector
    from rich.console import Console

    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ) as edge:
        await edge.authorize(CHANNEL)
        stale = replace(edge.grants[0], config_digest="0" * 64)
        rt = replace(edge.runtime(), gemini_resource_grants=(stale,))
        before = len(run.trace.requests)
        if read:
            with pytest.raises(ResourceHostMismatch, match="stale"):
                await agents_get(
                    rt=rt,
                    console=Console(file=StringIO()),
                    name="daimon",
                    as_json=False,
                    selector=TenantSelector(guild_id=WORKSPACE),
                )
        else:
            with pytest.raises(Exit):
                await channels_rule_set(
                    rt=rt,
                    console=Console(file=StringIO()),
                    platform="discord",
                    workspace_id=WORKSPACE,
                    channel_id=CHANNEL,
                    readers="own",
                    writers=None,
                    copy_from="daimon",
                    discord_transport=None,
                )
        assert len(run.trace.requests) == before


async def test_gemini_cli_readback_refuses_actual_native_drift(run: _Run, tmp_path: Path) -> None:
    from mux.errors import ProviderError

    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ) as edge:
        await edge.authorize(CHANNEL)
        assert run.resources.host is not None
        source = await run.resources.host.find("daimon")
        assert source is not None
        # The actual native HTTP GET returns changed data after the stored CAS row.
        server = run.trace.router
        assert server is not None
        server.add(
            "GET",
            r"/v1beta/agents/(?P<id>[^/]+)",
            lambda request, match: httpx.Response(
                200,
                json={
                    "id": match["id"],
                    "model": "gemini-3.8-flash",
                    "tools": [],
                    "system_prompt": "native drift",
                },
            ),
        )
        # MARouter matches the first route; replace only the offline native response.
        server.routes.insert(0, server.routes.pop())
        with pytest.raises(ProviderError):
            await edge.cli_check(f"daimon agents --guild {WORKSPACE} get daimon")


@pytest.mark.parametrize(
    "field", ["tenant_id", "channel_id", "workspace", "account_id", "ambiguous"]
)
async def test_gemini_unbound_grant_never_copies_on_another_backend(
    run: _Run,
    tmp_path: Path,
    field: str,
) -> None:
    from dataclasses import replace
    from io import StringIO

    from click.exceptions import Exit
    from daimon.adapters.cli.commands.channels import channels_rule_set
    from mux.errors import ScopeViolation
    from rich.console import Console

    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ) as edge:
        await edge.authorize(CHANNEL)
        grant = edge.grants[0]
        changes: dict[str, Any] = {}
        if field in ("tenant_id", "account_id"):
            changes[field] = uuid.uuid4()
        elif field == "channel_id":
            changes[field] = "foreign-channel"
        elif field == "workspace":
            changes[field] = "foreign-workspace"
        hostile_grants = (replace(grant, **changes),) if field != "ambiguous" else (grant, grant)
        rt = replace(edge.runtime(), gemini_resource_grants=hostile_grants)
        native_before = len(run.trace.requests)
        anthropic_before = len(run.binding.trace.requests)
        with pytest.raises(ScopeViolation if field == "account_id" else Exit):
            await channels_rule_set(
                rt=rt,
                console=Console(file=StringIO()),
                platform="discord",
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                readers="own",
                writers=None,
                copy_from="daimon",
                discord_transport=None,
            )
        assert len(run.trace.requests) == native_before
        assert len(run.binding.trace.requests) == anthropic_before


@pytest.mark.parametrize("target", ["neighboring-channel", "tenant-b"])
async def test_mixed_gemini_runtime_leaves_unconfigured_copy_on_existing_dispatch(
    run: _Run, tmp_path: Path, target: str
) -> None:
    from dataclasses import replace

    from daimon.adapters.cli.commands.channels import (
        _copy_resource_host,  # pyright: ignore[reportPrivateUsage]
    )

    other = None
    try:
        async with opt_in_operator_profile(
            run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
        ) as edge:
            await edge.authorize(CHANNEL)
            rt = edge.runtime()
            if target == "neighboring-channel":
                tenant_id = uuid.UUID(run.binding.scope.tenant_id)
                channel = "223456789012345679"
            else:
                other = await provision_admin_run(
                    run.binding.sessions,
                    workspace_id="123456789012345679",
                    backend="anthropic",
                    channels=frozenset({CHANNEL}),
                    trace=ScriptedTransport(),
                )
                tenant_id = uuid.UUID(other.scope.tenant_id)
                channel = CHANNEL
            native_before = len(run.trace.requests)
            anthropic_before = len(run.binding.trace.requests)
            baseline = await _copy_resource_host(
                replace(rt, gemini_resource_hosts=None, gemini_resource_grants=()),
                tenant_id=tenant_id,
                platform="discord",
                channel=channel,
            )
            mixed = await _copy_resource_host(
                rt, tenant_id=tenant_id, platform="discord", channel=channel
            )
            assert baseline is None and mixed is None
            assert len(run.trace.requests) == native_before
            assert len(run.binding.trace.requests) == anthropic_before
    finally:
        if other is not None:
            await other.close()


@pytest.mark.parametrize("restored_default", [False, True])
async def test_mixed_gemini_runtime_keeps_existing_anthropic_agent_get_bytes(
    run: _Run, tmp_path: Path, restored_default: bool
) -> None:
    from dataclasses import replace
    from io import StringIO

    from daimon.adapters.cli.commands.agents import agents_get
    from daimon.adapters.cli.tenant import TenantSelector
    from daimon.core.channel_backend import channel_ref, clear_channel_backend, current_backend
    from daimon.testing.ma_models import ma_agent
    from mux.testing.drivers.anthropic.ma import list_response
    from mux.testing.drivers.anthropic.ma_transport import RecordedRequest, ScriptedReply
    from rich.console import Console

    other = None
    target = run.binding
    if not restored_default:
        other = await provision_admin_run(
            run.binding.sessions,
            workspace_id="123456789012345679",
            backend="anthropic",
            channels=frozenset({CHANNEL}),
            trace=ScriptedTransport(),
        )
        target = other
    trace = target.trace
    agent = ma_agent(id="agent_existing", name="existing", tenant_id=target.scope.tenant_id)
    try:
        async with opt_in_operator_profile(
            run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
        ) as edge:
            await edge.authorize(CHANNEL)
            rt = replace(edge.runtime(), anthropic=target.anthropic)
            if restored_default:
                channel = channel_ref(uuid.UUID(target.scope.tenant_id), "discord", CHANNEL)
                async with target.sessions() as session, session.begin():
                    written = await clear_channel_backend(session, channel)
                async with target.sessions() as session:
                    read = await current_backend(session, channel)
                assert read is not None
                assert read == written and read.profile == "anthropic.managed_agents"
            native_before = len(run.trace.requests)
            requests: list[tuple[RecordedRequest, ...]] = []
            outputs: list[str] = []
            for candidate in (
                replace(rt, gemini_resource_hosts=None, gemini_resource_grants=()),
                rt,
            ):
                trace.queue(
                    ScriptedReply(
                        "GET",
                        "/v1/agents",
                        list_response([agent.model_dump(mode="json")]),
                    )
                )
                before = len(trace.requests)
                output = StringIO()
                await agents_get(
                    rt=candidate,
                    console=Console(file=output, force_terminal=False, width=200),
                    name="existing",
                    as_json=True,
                    selector=TenantSelector(guild_id=target.workspace_id),
                )
                requests.append(tuple(trace.requests[before:]))
                outputs.append(output.getvalue())
            trace.assert_consumed()
            assert requests[0] and requests[0] == requests[1]
            assert outputs[0] == outputs[1] and "agent_existing" in outputs[1]
            assert len(run.trace.requests) == native_before
            (tmp_path / "mixed-anthropic-get-bytes.json").write_text(
                json.dumps(
                    {
                        "baseline": [
                            {**asdict(r), "body": r.body.hex(), "body_encoding": "hex"}
                            for r in requests[0]
                        ],
                        "gemini_factory_installed": [
                            {**asdict(r), "body": r.body.hex(), "body_encoding": "hex"}
                            for r in requests[1]
                        ],
                        "byte_identical": requests[0] == requests[1],
                        "restored_default": restored_default,
                    },
                    indent=2,
                )
            )
    finally:
        if other is not None:
            await other.close()
