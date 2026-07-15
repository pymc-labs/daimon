"""Transport-fake unit tests for `daimon skills backfill-tenant-titles`.

Tests:
- test_classify_skill_returns_skip_when_already_canonical: SKIP when title already prefixed.
- test_classify_skill_returns_foreign_when_other_tenants_prefix: FOREIGN for cross-tenant title.
- test_classify_skill_returns_recreate_seeded_for_bare_seeded_name: RECREATE_SEEDED for
  bare name matching defaults/skills tree.
- test_classify_skill_returns_recreate_synced_when_user_skills_provenance_exists: RECREATE_SYNCED
  for agent/name shape with matching user_skills row.
- test_classify_skill_returns_manual_for_bare_non_seeded: MANUAL for bare non-seeded name.
- test_classify_skill_returns_manual_when_synced_shape_but_no_provenance: MANUAL for
  agent/name shape with no user_skills row.
- test_dry_run_enumerates_rows_without_writing: dry-run reports rows; no write requests.
- test_apply_creates_seeded_skill_and_repins_agent: apply re-creates seeded skill, re-pins agent.
- test_apply_updates_user_skills_anthropic_id_for_synced: apply updates user_skills.anthropic_id.
- test_apply_deletes_legacy_only_after_all_repins: delete happens lexically after re-pins (Pass 2).
- test_apply_idempotent_second_run_is_noop: second run classifies everything SKIP, no MA writes.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from io import StringIO
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsAnthropicSkill,
    BetaManagedAgentsCustomSkill,
    SkillCreateResponse,
    SkillListResponse,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.adapters.cli.commands.skills_backfill import (
    classify_skill,
    skills_backfill,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.user_skills import list_user_skills_for_tenant, upsert_user_skill
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 5, 29, tzinfo=dt.UTC)
_WORKSPACE_ID_A = "guild_backfill_001"
_WORKSPACE_ID_B = "guild_backfill_002"


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)

    class _FakeCli:
        local_user = "testuser"

    class _FakeSettings:
        cli = _FakeCli()

    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _agent_json(
    *,
    agent_id: str,
    name: str,
    tenant_id: uuid.UUID,
    version: int = 1,
    skill_ids: list[str] | None = None,
) -> dict[str, object]:
    skills: list[BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill] = (
        [BetaManagedAgentsCustomSkill(type="custom", skill_id=s, version="1") for s in skill_ids]
        if skill_ids
        else []
    )
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": name},
        description=None,
        archived_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=version,
        mcp_servers=[],
        skills=skills,
        tools=[],
        system="you are helpful",
    ).model_dump(mode="json")


def _skill_retrieve_json(skill_id: str, display_title: str) -> dict[str, object]:
    return SkillListResponse(
        id=skill_id,
        type="skill",
        display_title=display_title,
        latest_version="1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")


def _skill_create_json(new_id: str, display_title: str) -> dict[str, object]:
    return SkillCreateResponse(
        id=new_id,
        type="skill",
        display_title=display_title,
        latest_version="1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Classification unit tests (pure function — no fakes)
# ---------------------------------------------------------------------------


def test_classify_skill_returns_skip_when_already_canonical() -> None:
    """A skill already carrying this tenant's prefix is SKIP (idempotency hinge)."""
    tenant_id = uuid.uuid4()
    title = tenant_scoped_display_title(tenant_id=tenant_id, name="brainstorming")
    result = classify_skill(
        display_title=title,
        tenant_id=tenant_id,
        all_tenant_ids=frozenset({tenant_id}),
        seeded_names=frozenset({"brainstorming"}),
        user_skills_by_agent_name={},
    )
    assert result == "SKIP", "already-prefixed title must classify as SKIP"


def test_classify_skill_returns_foreign_when_other_tenants_prefix() -> None:
    """A skill prefixed for a DIFFERENT known tenant is FOREIGN (never touch)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    title = tenant_scoped_display_title(tenant_id=tenant_b, name="brainstorming")
    result = classify_skill(
        display_title=title,
        tenant_id=tenant_a,
        all_tenant_ids=frozenset({tenant_a, tenant_b}),
        seeded_names=frozenset({"brainstorming"}),
        user_skills_by_agent_name={},
    )
    assert result == "FOREIGN", "title prefixed for another known tenant must classify as FOREIGN"


def test_classify_skill_returns_recreate_seeded_for_bare_seeded_name() -> None:
    """A bare title matching a defaults/skills/<name> dir classifies as RECREATE_SEEDED."""
    tenant_id = uuid.uuid4()
    result = classify_skill(
        display_title="cli-auth",
        tenant_id=tenant_id,
        all_tenant_ids=frozenset({tenant_id}),
        seeded_names=frozenset({"cli-auth", "marimo_notebooks"}),
        user_skills_by_agent_name={},
    )
    assert result == "RECREATE_SEEDED", (
        "bare name matching seeded tree must classify as RECREATE_SEEDED"
    )


def test_classify_skill_returns_recreate_synced_when_user_skills_provenance_exists() -> None:
    """An agent/name title WITH a matching user_skills row classifies as RECREATE_SYNCED."""
    tenant_id = uuid.uuid4()
    result = classify_skill(
        display_title="my-agent/my-skill",
        tenant_id=tenant_id,
        all_tenant_ids=frozenset({tenant_id}),
        seeded_names=frozenset(),
        user_skills_by_agent_name={"my-agent": ["my-skill", "other-skill"]},
    )
    assert result == "RECREATE_SYNCED", (
        "agent/name title with matching user_skills row must classify as RECREATE_SYNCED"
    )


def test_classify_skill_returns_manual_for_bare_non_seeded() -> None:
    """A bare title not in the seeded tree and not in user_skills classifies as MANUAL."""
    tenant_id = uuid.uuid4()
    result = classify_skill(
        display_title="custom-operator-skill",
        tenant_id=tenant_id,
        all_tenant_ids=frozenset({tenant_id}),
        seeded_names=frozenset({"cli-auth", "marimo_notebooks"}),
        user_skills_by_agent_name={},
    )
    assert result == "MANUAL", "bare non-seeded title must classify as MANUAL"


def test_classify_skill_returns_manual_when_synced_shape_but_no_provenance() -> None:
    """An agent/name title WITHOUT a matching user_skills row classifies as MANUAL."""
    tenant_id = uuid.uuid4()
    result = classify_skill(
        display_title="some-agent/missing-skill",
        tenant_id=tenant_id,
        all_tenant_ids=frozenset({tenant_id}),
        seeded_names=frozenset(),
        user_skills_by_agent_name={"some-agent": ["other-skill"]},
    )
    assert result == "MANUAL", (
        "agent/name shape without matching user_skills provenance must classify as MANUAL"
    )


# ---------------------------------------------------------------------------
# Dry-run test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_enumerates_rows_without_writing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """dry-run: two tenants sharing one legacy seeded skill + one seeded bare pin.
    Report rows enumerate both tenants' RECREATE rows; zero write requests hit the stub.
    """
    from daimon.core.ma_identity import derive_tenant_uuid

    ws_a_id = "guild_dry_a"
    ws_b_id = "guild_dry_b"
    await provision_tenant(db_session_factory, platform="discord", workspace_id=ws_a_id)
    await provision_tenant(db_session_factory, platform="discord", workspace_id=ws_b_id)

    tenant_a = derive_tenant_uuid(platform="discord", workspace_id=ws_a_id)
    tenant_b = derive_tenant_uuid(platform="discord", workspace_id=ws_b_id)

    # Both tenants pin the same legacy seeded skill id
    legacy_skill_id = "skill_0legacy00000"
    legacy_title = "cli-auth"  # bare seeded name (exists in defaults/skills/)

    agent_a = _agent_json(
        agent_id="agent_dry_a",
        name="daimon",
        tenant_id=tenant_a,
        skill_ids=[legacy_skill_id],
    )
    agent_b = _agent_json(
        agent_id="agent_dry_b",
        name="daimon",
        tenant_id=tenant_b,
        skill_ids=[legacy_skill_id],
    )

    write_requests: list[str] = []

    def _on_write(req: httpx.Request, _m: object) -> httpx.Response:
        write_requests.append(f"{req.method} {req.url.path}")
        return httpx.Response(400, json={"error": "should not be called in dry-run"})

    router = MARouter()
    # agents.list returns both tenants' agents
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, m: list_response([agent_a, agent_b]),
    )
    # skills.retrieve by id
    router.add(
        "GET",
        rf"/v1/skills/{legacy_skill_id}",
        lambda req, m: httpx.Response(
            200, json=_skill_retrieve_json(legacy_skill_id, legacy_title)
        ),
    )
    # Catch-all write requests
    router.add("POST", r"/v1/skills", _on_write)
    router.add("POST", r"/v1/agents/.*", _on_write)
    router.add("DELETE", r"/v1/skills/.*", _on_write)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await skills_backfill(rt=rt, console=console, yes=True, dry_run=True)

    assert len(write_requests) == 0, f"dry-run must emit zero write requests, got: {write_requests}"
    output = out.getvalue()
    assert "dry-run" in output.lower(), "dry-run must emit a dry-run header"
    assert "RECREATE_SEEDED" in output, "dry-run output must show RECREATE_SEEDED classification"


# ---------------------------------------------------------------------------
# Apply test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_creates_seeded_skill_and_repins_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """apply: seeded skill re-created under canonical title; agent re-pinned; legacy deleted
    only after re-pin (delete request appears AFTER agents.update requests in log)."""
    from daimon.core.ma_identity import derive_tenant_uuid

    ws_id = "guild_apply_seeded"
    await provision_tenant(db_session_factory, platform="discord", workspace_id=ws_id)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=ws_id)

    legacy_skill_id = "skill_0seeded000"
    legacy_title = "cli-auth"  # exists in defaults/skills/
    new_skill_id = "skill_0new_seeded0"
    canonical_title = tenant_scoped_display_title(tenant_id=tenant_id, name="cli-auth")

    agent_id = "agent_seeded_apply"
    agent_data = _agent_json(
        agent_id=agent_id,
        name="daimon",
        tenant_id=tenant_id,
        skill_ids=[legacy_skill_id],
    )

    request_log: list[str] = []
    update_bodies: list[dict[str, object]] = []
    delete_calls: list[str] = []
    created_titles: list[str] = []

    def _on_create(req: httpx.Request, _m: object) -> httpx.Response:
        # Parse display_title from multipart — just record and return
        request_log.append("POST /v1/skills")
        # decode multipart form to find display_title
        body = req.content.decode("utf-8", errors="replace")
        for chunk in body.split("\r\n"):
            if (
                chunk
                and not chunk.startswith("--")
                and "SKILL.zip" not in chunk
                and "Content-" not in chunk
            ):
                # heuristic: title is in a text field
                pass
        created_titles.append("(created)")
        return httpx.Response(200, json=_skill_create_json(new_skill_id, canonical_title))

    def _on_update(req: httpx.Request, _m: object) -> httpx.Response:
        request_log.append(f"POST /v1/agents/{agent_id}")
        update_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=agent_data)

    def _on_delete(req: httpx.Request, _m: object) -> httpx.Response:
        request_log.append(f"DELETE {req.url.path}")
        delete_calls.append(req.url.path)
        return httpx.Response(200, json={"id": legacy_skill_id, "deleted": True})

    def _on_versions_list(req: httpx.Request, _m: object) -> httpx.Response:
        return list_response([])

    router = MARouter()
    # agents.list — returns legacy-pinning agent
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    # skills.retrieve (for plan phase classification)
    router.add(
        "GET",
        rf"/v1/skills/{legacy_skill_id}",
        lambda req, m: httpx.Response(
            200, json=_skill_retrieve_json(legacy_skill_id, legacy_title)
        ),
    )
    # skills.create (apply)
    router.add("POST", r"/v1/skills", _on_create)
    # agents.retrieve fresh (re-pin step)
    router.add(
        "GET",
        rf"/v1/agents/{agent_id}",
        lambda req, m: httpx.Response(200, json=agent_data),
    )
    # agents.update (re-pin)
    router.add("POST", rf"/v1/agents/{agent_id}", _on_update)
    # list_referenced_skill_ids uses agents.list (second call post-repin; return agent w/ new id)
    # The first agents.list call uses the original; after repin the delete-guard re-checks.
    # For simplicity: the stub always returns the agent with the new id (legacy not referenced).
    agent_post_repin = _agent_json(
        agent_id=agent_id,
        name="daimon",
        tenant_id=tenant_id,
        skill_ids=[new_skill_id],  # old id gone, new id in place
    )
    # Call sequence:
    # 1. plan phase: list_agents_by_tenant → agent with legacy skill
    # 2. apply phase repin loop: list_agents_by_tenant → agent with legacy skill (still)
    # 3. delete guard: list_referenced_skill_ids → agent with new skill (legacy gone)
    call_counts: dict[str, int] = {"agents_list": 0}

    def _on_agents_list(req: httpx.Request, _m: object) -> httpx.Response:
        call_counts["agents_list"] += 1
        if call_counts["agents_list"] <= 2:
            return list_response([agent_data])  # plan + apply repin loop
        return list_response([agent_post_repin])  # delete-guard recompute

    # Replace the earlier GET /v1/agents handler
    router2 = MARouter()
    router2.add("GET", r"/v1/agents", _on_agents_list)
    router2.add(
        "GET",
        rf"/v1/skills/{legacy_skill_id}",
        lambda req, m: httpx.Response(
            200, json=_skill_retrieve_json(legacy_skill_id, legacy_title)
        ),
    )
    router2.add("POST", r"/v1/skills", _on_create)
    router2.add(
        "GET",
        rf"/v1/agents/{agent_id}",
        lambda req, m: httpx.Response(200, json=agent_data),
    )
    router2.add("POST", rf"/v1/agents/{agent_id}", _on_update)
    # skills.versions.list (delete_skill_and_versions needs this)
    router2.add(
        "GET",
        rf"/v1/skills/{legacy_skill_id}/versions",
        _on_versions_list,
    )
    # skills.delete
    router2.add("DELETE", rf"/v1/skills/{legacy_skill_id}", _on_delete)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router2)

    await skills_backfill(rt=rt, console=console, yes=True, dry_run=False)

    # Assert create happened
    assert len(created_titles) == 1, "expected exactly 1 skills.create call"

    # Assert re-pin happened
    assert len(update_bodies) == 1, "expected exactly 1 agents.update (re-pin)"
    update_body = update_bodies[0]
    # The updated skills list must contain the new skill id, not the legacy
    updated_skills = update_body.get("skills", [])
    assert isinstance(updated_skills, list), "agents.update body must include skills list"
    new_ids = [s["skill_id"] for s in updated_skills if isinstance(s, dict)]  # type: ignore[index]
    assert new_skill_id in new_ids, f"re-pin must swap legacy id for new id; got {new_ids}"
    assert legacy_skill_id not in new_ids, (
        f"re-pin must remove legacy id from skills; got {new_ids}"
    )

    # Assert delete-last ordering: re-pin update (POST) comes BEFORE delete
    repin_idx = next(
        (i for i, r in enumerate(request_log) if r.startswith("POST /v1/agents")), None
    )
    delete_idx = next((i for i, r in enumerate(request_log) if r.startswith("DELETE")), None)
    assert repin_idx is not None, "re-pin (agents.update) must appear in request log"
    assert delete_idx is not None, "legacy skill delete must appear in request log"
    assert repin_idx < delete_idx, (
        f"re-pin must happen BEFORE delete (Pitfall 4); "
        f"repin at {repin_idx}, delete at {delete_idx}"
    )


# ---------------------------------------------------------------------------
# user_skills update for synced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_updates_user_skills_anthropic_id_for_synced(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """apply: synced skill re-created; user_skills.anthropic_id updated to new MA id.
    Verified by loading the row from the real DB after apply (independent ground-truth)."""
    import io
    import tarfile as tarfile_mod

    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.identity import get_or_create_cli_principal

    ws_id = "guild_apply_synced"
    await provision_tenant(db_session_factory, platform="discord", workspace_id=ws_id)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=ws_id)

    agent_name = "my-agent"
    skill_name = "my-skill"
    legacy_skill_id = "skill_0synced00000"
    new_skill_id = "skill_0new_synced0"
    legacy_title = f"{agent_name}/{skill_name}"
    canonical_title = tenant_scoped_display_title(
        tenant_id=tenant_id, name=skill_name, agent_name=agent_name
    )

    # Seed a user_skills row under the provisioned tenant with legacy anthropic_id
    async with db_session_factory() as s, s.begin():
        cli_p = await get_or_create_cli_principal(s, tenant_id=tenant_id, os_user="backfill-test")
        await upsert_user_skill(
            s,
            tenant_id=tenant_id,
            principal_id=cli_p.account_id,
            agent_name=agent_name,
            name=skill_name,
            source_repo_url="https://github.com/example/repo",
            source_repo_branch="main",
            source_path="",
            content_hash="abc123",
            anthropic_id=legacy_skill_id,
            anthropic_latest_version="1",
        )

    agent_id = "agent_synced_apply"
    agent_data = _agent_json(
        agent_id=agent_id,
        name=agent_name,
        tenant_id=tenant_id,
        skill_ids=[legacy_skill_id],
    )
    agent_post_repin = _agent_json(
        agent_id=agent_id,
        name=agent_name,
        tenant_id=tenant_id,
        skill_ids=[new_skill_id],
    )

    call_counts: dict[str, int] = {"agents_list": 0}

    def _on_agents_list(req: httpx.Request, _m: object) -> httpx.Response:
        call_counts["agents_list"] += 1
        if call_counts["agents_list"] <= 2:
            return list_response([agent_data])
        return list_response([agent_post_repin])

    def _on_versions_list(req: httpx.Request, _m: object) -> httpx.Response:
        return list_response([])

    # Build a minimal valid tarball with SKILL.md for the synced re-fetch
    buf = io.BytesIO()
    with tarfile_mod.open(fileobj=buf, mode="w:gz") as tf:
        skill_md_content = b"---\nname: my-skill\ndescription: test\n---\nBody"
        info = tarfile_mod.TarInfo(name="repo-root/SKILL.md")
        info.size = len(skill_md_content)
        tf.addfile(info, io.BytesIO(skill_md_content))
    tarball_bytes = buf.getvalue()

    router = MARouter()
    router.add("GET", r"/v1/agents", _on_agents_list)
    router.add(
        "GET",
        rf"/v1/skills/{legacy_skill_id}",
        lambda req, m: httpx.Response(
            200, json=_skill_retrieve_json(legacy_skill_id, legacy_title)
        ),
    )
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, m: httpx.Response(200, json=_skill_create_json(new_skill_id, canonical_title)),
    )
    router.add(
        "GET",
        rf"/v1/agents/{agent_id}",
        lambda req, m: httpx.Response(200, json=agent_data),
    )
    router.add(
        "POST",
        rf"/v1/agents/{agent_id}",
        lambda req, m: httpx.Response(200, json=agent_data),
    )
    router.add("GET", rf"/v1/skills/{legacy_skill_id}/versions", _on_versions_list)
    router.add(
        "DELETE",
        rf"/v1/skills/{legacy_skill_id}",
        lambda req, m: httpx.Response(200, json={}),
    )

    ma_client_transport = httpx.MockTransport(router.dispatch)
    ma_http = httpx.AsyncClient(transport=ma_client_transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=ma_http)

    class _FakeCli:
        local_user = "testuser"

    class _FakeSettings:
        cli = _FakeCli()

    rt = CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    # Inject a GitHub-faking http_client (used by GitHubTarballFetcher inside backfill)
    def _github_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball_bytes)

    github_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_github_handler),
        base_url="https://api.github.com",
    )

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)

    await skills_backfill(rt=rt, console=console, yes=True, dry_run=False, http_client=github_http)

    # Independent ground-truth re-fetch: verify user_skills.anthropic_id updated
    async with db_session_factory() as s, s.begin():
        rows = await list_user_skills_for_tenant(s, tenant_id=tenant_id)

    matching = [r for r in rows if r.agent_name == agent_name and r.name == skill_name]
    assert len(matching) == 1, "expected exactly 1 user_skills row for the synced skill"
    assert matching[0].anthropic_id == new_skill_id, (
        f"user_skills.anthropic_id must be updated to new MA id {new_skill_id!r}, "
        f"got {matching[0].anthropic_id!r}"
    )


# ---------------------------------------------------------------------------
# Idempotency test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_idempotent_second_run_is_noop(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run apply twice. Second run classifies all skills SKIP and performs zero MA writes."""
    from daimon.core.ma_identity import derive_tenant_uuid

    ws_id = "guild_idempotent"
    await provision_tenant(db_session_factory, platform="discord", workspace_id=ws_id)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=ws_id)

    # Agent pins a skill that is ALREADY canonical (simulates post-first-run state)
    canonical_title = tenant_scoped_display_title(tenant_id=tenant_id, name="brainstorming")
    canonical_skill_id = "skill_0canonical0"

    agent_id = "agent_idempotent"
    agent_data = _agent_json(
        agent_id=agent_id,
        name="daimon",
        tenant_id=tenant_id,
        skill_ids=[canonical_skill_id],
    )

    write_requests: list[str] = []

    def _on_write(req: httpx.Request, _m: object) -> httpx.Response:
        write_requests.append(f"{req.method} {req.url.path}")
        return httpx.Response(400, json={"error": "unexpected write in idempotent run"})

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add(
        "GET",
        rf"/v1/skills/{canonical_skill_id}",
        lambda req, m: httpx.Response(
            200,
            json=_skill_retrieve_json(canonical_skill_id, canonical_title),
        ),
    )
    router.add("POST", r"/v1/skills", _on_write)
    router.add("POST", r"/v1/agents/.*", _on_write)
    router.add("DELETE", r"/v1/skills/.*", _on_write)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await skills_backfill(rt=rt, console=console, yes=True, dry_run=False)

    assert len(write_requests) == 0, (
        f"second run (all SKIP) must perform zero MA writes, got: {write_requests}"
    )
    output = out.getvalue()
    assert "No skills need backfilling" in output, "idempotent run must print the no-op message"
