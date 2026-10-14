"""QA-D14 and QA-D5 in the OpenAI column, through the real operator CLI, under the opt-in profile.

The frozen scenario operations (`tests/judge/fixtures` native pack) run through
N6's admin bindings. Under `opt_in_operator_profile`, the channel copy runs the
real `daimon channels rule set` CLI command (A1's OpenAI path) and each frozen
`cli_check` runs the real `daimon agents … get`. Everything else stays on N6's
resource edge. The fake provider is an offline stand-in for the OpenAI agents
API; every result here is labelled "under opt-in profile".
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.core.defaults.metadata import build_metadata
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.testing.admin_bindings import AdminBindings, provision_admin_run
from daimon.testing.admin_resources import ResourceAdminBindings
from daimon.testing.cli_resources import (
    OPT_IN_OPERATOR_PROFILE,
    CliCheckReceipt,
    cli_check_holds,
    opt_in_operator_profile,
)
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.isolated_sdk import build_async_openai
from daimon.testing.ma import MARouter
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport as OpenAITransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["daimon.testing.db"]

NATIVE = Path(__file__).resolve().parents[1] / "fixtures" / "target53" / "native"
WORKSPACE, CHANNEL = "123456789012345678", "223456789012345678"
DATA = bundle_from_markdown(
    "---\nname: qa-echo-skill\ndescription: Offline echo\n---\nEcho the requested text.\n"
)


def _scenario(scenario_id: str) -> dict[str, Any]:
    return json.loads((NATIVE / f"{scenario_id}.json").read_text())["scenario"]


class _OpenAIServer:
    """An offline stand-in for the OpenAI agents and skills API."""

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.router = MARouter()
        self.router.add("POST", r"/v1/agents", self.create)
        self.router.add(
            "GET",
            r"/v1/agents/(?P<id>[^/]+)",
            lambda request, match: httpx.Response(200, json=self.agents[match["id"]]),
        )
        self.router.add("POST", r"/v1/skills", self.upload)
        self.router.add(
            "GET",
            r"/v1/skills/(?P<id>[^/]+)/versions/1",
            lambda request, match: httpx.Response(
                200,
                json={"skill_id": match["id"], "version": "1", "name": "qa-echo-skill"}
                | {"created_at": 0},
            ),
        )

    def upload(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        id_ = f"skill_{len(self.skills)}"
        self.skills[id_] = {
            "id": id_,
            "name": "qa-echo-skill",
            "latest_version": "1",
            "default_version": "1",
            "created_at": 0,
        }
        return httpx.Response(200, json=self.skills[id_])

    def create(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = json.loads(request.content)
        id_ = f"agent_{len(self.agents)}"
        self.agents[id_] = {**raw, "id": id_, "created_at": 0}
        return httpx.Response(200, json=self.agents[id_])


class _Run:
    def __init__(
        self, binding: AdminBindings, resources: ResourceAdminBindings, driver: OpenAIDriver
    ) -> None:
        self.binding, self.resources, self.driver = binding, resources, driver

    def values(self, **extra: str) -> dict[str, str]:
        return {
            "guild_id": WORKSPACE,
            "channel_id": CHANNEL,
            "env.qa_base_with_own_skill": "daimon",
            **extra,
        }


@pytest.fixture
async def run(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[_Run]:
    """An OpenAI column run: provisioned tenant, the `daimon` agent with its own skill."""
    require_owned_sessionmaker(db_session_factory)
    binding = await provision_admin_run(
        db_session_factory,
        workspace_id=WORKSPACE,
        backend="openai",
        channels=frozenset({CHANNEL}),
        trace=ScriptedTransport(),
    )
    binding.default = DeploymentDefault(agent_name="daimon")
    trace = ScriptedTransport(router=_OpenAIServer().router)
    openai = build_async_openai(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(trace.dispatch),
    )
    tenant, account = binding.scope.tenant_id, binding.scope.account_id
    driver = OpenAIDriver(
        OpenAITransport(openai),
        account_scope_id="project",
        # Both the run's admin scope and the operator CLI's act for this account.
        authorization=lambda scope, _kind, _id: (
            (scope.tenant_id, scope.account_id) == (tenant, account)
        ),
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    resources = ResourceAdminBindings(
        binding,
        backend=driver,
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
            model=ModelRef(provider="openai", id="gpt-6-luna"),
            system="Help offline.",
            tools=(ToolSpec(name="bash", kind="builtin"),),
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
        yield _Run(binding, resources, driver)
    finally:
        await binding.close()
        await openai.close()


async def test_d5_openai_create_stays_pending_under_the_opt_in_profile(
    run: _Run, tmp_path: Path
) -> None:
    scenario = _scenario("QA-D5-UNBOUND-AGENT-EDIT-REFUSED")
    async with opt_in_operator_profile(
        run.binding, run.driver, workspace="project", root=tmp_path, fallback=run.resources
    ):
        created = await run.binding.execute(scenario["setup"][0], run.values(nonce="qa"))
    # No YAML → neutral spec edge exists, and the fixture's Claude model is never translated.
    assert (created.status, created.code) == ("pending", "ADMIN_AGENT_SPEC_FIXTURE_UNBOUND")


def _receipt(output: str, error: str | None = None) -> CliCheckReceipt:
    return CliCheckReceipt(
        profile=OPT_IN_OPERATOR_PROFILE, command="daimon agents get x", output=output, error=error
    )


_D5 = {
    "kind": "cli_check",
    "cmd": "c",
    "expect": "qa-original",
    "expect_absent": "qa probe|deepwiki",
}


@pytest.mark.parametrize(
    ("assertion", "output", "holds"),
    [
        ({"expect": "{model}"}, "qa-fork  gpt-6-luna", True),
        ({"expect": "{model}"}, "qa-fork", False),
        ({"expect_all_of": "{skills}"}, "uses qa-echo-skill and notes", True),
        ({"expect_all_of": "{skills}"}, "uses qa-echo-skill only", False),
        ({"expect_all_of": "qa-echo-skill"}, "qa-echo-skill", True),
        (_D5, "qa-agent  qa-original", True),
        (_D5, "qa-agent  qa-original  qa probe", False),
        (_D5, "qa-agent  deepwiki", False),
        (_D5, "qa-agent", False),
    ],
)
def test_cli_check_holds_applies_the_catalog_semantics(
    assertion: dict[str, Any], output: str, holds: bool
) -> None:
    values: dict[str, object] = {"model": "gpt-6-luna", "skills": ["qa-echo-skill", "notes"]}
    assert cli_check_holds(assertion, _receipt(output), values) is holds


def test_cli_check_holds_is_false_on_a_failed_command_and_refuses_unbound_values() -> None:
    assert cli_check_holds({"expect": "x"}, _receipt("x", error="StoreError"), {}) is False
    with pytest.raises(ValueError, match="unbound placeholder"):
        cli_check_holds({"expect": "{fork.name}"}, _receipt("x"), {})
