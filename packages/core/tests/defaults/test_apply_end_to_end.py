from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsModelConfig,
    SkillListResponse,
)
from daimon.core.defaults import apply_defaults
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    tenant_scoped_display_title,
)
from daimon.core.defaults.report import Action
from daimon.core.errors import DefaultsError
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    json_body,
    list_response,
)
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CREATED_AT = datetime(2026, 4, 21, 0, 0, 0, tzinfo=UTC)
_AGENT_MODEL = BetaManagedAgentsModelConfig(id="claude-opus-4-7")


def _write_tree(root: Path) -> None:
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)

    (root / "agents" / "daimon.yaml").write_text("name: daimon\nmodel: claude-sonnet-4-6\n")
    (root / "environments" / "default.yaml").write_text("name: default\n")


def _full_router(
    *,
    skills: list[dict[str, Any]] | None = None,
    environments: list[dict[str, Any]] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> MARouter:
    """Build a router with GET list routes for all three resource kinds."""
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(list(skills or [])))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response(list(environments or [])))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response(list(agents or [])))
    return router


async def _get_tenant_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Helper to retrieve the bootstrapped tenant_id as a string."""
    from daimon.core._models import Tenant
    from sqlalchemy import select

    async with session_factory() as s:
        tenant = (await s.execute(select(Tenant).limit(1))).scalar_one()
    return str(tenant.id)


async def test_apply_creates_all_on_fresh_db(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _write_tree(tmp_path)
    router = _full_router()
    agent_payload: dict[str, Any] = {}

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

    def on_agent_create(req: httpx.Request, _m: object) -> httpx.Response:
        agent_payload.update(json.loads(req.content))
        return httpx.Response(
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
        )

    router.add("POST", r"/v1/agents", on_agent_create)
    client = build_fake_anthropic_http(router.dispatch)

    report = await apply_defaults(
        db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
    )

    assert report.skills == []
    assert [o.action for o in report.environments] == [Action.CREATED]
    assert [o.action for o in report.agents] == [Action.CREATED]
    assert agent_payload.get("skills") == []


async def test_apply_second_run_skips_everything(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _write_tree(tmp_path)

    # --- first run: create everything ---
    router1 = _full_router()
    router1.add(
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
    router1.add(
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
    first = await apply_defaults(
        db_session_factory,
        build_fake_anthropic_http(router1.dispatch),
        tmp_path,
        dry_run=False,
        run_preflight=False,
    )
    assert first.skills == []
    assert [o.action for o in first.environments] == [Action.CREATED]
    assert [o.action for o in first.agents] == [Action.CREATED]

    # --- second run: lists return the created resources; no writes expected ---
    tenant_id_str = await _get_tenant_id(db_session_factory)
    existing_env = BetaEnvironment(
        id="env_1",
        type="environment",
        name="default",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "default",
        },
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")
    existing_agent = BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name="daimon",
        model=_AGENT_MODEL,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "daimon",
        },
        description=None,
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")
    # No write handlers registered — router raises if anything tries to write.
    # 2-state design: always update when resource found on MA.
    router2 = _full_router(
        environments=[existing_env],
        agents=[existing_agent],
    )
    router2.add(
        "POST",
        r"/v1/environments/env_1",
        lambda req, _m: httpx.Response(200, json=existing_env),
    )
    router2.add(
        "POST",
        r"/v1/agents/ag_1",
        lambda req, _m: httpx.Response(200, json=existing_agent),
    )
    client2 = build_fake_anthropic_http(router2.dispatch)

    second = await apply_defaults(
        db_session_factory, client2, tmp_path, dry_run=False, run_preflight=False
    )

    assert second.skills == []
    assert [o.action for o in second.environments] == [Action.UPDATED]
    assert [o.action for o in second.agents] == [Action.UPDATED]


async def test_apply_hard_fails_when_agent_references_unknown_skill(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "daimon.yaml").write_text(
        "name: daimon\nmodel: m\nskills:\n  - type: custom\n    skill_id: missing\n"
    )
    (tmp_path / "environments").mkdir(parents=True)
    (tmp_path / "skills").mkdir(parents=True)
    # Validation (step 3) fires before any network call; router can be empty.
    client = build_fake_anthropic_http(MARouter().dispatch)

    with pytest.raises(DefaultsError, match="unknown skill"):
        await apply_defaults(
            db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
        )


async def test_apply_dry_run_reports_actions_without_writes(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _write_tree(tmp_path)
    # Dry run still checks lists (to determine CREATED vs ADOPTED) but must not write.
    router = _full_router()
    # No create handlers — router raises if anything tries to write.
    client = build_fake_anthropic_http(router.dispatch)

    report = await apply_defaults(db_session_factory, client, tmp_path, dry_run=True)

    assert report.skills == []
    assert [o.action for o in report.environments] == [Action.CREATED], (
        "fresh dry run reports environment would be created"
    )
    assert [o.action for o in report.agents] == [Action.CREATED], (
        "fresh dry run reports agent would be created"
    )


async def test_apply_without_config_yaml_reports_empty_system_config(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No config.yaml -> system_config is always empty (no seeding behavior)."""
    _write_tree(tmp_path)  # no config.yaml
    router = _full_router()
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
    client = build_fake_anthropic_http(router.dispatch)

    report = await apply_defaults(
        db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
    )

    assert report.system_config == []


async def test_apply_reports_failed_without_traceback_when_reconcile_raises_daimon_error(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected failures (DaimonError / APIError): WARNING log, no exc_info,
    FAILED outcome carrying str(err). Apply must continue past the failing
    resource so later resources still run."""
    import structlog.testing
    from daimon.core.defaults import _reconcile as reconcile_mod
    from daimon.core.errors import DefaultsError

    _write_tree(tmp_path)

    async def boom(*args: object, **kwargs: object) -> object:
        raise DefaultsError("environment blew up on purpose")

    monkeypatch.setattr(reconcile_mod, "reconcile_environment", boom)

    router = _full_router()
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
    client = build_fake_anthropic_http(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        report = await apply_defaults(
            db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
        )

    assert [o.action for o in report.environments] == [Action.FAILED]
    assert report.environments[0].error == "environment blew up on purpose"

    assert [o.action for o in report.agents] == [Action.CREATED]

    matching = [
        r
        for r in logs
        if r.get("event") == "defaults.reconcile_failed" and r.get("kind") == "environment"
    ]
    assert len(matching) == 1, f"expected one environment WARNING log, got {matching}"
    assert matching[0]["log_level"] == "warning"
    assert matching[0]["name"] == "default"
    assert matching[0]["error"] == "environment blew up on purpose"
    assert "exc_info" not in matching[0], "expected failures should not log a traceback"


async def test_apply_reports_failed_with_scrubbed_error_when_reconcile_raises_typeerror(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected exceptions (anything not APIError/DaimonError) must not
    abort the whole apply. They surface as FAILED with a scrubbed error
    field (type name only); full traceback goes to stderr via _log.exception."""
    import structlog.testing
    from daimon.core.defaults import _reconcile as reconcile_mod

    _write_tree(tmp_path)

    async def boom(*args: object, **kwargs: object) -> object:
        raise TypeError("internal shape bug")

    monkeypatch.setattr(reconcile_mod, "reconcile_environment", boom)

    router = _full_router()
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
    client = build_fake_anthropic_http(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        report = await apply_defaults(
            db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
        )

    assert [o.action for o in report.environments] == [Action.FAILED]
    assert report.environments[0].error == "internal error: TypeError"

    assert [o.action for o in report.agents] == [Action.CREATED]

    matching = [r for r in logs if r.get("event") == "defaults.reconcile_unexpected"]
    assert len(matching) == 1, f"expected one ERROR log, got {matching}"
    assert matching[0]["log_level"] == "error"
    assert matching[0]["kind"] == "environment"
    assert matching[0]["name"] == "default"
    assert matching[0].get("exc_info") is True, "unexpected exceptions must log with traceback"


async def test_apply_sweeps_previously_seeded_brainstorming_skill(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Migration path: a workspace that previously ran `defaults apply` with
    the seeded brainstorming skill must, on the next apply against the new
    skill-less tree, UPDATE the daimon agent with skills=[] and sweep the
    orphaned brainstorming skill (MA delete)."""
    _write_tree(tmp_path)  # skill-less tree

    from daimon.core.defaults.provisioning import provision_tenant  # noqa: PLC0415
    from daimon.core.ma_identity import derive_tenant_uuid  # noqa: PLC0415

    await provision_tenant(db_session_factory, platform="cli", workspace_id="local")
    tenant_id = derive_tenant_uuid(platform="cli", workspace_id="local")

    tenant_id_str = str(tenant_id)
    brainstorming_title = tenant_scoped_display_title(tenant_id=tenant_id, name="brainstorming")

    existing_skill = SkillListResponse(
        id="sk_old",
        type="custom",
        display_title=brainstorming_title,
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    existing_agent = BetaManagedAgentsAgent(
        id="ag_old",
        type="agent",
        name="daimon",
        model=_AGENT_MODEL,
        metadata={
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: "daimon",
        },
        description=None,
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    router = _full_router(skills=[existing_skill], agents=[existing_agent])

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
        "GET",
        r"/v1/skills/sk_old/versions",
        lambda req, _m: list_response([]),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_old",
        lambda req, _m: httpx.Response(200, json={"id": "sk_old", "deleted": True}),
    )

    update_requests: list[httpx.Request] = []

    def on_agent_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_requests.append(req)
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_old",
                type="agent",
                name="daimon",
                model=_AGENT_MODEL,
                metadata={
                    MA_METADATA_KEY_TENANT: tenant_id_str,
                    MA_METADATA_KEY_NAME: "daimon",
                },
                description=None,
                created_at=_CREATED_AT,
                updated_at=_CREATED_AT,
                version=2,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/agents/ag_old", on_agent_update)

    client = build_fake_anthropic_http(router.dispatch)

    report = await apply_defaults(
        db_session_factory, client, tmp_path, dry_run=False, run_preflight=False
    )

    assert [o.action for o in report.agents] == [Action.UPDATED]
    assert len(update_requests) == 1, "agent update must be called exactly once"
    body = json_body(update_requests[0])
    assert body["skills"] == [], "update payload must carry skills=[] so MA drops the old binding"
    assert [o.action for o in report.skills] == [Action.DELETED], (
        "brainstorming must be swept in this migration run"
    )
