"""Tests for provision_tenant idempotent DB saga.

Validates SC-4: calling provision_tenant twice with identical inputs produces
exactly one Tenant row.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsModelConfig,
    SkillListResponse,
)
from daimon.core._models import Account, Tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    tenant_scoped_display_title,
)
from daimon.core.defaults.provisioning import (
    ProvisionResult,
    _derive_account_uuid,  # pyright: ignore[reportPrivateUsage]
    provision_tenant,
    reconcile_tenant_defaults,
)
from daimon.core.defaults.report import Action
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_ledger
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CREATED_AT = datetime(2026, 4, 21, 0, 0, 0, tzinfo=UTC)
_AGENT_MODEL = BetaManagedAgentsModelConfig(id="claude-opus-4-7")


def _write_seed_tree(root: Path) -> None:
    """Write a minimal defaults tree: one agent, one environment, one skill."""
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)
    skill_dir = root / "skills" / "cli-auth"
    skill_dir.mkdir(parents=True)
    (root / "agents" / "daimon.yaml").write_text("name: daimon\nmodel: claude-sonnet-4-6\n")
    (root / "environments" / "default.yaml").write_text("name: default\n")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: cli-auth\ndescription: seed skill for provisioning tests.\n---\n\n# cli-auth\n"
    )


def _create_serving_router(
    *,
    skill_id: str = "sk_1",
    skill_create_handler: Any = None,
    agent_create_handler: Any = None,
    env_create_handler: Any = None,
) -> MARouter:
    """Router that lists nothing and serves successful CREATE for skill/env/agent.

    Tests needing to capture or fail a specific create pass their own handler —
    MARouter dispatches the FIRST matching route, so a custom handler must be
    registered BEFORE the default. The optional handler params register first.
    """
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    if skill_create_handler is not None:
        router.add("POST", r"/v1/skills", skill_create_handler)
    if env_create_handler is not None:
        router.add("POST", r"/v1/environments", env_create_handler)
    if agent_create_handler is not None:
        router.add("POST", r"/v1/agents", agent_create_handler)
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillListResponse(
                id=skill_id,
                type="custom",
                display_title="seed-skill",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_1",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_1",
                type="agent",
                name="daimon",
                model=_AGENT_MODEL,
                metadata={},
                description=None,
                created_at=_CREATED_AT,
                updated_at=_CREATED_AT,
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    # Preflight probe (check_models_accepted) creates then archives a throwaway agent.
    router.add(
        "POST",
        r"/v1/agents/[^/]+/archive",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_1",
                type="agent",
                name="daimon",
                model=_AGENT_MODEL,
                metadata={},
                description=None,
                created_at=_CREATED_AT,
                updated_at=_CREATED_AT,
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    return router


def _existing_resources_router(*, tenant_id: uuid.UUID) -> MARouter:
    """Router whose LIST routes return resources already tagged for `tenant_id`.

    A second reconcile run must find these and record SKIPPED — no CREATE routes
    are registered, so any write attempt raises in the router.
    """
    tenant_id_str = str(tenant_id)
    created_at_str = "2026-04-21T00:00:00Z"
    existing_skill = SkillListResponse(
        id="sk_1",
        type="custom",
        display_title=tenant_scoped_display_title(tenant_id=tenant_id, name="cli-auth"),
        latest_version="1",
        created_at=created_at_str,
        updated_at=created_at_str,
        source="custom",
    ).model_dump(mode="json")
    existing_env = BetaEnvironment(
        id="env_1",
        type="environment",
        name="default",
        config=EMPTY_CLOUD_CONFIG,
        metadata={MA_METADATA_KEY_TENANT: tenant_id_str, MA_METADATA_KEY_NAME: "default"},
        description="",
        created_at=created_at_str,
        updated_at=created_at_str,
    ).model_dump(mode="json")
    existing_agent = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="daimon",
        model=_AGENT_MODEL,
        metadata={MA_METADATA_KEY_TENANT: tenant_id_str, MA_METADATA_KEY_NAME: "daimon"},
        description=None,
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([existing_skill]))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([existing_env]))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([existing_agent]))
    # 2-state reconcile updates env/agent in place when found on MA — serve the
    # update echo so the second run does not crash on a write.
    router.add(
        "POST", r"/v1/environments/env_1", lambda req, _m: httpx.Response(200, json=existing_env)
    )
    router.add("POST", r"/v1/agents/ag_1", lambda req, _m: httpx.Response(200, json=existing_agent))
    # Preflight probe: creates a throwaway agent (POST /v1/agents) then archives it.
    # The agent reconcile itself UPDATEs the found agent via POST /v1/agents/ag_1, so
    # this collection-POST route is only ever hit by the preflight probe.
    router.add("POST", r"/v1/agents", lambda req, _m: httpx.Response(200, json=existing_agent))
    router.add(
        "POST",
        r"/v1/agents/[^/]+/archive",
        lambda req, _m: httpx.Response(200, json=existing_agent),
    )
    return router


async def test_provision_tenant_creates_tenant_and_account_when_new(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await provision_tenant(db_session_factory, platform="discord", workspace_id="g1")

    assert isinstance(result, ProvisionResult), "should return ProvisionResult Pydantic model"
    assert result.platform == "discord", "platform should be discord"
    assert result.external_id == "g1", "external_id should match workspace_id arg"

    expected_tenant_id = derive_tenant_uuid(platform="discord", workspace_id="g1")
    assert result.tenant_id == expected_tenant_id, (
        "tenant_id should equal derive_tenant_uuid(discord, g1)"
    )

    tenant_count = (
        await db_session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == expected_tenant_id)
        )
    ).scalar_one()
    assert tenant_count == 1, "should have exactly 1 tenant row"

    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account).where(Account.id == result.account_id)
        )
    ).scalar_one()
    assert account_count == 1, "should have exactly 1 account row"


async def test_provision_tenant_is_idempotent_when_called_twice(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-idempotent"
    )
    second = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-idempotent"
    )

    assert first.tenant_id == second.tenant_id, (
        "both calls should return the same deterministic tenant_id"
    )
    assert first.account_id == second.account_id, (
        "both calls should return the same deterministic account_id"
    )

    tenant_count = (
        await db_session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == first.tenant_id)
        )
    ).scalar_one()
    assert tenant_count == 1, "SC-4: exactly 1 Tenant row after two identical calls"

    account_count = (
        await db_session.execute(
            select(func.count()).select_from(Account).where(Account.id == first.account_id)
        )
    ).scalar_one()
    assert account_count == 1, "SC-4: exactly 1 Account row after two identical calls"


async def test_provision_tenant_distinct_workspaces_yield_distinct_tenants(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result_a = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-aaa"
    )
    result_b = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-bbb"
    )

    assert result_a.tenant_id != result_b.tenant_id, (
        "distinct workspace_ids should yield distinct tenant_ids"
    )
    assert isinstance(result_a.tenant_id, uuid.UUID), "tenant_id should be a UUID"
    assert isinstance(result_b.tenant_id, uuid.UUID), "tenant_id should be a UUID"

    all_tenant_ids = (
        (
            await db_session.execute(
                select(Tenant.id).where(Tenant.id.in_([result_a.tenant_id, result_b.tenant_id]))
            )
        )
        .scalars()
        .all()
    )
    assert len(all_tenant_ids) == 2, "should have 2 distinct tenant rows in DB"

    tenant_a_expected = derive_tenant_uuid(platform="discord", workspace_id="guild-aaa")
    tenant_b_expected = derive_tenant_uuid(platform="discord", workspace_id="guild-bbb")
    assert result_a.tenant_id == tenant_a_expected, (
        "tenant_id for guild-aaa should match derive_tenant_uuid"
    )
    assert result_b.tenant_id == tenant_b_expected, (
        "tenant_id for guild-bbb should match derive_tenant_uuid"
    )


# --- reconcile_tenant_defaults orchestrator (provision_idempotent invariant) ---


async def test_reconcile_tenant_defaults_idempotent_on_rerun(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """provision_idempotent: re-running the orchestrator for the same guild
    creates zero duplicate MA resources. First run CREATEs; second run (lists
    already serving the tenant-tagged resources) records no CREATED actions."""
    _write_seed_tree(tmp_path)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-rerun"
    )

    first = await reconcile_tenant_defaults(
        build_fake_anthropic_http(_create_serving_router().dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )
    assert [o.action for o in first.skills] == [Action.CREATED], "first run creates the skill"
    assert [o.action for o in first.environments] == [Action.CREATED], "first run creates the env"
    assert [o.action for o in first.agents] == [Action.CREATED], "first run creates the agent"

    second = await reconcile_tenant_defaults(
        build_fake_anthropic_http(_existing_resources_router(tenant_id=result.tenant_id).dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )
    assert Action.CREATED not in [o.action for o in second.skills], (
        "second run must not re-create the skill"
    )
    assert Action.CREATED not in [o.action for o in second.environments], (
        "second run must not re-create the environment"
    )
    assert Action.CREATED not in [o.action for o in second.agents], (
        "second run must not re-create the agent"
    )


async def test_reconcile_tenant_defaults_uses_passed_tenant_not_bootstrap(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """provision_idempotent: resources reconcile under the PASSED tenant_id.
    The agent CREATE payload must carry daimon_tenant == passed tenant, proving
    no ensure_tenant_bootstrap (earliest-row) substitution happened."""
    _write_seed_tree(tmp_path)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-tenant"
    )

    real_agent_payload: dict[str, Any] = {}

    def on_agent_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        # Preflight probe also POSTs /v1/agents (metadata daimon_preflight); capture
        # only the real reconcile create, which carries daimon_tenant.
        if "daimon_preflight" not in body.get("metadata", {}):
            real_agent_payload.update(body)
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_1",
                type="agent",
                name="daimon",
                model=_AGENT_MODEL,
                metadata=body.get("metadata", {}),
                description=None,
                created_at=_CREATED_AT,
                updated_at=_CREATED_AT,
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    router = _create_serving_router(agent_create_handler=on_agent_create)
    await reconcile_tenant_defaults(
        build_fake_anthropic_http(router.dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )
    assert real_agent_payload["metadata"][MA_METADATA_KEY_TENANT] == str(result.tenant_id), (
        "agent must be reconciled under the passed tenant_id, not a bootstrap tenant"
    )


async def test_reconcile_tenant_defaults_applies_tenant_prefix_to_skill_title(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """provision_idempotent: the reconciled skill's display_title carries the
    {tenant_id[:8]}- prefix, proving the ISO-04 tenant-prefixed reconcile_skill
    path is reached (W3 coverage)."""
    _write_seed_tree(tmp_path)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-prefix"
    )

    skill_payload: dict[str, Any] = {}

    def on_skill_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        # Skill create is multipart; display_title rides as a form field.
        skill_payload["body"] = req.content.decode("utf-8", errors="replace")
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_1",
                type="custom",
                display_title="created",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router = _create_serving_router(skill_create_handler=on_skill_create)
    await reconcile_tenant_defaults(
        build_fake_anthropic_http(router.dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )
    expected_prefix = f"{str(result.tenant_id)[:8]}-"
    assert expected_prefix in skill_payload["body"], (
        "skill create must carry the tenant-prefixed display_title (ISO-04)"
    )


async def test_reconcile_tenant_defaults_stamps_guild_account_on_agents(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seeded guild agents must carry daimon_account = derived guild account.

    Environments are NOT account-owned and must NOT carry daimon_account.
    Verified via transport-fake AsyncAnthropic — captures the raw request bodies
    so the assertion runs against what actually reaches MA.
    """
    _write_seed_tree(tmp_path)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-rbac01"
    )

    agent_metadata: dict[str, Any] = {}
    env_metadata: dict[str, Any] = {}

    def on_agent_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        # Preflight probe POSTs /v1/agents with a throwaway agent — skip it.
        if "daimon_preflight" not in body.get("metadata", {}):
            agent_metadata.update(body.get("metadata", {}))
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_rbac",
                type="agent",
                name="daimon",
                model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                metadata=body.get("metadata", {}),
                description=None,
                created_at=datetime(2026, 4, 21),
                updated_at=datetime(2026, 4, 21),
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    def on_env_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        env_metadata.update(body.get("metadata", {}))
        return httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_rbac",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata=body.get("metadata", {}),
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        )

    router = _create_serving_router(
        agent_create_handler=on_agent_create,
        env_create_handler=on_env_create,
    )
    await reconcile_tenant_defaults(
        build_fake_anthropic_http(router.dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )

    expected_account = str(_derive_account_uuid(result.tenant_id))
    assert agent_metadata.get(MA_METADATA_KEY_ACCOUNT) == expected_account, (
        "seeded guild agent must be guild-owned: "
        f"expected daimon_account={expected_account!r}, got {agent_metadata.get(MA_METADATA_KEY_ACCOUNT)!r}"
    )
    assert MA_METADATA_KEY_ACCOUNT not in env_metadata, (
        "environments are not account-owned: daimon_account must not appear in env metadata"
    )


# ---------------------------------------------------------------------------
# SC-1: end-to-end seed-tree with a custom skill ref (#128 gap proof)
# ---------------------------------------------------------------------------


def _write_seed_tree_with_skill_ref(root: Path, skill_name: str) -> None:
    """Write a minimal defaults tree where the agent's skills list references a custom skill.

    The agent YAML carries a bare-name SkillRef (as authored). reconcile_tenant_defaults
    must create the skill first (prefixed), then resolve the bare name -> canonical title
    -> MA id when building the agent create/update payload (the #128 gap proof).
    """
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)
    skill_dir = root / "skills" / skill_name
    skill_dir.mkdir(parents=True)
    (root / "agents" / "daimon.yaml").write_text(
        f"name: daimon\nmodel: claude-sonnet-4-6\n"
        f"skills:\n  - type: custom\n    skill_id: {skill_name}\n"
    )
    (root / "environments" / "default.yaml").write_text("name: default\n")
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: seed skill for SC-1 test.\n---\n\n# {skill_name}\n"
    )


async def test_reconcile_tenant_defaults_seed_tree_with_skill_ref_pins_tenant_skill(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SC-1 end-to-end proof: seed tree whose agent references a custom skill results in:

    1. create-prefixed: skill create carries the canonical tenant-prefixed display_title.
    2. resolve-pinned: agent create payload pins the CREATED skill's MA id
       ({"type": "custom", "skill_id": "sk_sc1"}) — the #128 gap proof.
    3. status-ready: report has no FAILED outcomes (caller maps this to 'ready').
    4. no-delete: the sweep spares the just-created skill (SC-2 composition check).
    """
    skill_name = "brainstorm"
    _write_seed_tree_with_skill_ref(tmp_path, skill_name)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-sc1"
    )

    # Stateful list handler: skills list starts empty, reflects created skills after POST.
    created_skills: list[dict[str, Any]] = []
    delete_requests: list[str] = []
    agent_payloads: list[dict[str, Any]] = []
    canonical = tenant_scoped_display_title(tenant_id=result.tenant_id, name=skill_name)

    def skills_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response(list(created_skills))

    def skill_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        row = SkillListResponse(
            id="sk_sc1",
            type="custom",
            display_title=canonical,
            latest_version="1",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        created_skills.append(row)
        return httpx.Response(200, json=row)

    def skill_delete(req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        delete_requests.append(m.group(0))
        return httpx.Response(200, json={"deleted": True})

    def agent_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body: dict[str, Any] = json.loads(req.content)
        # Preflight probe POSTs /v1/agents with daimon_preflight; capture only the real reconcile.
        if "daimon_preflight" not in body.get("metadata", {}):
            agent_payloads.append(body)
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_sc1",
                type="agent",
                name="daimon",
                model={"id": "claude-sonnet-4-6"},
                metadata=body.get("metadata", {}),
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("GET", r"/v1/skills", skills_list)
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/skills", skill_create)
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_sc1",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        ),
    )
    router.add("POST", r"/v1/agents", agent_create)
    router.add(
        "POST",
        r"/v1/agents/[^/]+/archive",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_sc1",
                type="agent",
                name="daimon",
                model={"id": "claude-sonnet-4-6"},
                metadata={},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    router.add("DELETE", r"/v1/skills/[^/]+", skill_delete)

    report = await reconcile_tenant_defaults(
        build_fake_anthropic_http(router.dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )

    expected_prefix = f"{str(result.tenant_id)[:8]}-"

    # Leg 1: create-prefixed — skill outcome is CREATED (not FAILED/SKIPPED).
    assert [o.action for o in report.skills] == [Action.CREATED], (
        "create-prefixed: skill outcome must be CREATED, "
        "proving reconcile_skill used the canonical tenant-prefixed title"
    )

    # Leg 2: resolve-pinned — agent payload carries the MA id of the created skill.
    # The SDK may retry on transient errors; assert that at least one real create happened
    # and every captured payload pins the created skill id (all retries carry the same resolved skills).
    assert len(agent_payloads) >= 1, (
        "resolve-pinned: at least one real agent create payload must be captured"
    )
    for idx, payload in enumerate(agent_payloads):
        agent_skills: list[dict[str, Any]] = payload.get("skills", [])
        assert len(agent_skills) == 1, (
            f"resolve-pinned: agent payload[{idx}] must pin exactly one skill, got {agent_skills!r}"
        )
        pinned = agent_skills[0]
        assert pinned.get("type") == "custom", (
            f"resolve-pinned: payload[{idx}] pinned skill must have type='custom', got {pinned!r}"
        )
        assert pinned.get("skill_id") == "sk_sc1", (
            f"resolve-pinned: payload[{idx}] must pin the CREATED skill id 'sk_sc1' (the #128 proof); "
            f"prefix applied: {expected_prefix!r}; canonical title used for lookup: {canonical!r}; "
            f"got skill_id={pinned.get('skill_id')!r}"
        )

    # Leg 3: status-ready — no FAILED outcomes. The reconcile spine is DB-free;
    # the on_guild_join caller maps `not report.is_failure()` to provision_status
    # "ready", so report-level success is the core-layer equivalent of that flip.
    assert not report.is_failure(), (
        f"status-ready: report must not be a failure; "
        f"outcomes: {[(o.action, o.error) for o in report.skills + report.agents]!r}"
    )

    # Leg 4: no-delete — the sweep must have spared the just-created skill.
    assert len(delete_requests) == 0, (
        f"no-delete: zero skill DELETE requests expected (SC-2 composition: "
        f"sweep spares same-run creation), but got: {delete_requests!r}"
    )


# ---------------------------------------------------------------------------
# SC-5: full list page fails the guild seed loudly
# ---------------------------------------------------------------------------


async def test_reconcile_tenant_defaults_flips_status_failed_when_skills_list_page_is_full(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SC-5 end-to-end proof: skills list at 100 rows (full page) -> SkillsListTruncatedError
    is caught by _run_per_resource -> FAILED skill outcome -> provision_status 'failed'.

    Assertions:
    1. The run completes without raising (boundary catches SkillsListTruncatedError as DaimonError).
    2. At least one FAILED skill outcome whose error string names the truncation.
    3. report.is_failure() is True (caller maps this to provision_status 'failed').
    4. Zero skills.create calls (no writes on a truncated view).
    """
    skill_name = "brainstorm"
    _write_seed_tree_with_skill_ref(tmp_path, skill_name)
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-sc5"
    )
    skill_create_count = 0

    def skill_create_sentinel(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal skill_create_count
        skill_create_count += 1
        return httpx.Response(
            500, json={"type": "error", "error": {"type": "api_error", "message": "unexpected"}}
        )

    filler = [
        SkillListResponse(
            id=f"filler_{i}",
            type="custom",
            display_title=f"unrelated-filler-{i}",
            latest_version="1",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        for i in range(100)
    ]

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(filler))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response([]))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/skills", skill_create_sentinel)
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_sc5",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_sc5",
                type="agent",
                name="daimon",
                model={"id": "claude-sonnet-4-6"},
                metadata={},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/[^/]+/archive",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_sc5",
                type="agent",
                name="daimon",
                model={"id": "claude-sonnet-4-6"},
                metadata={},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )

    # Assertion 1: run completes without raising (boundary catches SkillsListTruncatedError).
    report = await reconcile_tenant_defaults(
        build_fake_anthropic_http(router.dispatch),
        tmp_path,
        tenant_id=result.tenant_id,
    )

    # Assertion 2: at least one FAILED skill outcome whose error names the truncation.
    failed_skills = [o for o in report.skills if o.action == Action.FAILED]
    assert len(failed_skills) >= 1, (
        "SC-5: at least one FAILED skill outcome expected when the skills list is full, "
        f"got outcomes: {[(o.action, o.error) for o in report.skills]!r}"
    )
    truncation_errors = [
        o
        for o in failed_skills
        if o.error is not None
        and (
            "truncat" in o.error.lower() or "page" in o.error.lower() or "limit" in o.error.lower()
        )
    ]
    assert len(truncation_errors) >= 1, (
        "SC-5: FAILED skill outcome error must mention truncation/page/limit, "
        f"got error strings: {[o.error for o in failed_skills]!r}"
    )

    # Assertion 3: the report is a failure. The reconcile spine is DB-free; the
    # on_guild_join caller maps `report.is_failure()` to provision_status "failed",
    # so report-level failure is the core-layer half of "fails the guild loudly".
    assert report.is_failure(), (
        "SC-5: report.is_failure() must be True when the skills list is truncated "
        "(the caller maps this to provision_status='failed')"
    )

    # Assertion 4: zero skills.create calls (no writes on a truncated view).
    assert skill_create_count == 0, (
        f"SC-5: skill create must never be called when the list is truncated, "
        f"got {skill_create_count} create call(s)"
    )


# ---------------------------------------------------------------------------
# Trial credit tests
# ---------------------------------------------------------------------------


async def test_provision_tenant_seeds_trial_credit_when_signup_credit_set(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """provision_tenant with signup_credit=5 seeds a trial ledger row; balance == 5."""
    result = await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id="guild-trial-1",
        signup_credit=Decimal("5.00"),
    )
    balance = await tenant_ledger.get_balance(db_session, tenant_id=result.tenant_id)
    assert balance == Decimal("5.00"), (
        "provision_tenant with signup_credit=5 must seed exactly +5 to the ledger"
    )


async def test_provision_tenant_trial_credit_idempotent_on_reprovision(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-running provision_tenant with same workspace_id does NOT double-credit."""
    for _ in range(2):
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id="guild-trial-idem",
            signup_credit=Decimal("5.00"),
        )
    tenant_id = (
        await provision_tenant(
            db_session_factory,
            platform="discord",
            workspace_id="guild-trial-idem",
            signup_credit=Decimal("5.00"),
        )
    ).tenant_id
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)
    assert balance == Decimal("5.00"), (
        "re-provisioning the same guild must not double-credit (idempotency_key: trial:{tenant_id})"
    )


async def test_provision_tenant_zero_signup_credit_seeds_no_ledger_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """signup_credit=0 (the default) must insert no ledger row."""
    result = await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id="guild-trial-zero",
        signup_credit=Decimal("0"),
    )
    balance = await tenant_ledger.get_balance(db_session, tenant_id=result.tenant_id)
    assert balance == Decimal("0"), (
        "signup_credit=0 must seed no ledger row — zero-credit guilds start at Decimal('0')"
    )
