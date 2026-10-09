"""Tenant entry points preserve SDK requests without operator capabilities."""

from __future__ import annotations

import ast
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.core import mux_compat
from daimon.core.defaults import ma_index
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.reconcile_environments import reconcile_environment
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.report import Action
from daimon.core.defaults.sweep import (
    sweep_removed_agents,
    sweep_removed_environments,
    sweep_removed_skills,
)
from daimon.core.mux_backend import managed_agents
from daimon.core.specs import AgentSpec, EnvironmentSpec
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
FOREIGN = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def observed_scopes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Scope]]:
    scopes: list[Scope] = []

    def record(
        client: AsyncAnthropic,
        *,
        scope: Scope | None = None,
        resources: frozenset[tuple[str, str]] = frozenset(),
    ) -> AnthropicManagedAgents:
        assert scope is not None
        scopes.append(scope)
        return managed_agents(client, scope=scope, resources=resources)

    monkeypatch.setattr(mux_compat, "managed_agents", record)
    yield scopes


def assert_tenant_scopes(scopes: list[Scope], count: int) -> None:
    assert len(scopes) == count
    for scope in scopes:
        assert scope.tenant_id == str(TENANT)
        assert not scope.is_platform
        assert not scope.is_legacy_host_authorized


@pytest.mark.parametrize(
    "entry_point,resource",
    [
        ("find_agents_by_daimon_tag", "agents"),
        ("find_environments_by_daimon_tag", "environments"),
        ("list_agents_by_tenant", "agents"),
        ("list_environments_by_tenant", "environments"),
    ],
)
async def test_tenant_index_uses_tenant_scope_and_preserves_pagination(
    entry_point: str, resource: str, observed_scopes: list[Scope]
) -> None:
    factory = ma_agent if resource == "agents" else ma_environment
    rows = [
        factory(
            id=f"{resource}-{tenant}",
            name="example",
            metadata={"daimon_tenant": str(tenant), "daimon_name": "example"},
        ).model_dump(mode="json")
        for tenant in (FOREIGN, TENANT)
    ]

    def transport() -> ScriptedTransport:
        result = ScriptedTransport()
        for index, row in enumerate(rows):
            result.queue(
                ScriptedReply(
                    "GET",
                    f"/v1/{resource}",
                    httpx.Response(
                        200,
                        json={"data": [row], "next_page": "second" if index == 0 else None},
                    ),
                )
            )
        return result

    old, new = transport(), transport()
    async with old.client() as before, new.client() as after:
        sdk = before.beta.agents if resource == "agents" else before.beta.environments
        expected = [
            row.model_dump(mode="json")
            async for row in sdk.list(include_archived=False)
            if row.metadata.get("daimon_tenant") == str(TENANT)
        ]
        if entry_point == "find_agents_by_daimon_tag":
            actual = await ma_index.find_agents_by_daimon_tag(
                after, tenant_id=TENANT, name="example"
            )
        elif entry_point == "find_environments_by_daimon_tag":
            actual = await ma_index.find_environments_by_daimon_tag(
                after, tenant_id=TENANT, name="example"
            )
        elif entry_point == "list_agents_by_tenant":
            actual = await ma_index.list_agents_by_tenant(after, tenant_id=TENANT)
        else:
            actual = await ma_index.list_environments_by_tenant(after, tenant_id=TENANT)
    old.assert_consumed()
    new.assert_consumed()
    assert new.requests == old.requests
    assert [row.model_dump(mode="json") for row in actual] == expected
    assert_tenant_scopes(observed_scopes, 1)


@pytest.mark.parametrize("existing", [False, True])
async def test_tenant_reconcile_scopes_create_update_retry_and_duplicate_archive(
    existing: bool, observed_scopes: list[Scope]
) -> None:
    def agent(native_id: str, tenant: uuid.UUID = TENANT, day: int = 2):
        return ma_agent(
            id=native_id,
            name="example",
            metadata={"daimon_tenant": str(tenant), "daimon_name": "example"},
            created_at=datetime(2026, 1, day, tzinfo=UTC),
        ).model_dump(mode="json")

    canonical = agent("canonical")
    duplicate = agent("duplicate", day=1)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [agent("foreign", FOREIGN), duplicate, canonical] if existing else []
        ),
    )
    router.add("GET", r"/v1/agents/canonical", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add("POST", r"/v1/agents/canonical", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add("POST", r"/v1/agents", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add(
        "POST", r"/v1/agents/duplicate/archive", lambda _r, _m: httpx.Response(200, json=duplicate)
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        result = await reconcile_agent(
            client,
            AgentSpec(name="example", model="claude-sonnet-4-6"),
            tenant_id=TENANT,
            dry_run=False,
        )
    transport.assert_consumed()
    assert result.action == (Action.UPDATED if existing else Action.CREATED)
    expected = [("GET", "/v1/agents")]
    if existing:
        expected += [
            ("POST", "/v1/agents/duplicate/archive"),
            ("GET", "/v1/agents/canonical"),
            ("POST", "/v1/agents/canonical"),
        ]
    else:
        expected += [("POST", "/v1/agents")]
    assert [(r.method, r.path) for r in transport.requests] == expected
    assert_tenant_scopes(observed_scopes, len(expected))


@pytest.mark.parametrize("existing", [False, True])
async def test_tenant_environment_reconcile_scopes_create_update_and_duplicate_archive(
    existing: bool, observed_scopes: list[Scope]
) -> None:
    def environment(native_id: str, tenant: uuid.UUID = TENANT, day: int = 2):
        return ma_environment(
            id=native_id,
            name="example",
            metadata={"daimon_tenant": str(tenant), "daimon_name": "example"},
            created_at=f"2026-01-{day:02d}T00:00:00Z",
        ).model_dump(mode="json")

    canonical = environment("canonical")
    duplicate = environment("duplicate", day=1)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda _r, _m: list_response(
            [environment("foreign", FOREIGN), duplicate, canonical] if existing else []
        ),
    )
    router.add("POST", r"/v1/environments", lambda _r, _m: httpx.Response(200, json=canonical))
    router.add(
        "POST", r"/v1/environments/canonical", lambda _r, _m: httpx.Response(200, json=canonical)
    )
    router.add(
        "POST",
        r"/v1/environments/duplicate/archive",
        lambda _r, _m: httpx.Response(200, json=duplicate),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        result = await reconcile_environment(
            client, EnvironmentSpec(name="example"), tenant_id=TENANT, dry_run=False
        )
    transport.assert_consumed()
    assert result.action == (Action.UPDATED if existing else Action.CREATED)
    expected = [("GET", "/v1/environments")]
    expected += (
        [
            ("POST", "/v1/environments/duplicate/archive"),
            ("POST", "/v1/environments/canonical"),
        ]
        if existing
        else [("POST", "/v1/environments")]
    )
    assert [(r.method, r.path) for r in transport.requests] == expected
    assert_tenant_scopes(observed_scopes, len(expected))


def skill_row(native_id: str, title: str, day: int = 2) -> SkillListResponse:
    return SkillListResponse.model_validate(
        {
            "id": native_id,
            "type": "skill",
            "display_title": title,
            "source": "custom",
            "latest_version": "1",
            "created_at": f"2026-01-{day:02d}T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_tenant_skill_reconcile_scopes_create_publish_and_duplicate_delete(
    existing: bool,
    observed_scopes: list[Scope],
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await make_tenant(db_session, id=TENANT)
    await db_session.commit()
    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: example\ndescription: d\n---\nbody")
    canonical = skill_row("canonical", f"{str(TENANT)[:8]}-example")
    duplicate = skill_row("duplicate", canonical.display_title or "", day=1)
    router = MARouter()
    router.add("GET", r"/v1/skills/duplicate/versions", lambda _r, _m: list_response([]))
    router.add(
        "DELETE",
        r"/v1/skills/duplicate",
        lambda _r, _m: httpx.Response(
            200, json={"id": "duplicate", "type": "skill", "deleted": True}
        ),
    )
    router.add(
        "POST",
        r"/v1/skills",
        lambda _r, _m: httpx.Response(200, json=canonical.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/skills/canonical/versions",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "id": "version",
                "skill_id": "canonical",
                "version": "2",
                "type": "skill_version",
                "name": "example",
                "directory": "example",
                "description": "d",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ),
    )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        result = await reconcile_skill(
            client,
            db_session_factory,
            skill_dir,
            tenant_id=TENANT,
            dry_run=False,
            skills_view=[canonical, duplicate, skill_row("foreign", "ffffffff-example")]
            if existing
            else [],
        )
    transport.assert_consumed()
    assert result.action == (Action.UPDATED if existing else Action.CREATED)
    expected = (
        [
            ("GET", "/v1/skills/duplicate/versions"),
            ("DELETE", "/v1/skills/duplicate"),
            ("POST", "/v1/skills/canonical/versions"),
        ]
        if existing
        else [("POST", "/v1/skills")]
    )
    assert [(r.method, r.path) for r in transport.requests] == expected
    assert_tenant_scopes(observed_scopes, len(expected))


@pytest.mark.parametrize("resource", ["agents", "environments", "skills"])
async def test_tenant_sweeps_use_tenant_scope_for_mutation(
    resource: str, observed_scopes: list[Scope]
) -> None:
    router = MARouter()
    if resource == "skills":
        router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
        router.add("GET", r"/v1/skills/owned/versions", lambda _r, _m: list_response([]))
        router.add(
            "DELETE",
            r"/v1/skills/owned",
            lambda _r, _m: httpx.Response(
                200, json={"id": "owned", "type": "skill", "deleted": True}
            ),
        )
    else:
        factory = ma_agent if resource == "agents" else ma_environment
        records = [
            factory(
                id=native_id,
                name="gone",
                metadata={
                    "daimon_tenant": str(tenant),
                    "daimon_name": "gone",
                    "daimon_managed": "true",
                },
            ).model_dump(mode="json")
            for native_id, tenant in [("foreign", FOREIGN), ("owned", TENANT)]
        ]
        router.add("GET", f"/v1/{resource}", lambda _r, _m: list_response(records))
        router.add(
            "POST",
            f"/v1/{resource}/owned/archive",
            lambda _r, _m: httpx.Response(200, json=records[1]),
        )
    transport = ScriptedTransport(router=router)
    async with transport.client() as client:
        if resource == "agents":
            result = await sweep_removed_agents(
                client, present_names=set(), tenant_id=TENANT, dry_run=False
            )
        elif resource == "environments":
            result = await sweep_removed_environments(
                client, present_names=set(), tenant_id=TENANT, dry_run=False
            )
        else:
            result = await sweep_removed_skills(
                client,
                present_names=set(),
                tenant_id=TENANT,
                dry_run=False,
                skills_view=[
                    skill_row("owned", f"{str(TENANT)[:8]}-gone"),
                    skill_row("foreign", "ffffffff-gone"),
                ],
            )
    transport.assert_consumed()
    assert len(result) == 1 and result[0].anthropic_id == "owned"
    if resource == "skills":
        assert [(r.method, r.path) for r in transport.requests] == [
            ("GET", "/v1/agents"),
            ("GET", "/v1/skills/owned/versions"),
            ("DELETE", "/v1/skills/owned"),
        ]
        assert observed_scopes[
            0
        ].is_platform  # References across all tenants protect shared skills.
        assert_tenant_scopes(observed_scopes[1:], 2)
    else:
        assert [(r.method, r.path) for r in transport.requests] == [
            ("GET", f"/v1/{resource}"),
            ("POST", f"/v1/{resource}/owned/archive"),
        ]
        assert_tenant_scopes(observed_scopes, 2)


def test_core_platform_scopes_are_limited_to_workspace_operations() -> None:
    """A new privileged core call must be audited before entering this inventory."""
    core = Path(ma_index.__file__).parents[1]
    calls: Counter[tuple[str, str]] = Counter()
    for path in core.rglob("*.py"):

        class Scan(ast.NodeVisitor):
            function = "<module>"

            def __init__(self, module: str) -> None:
                self.module = module

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                previous, self.function = self.function, node.name
                self.generic_visit(node)
                self.function = previous

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                previous, self.function = self.function, node.name
                self.generic_visit(node)
                self.function = previous

            def visit_Call(self, node: ast.Call) -> None:
                target = node.func
                if (
                    (isinstance(target, ast.Name) and target.id == "platform_scope")
                    or (isinstance(target, ast.Attribute) and target.attr == "platform_scope")
                    or (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "Scope"
                        and target.attr == "platform"
                    )
                ):
                    calls[(self.module, self.function)] += 1
                self.generic_visit(node)

        Scan(path.relative_to(core).as_posix()).visit(ast.parse(path.read_text()))
    assert calls == Counter(
        {
            ("usage_sweep.py", "sweep_headless_usage"): 1,
            ("defaults/ma_index.py", "list_agents_by_tenants"): 1,
            ("defaults/ma_index.py", "list_referenced_skill_ids"): 1,
            ("defaults/ma_index.py", "_collect_skills_page"): 1,
            ("defaults/platform_export.py", "export_platform"): 1,
            ("defaults/preflight.py", "check_model_accepted"): 2,
            ("mux_backend.py", "platform_scope"): 1,  # Shared constructor, not an operation.
            ("mcp_vault_janitor.py", "archive_orphan_mcp_vaults"): 1,
            ("mcp_credential_sweep.py", "sweep_stale_admin_credentials"): 1,
            ("pending_file_sweeper.py", "sweep_pending_file_deletes"): 1,
            ("ma.py", "find_workspace_disposable_sentinel"): 1,
            ("ma.py", "delete_entire_workspace_for_testing"): 1,
        }
    )
