"""Transport-fake unit tests for `daimon agents rekey-guild-ownership`.

Tests:
- test_rekey_updates_user_owned_guild_agent: user-owned agent re-keyed to guild
  account; full metadata (daimon_name/daimon_managed/daimon_spec_hash) preserved.
- test_rekey_skips_already_guild_owned: idempotent — already guild-owned skipped.
- test_rekey_skips_system_agent_no_account: system agent (no owner) left untouched.
- test_rekey_dry_run_writes_nothing: --dry-run reports without writing.
- test_rekey_renames_collision_keeps_canonical: two agents with the same name in one
  tenant; max-created_at keeps the bare name; the other becomes <name>-2.
- test_rekey_dry_run_reports_rename_without_writing: --dry-run with collision shows
  rename without calling agents.update.
- test_rekey_renames_when_name_already_guild_owned: a personal-stamped agent whose
  name is already held by a guild-owned agent gets a suffix, never the bare name.
- test_rekey_suffix_never_collides_with_later_bare_name: an assigned suffix must
  not collide with another candidate that keeps that literal name.
- test_rekey_processes_tenant_once_despite_duplicate_workspace_rows: a tenant with
  two workspace rows is enumerated once — no double update, no self-collision.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from io import StringIO
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.adapters.cli.commands.agents import agents_rekey
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import derive_guild_account_uuid, provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = dt.datetime(2026, 5, 29, tzinfo=dt.UTC)
_WORKSPACE_ID = "guild_001"


def _tenant_and_account() -> tuple[uuid.UUID, uuid.UUID]:
    """Return the (tenant_id, guild_account_id) pair for _WORKSPACE_ID."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=_WORKSPACE_ID)
    guild_account = derive_guild_account_uuid(tenant_id)
    return tenant_id, guild_account


def _user_account_id() -> uuid.UUID:
    """A distinct per-user account (≠ guild account)."""
    return uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _agent_json(
    *,
    agent_id: str,
    name: str,
    version: int = 1,
    metadata: dict[str, str],
) -> dict[str, object]:
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        version=version,
        mcp_servers=[],
        skills=[],
        tools=[],
        system="you are helpful",
    ).model_dump(mode="json")


class _FakeCli:
    local_user = "testuser"


class _FakeMcp:
    public_url = None


class _FakeSettings:
    cli = _FakeCli()
    mcp = _FakeMcp()


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    return CliRuntime(
        settings=cast(Settings, _FakeSettings()),
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rekey_updates_user_owned_guild_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """user-owned guild agent re-keyed to guild account; full metadata preserved."""
    tenant_id, guild_account = _tenant_and_account()
    user_account = _user_account_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_id = "agent_abc123"
    agent_version = 5
    agent_metadata = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "my-agent",
        "daimon_account": str(user_account),
        "daimon_managed": "true",
        "daimon_spec_hash": "deadbeef12345678",
    }
    agent_data = _agent_json(
        agent_id=agent_id,
        name="my-agent",
        version=agent_version,
        metadata=agent_metadata,
    )

    update_bodies: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies.append(json.loads(req.content))
        return httpx.Response(200, json=agent_data)

    def on_retrieve(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("GET", rf"/v1/agents/{agent_id}", on_retrieve)
    router.add("POST", rf"/v1/agents/{agent_id}", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_bodies) == 1, (
        "user-owned guild agent re-keyed to guild account; expected 1 agents.update call"
    )
    body = update_bodies[0]
    assert body.get("version") == agent_version, (
        "agents.update must include version=agent.version (Pitfall 3)"
    )
    raw_meta = body.get("metadata")
    assert isinstance(raw_meta, dict), "update body must include metadata dict"
    sent_meta = cast(dict[str, object], raw_meta)
    assert sent_meta.get("daimon_account") == str(guild_account), (
        "daimon_account must be re-keyed to guild account"
    )
    assert sent_meta.get("daimon_name") == "my-agent", (
        "daimon_name must be preserved in full-dict update (Pitfall 3)"
    )
    assert sent_meta.get("daimon_managed") == "true", (
        "daimon_managed must be preserved in full-dict update (Pitfall 3)"
    )
    assert sent_meta.get("daimon_spec_hash") == "deadbeef12345678", (
        "daimon_spec_hash must be preserved in full-dict update (Pitfall 3)"
    )


@pytest.mark.asyncio
async def test_rekey_skips_already_guild_owned(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """idempotent — already guild-owned skipped."""
    tenant_id, guild_account = _tenant_and_account()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    # Agent already points at the derived guild account.
    agent_data = _agent_json(
        agent_id="agent_already",
        name="already-guild-owned",
        version=2,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "already-guild-owned",
            "daimon_account": str(guild_account),
        },
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/agent_already", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_calls) == 0, (
        "idempotent — already guild-owned skipped; no agents.update expected"
    )


@pytest.mark.asyncio
async def test_rekey_skips_system_agent_no_account(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """system agent (no owner) left untouched."""
    tenant_id, _guild_account = _tenant_and_account()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    # System agent — no daimon_account key.
    agent_data = _agent_json(
        agent_id="agent_system",
        name="system-agent",
        version=1,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "system-agent",
        },
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/agent_system", on_update)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_calls) == 0, (
        "system agent (no owner) left untouched; no agents.update expected"
    )


@pytest.mark.asyncio
async def test_rekey_dry_run_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """--dry-run reports without writing."""
    tenant_id, _guild_account = _tenant_and_account()
    user_account = _user_account_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    agent_data = _agent_json(
        agent_id="agent_dryrun",
        name="dry-run-target",
        version=3,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "dry-run-target",
            "daimon_account": str(user_account),
        },
    )

    update_calls: list[str] = []

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=agent_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent_data]))
    router.add("POST", r"/v1/agents/agent_dryrun", on_update)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=True)

    assert len(update_calls) == 0, "--dry-run reports without writing; no agents.update expected"
    output = out.getvalue()
    assert "dry-run" in output.lower(), (
        "--dry-run must emit a report row indicating what would change"
    )


@pytest.mark.asyncio
async def test_rekey_renames_collision_keeps_canonical(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two distinct agents with the same name in one tenant are both user-stamped.
    After re-key, the canonical (max created_at) keeps the bare name and the
    older one becomes <name>-2. Both are stamped the guild account."""
    tenant_id, guild_account = _tenant_and_account()
    user_account_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    user_account_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    # canonical: created LATER (max created_at) — keeps the bare name
    canonical_id = "agent_canonical"
    canonical_data = _agent_json(
        agent_id=canonical_id,
        name="daimon-copy",
        version=3,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "daimon-copy",
            "daimon_account": str(user_account_a),
        },
    )
    # Override created_at so canonical is clearly newer.
    canonical_data = dict(canonical_data)
    canonical_data["created_at"] = "2026-05-29T12:00:00+00:00"

    # older: created EARLIER — will be renamed to daimon-copy-2
    older_id = "agent_older"
    older_data = _agent_json(
        agent_id=older_id,
        name="daimon-copy",
        version=2,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "daimon-copy",
            "daimon_account": str(user_account_b),
        },
    )
    older_data = dict(older_data)
    older_data["created_at"] = "2026-05-01T00:00:00+00:00"

    update_bodies: dict[str, dict[str, object]] = {}  # agent_id → last update body

    def on_retrieve_canonical(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(200, json=canonical_data)

    def on_retrieve_older(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(200, json=older_data)

    def on_update_canonical(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies[canonical_id] = json.loads(req.content)
        return httpx.Response(200, json=canonical_data)

    def on_update_older(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies[older_id] = json.loads(req.content)
        return httpx.Response(200, json=older_data)

    router = MARouter()
    # list_agents_by_tenant iterates via pagination; return both agents.
    # list_response returns canonical first (higher created_at sorts first in the
    # list — the command itself re-sorts by ordering in to_rekey).
    router.add("GET", r"/v1/agents", lambda req, m: list_response([canonical_data, older_data]))
    router.add("GET", rf"/v1/agents/{canonical_id}", on_retrieve_canonical)
    router.add("GET", rf"/v1/agents/{older_id}", on_retrieve_older)
    router.add("POST", rf"/v1/agents/{canonical_id}", on_update_canonical)
    router.add("POST", rf"/v1/agents/{older_id}", on_update_older)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_bodies) == 2, (
        "both agents must be re-keyed (two agents.update calls expected)"
    )

    # canonical: keeps "daimon-copy", gets guild account
    canon_meta = cast(dict[str, object], update_bodies[canonical_id].get("metadata"))
    assert canon_meta is not None, "canonical update must include metadata"
    assert canon_meta.get("daimon_name") == "daimon-copy", (
        "canonical (max created_at) must keep the bare name"
    )
    assert canon_meta.get("daimon_account") == str(guild_account), (
        "canonical must be re-keyed to guild account"
    )
    assert update_bodies[canonical_id].get("name") == "daimon-copy", (
        "canonical agent.name must also remain 'daimon-copy'"
    )

    # older: renamed to "daimon-copy-2", gets guild account
    older_meta = cast(dict[str, object], update_bodies[older_id].get("metadata"))
    assert older_meta is not None, "older update must include metadata"
    assert older_meta.get("daimon_name") == "daimon-copy-2", (
        "older agent (min created_at) must be renamed to daimon-copy-2 in metadata"
    )
    assert older_meta.get("daimon_account") == str(guild_account), (
        "older must also be re-keyed to guild account"
    )
    assert update_bodies[older_id].get("name") == "daimon-copy-2", (
        "older agent.name must be updated to 'daimon-copy-2'"
    )


@pytest.mark.asyncio
async def test_rekey_dry_run_reports_rename_without_writing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """--dry-run with a collision pair reports the rename (new_name visible in
    output) without calling agents.update."""
    tenant_id, _guild_account = _tenant_and_account()
    user_account_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")
    user_account_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000004")

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    canonical_data = _agent_json(
        agent_id="agent_dry_c",
        name="shared-name",
        version=1,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "shared-name",
            "daimon_account": str(user_account_a),
        },
    )
    canonical_data = dict(canonical_data)
    canonical_data["created_at"] = "2026-05-29T12:00:00+00:00"

    older_data = _agent_json(
        agent_id="agent_dry_o",
        name="shared-name",
        version=1,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "shared-name",
            "daimon_account": str(user_account_b),
        },
    )
    older_data = dict(older_data)
    older_data["created_at"] = "2026-05-01T00:00:00+00:00"

    update_calls: list[str] = []

    def on_update(req: httpx.Request, m: object) -> httpx.Response:
        update_calls.append(req.url.path)
        return httpx.Response(200, json=canonical_data)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([canonical_data, older_data]))
    router.add("POST", r"/v1/agents/agent_dry_c", on_update)
    router.add("POST", r"/v1/agents/agent_dry_o", on_update)

    out = StringIO()
    console = Console(file=out, force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=True)

    assert len(update_calls) == 0, (
        "--dry-run must not call agents.update even when collision renames are pending"
    )
    output = out.getvalue()
    assert "dry-run" in output.lower(), "--dry-run must emit a dry-run header"
    assert "shared-name-2" in output, (
        "--dry-run must show the renamed new_name ('shared-name-2') in the report"
    )


@pytest.mark.asyncio
async def test_rekey_renames_when_name_already_guild_owned(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One already-guild-owned 'demo' (skipped) plus one personal-stamped 'demo'
    (re-keyed): the personal one must be renamed to demo-2, NOT keep the bare
    name — two guild-stamped agents named 'demo' would otherwise coexist."""
    tenant_id, guild_account = _tenant_and_account()
    user_account = _user_account_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    guild_owned_data = _agent_json(
        agent_id="agent_guildowned",
        name="demo",
        version=1,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "demo",
            "daimon_account": str(guild_account),
        },
    )
    personal_data = _agent_json(
        agent_id="agent_personal",
        name="demo",
        version=1,
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "demo",
            "daimon_account": str(user_account),
        },
    )

    update_bodies: dict[str, dict[str, object]] = {}

    def on_update_guild(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies["agent_guildowned"] = json.loads(req.content)
        return httpx.Response(200, json=guild_owned_data)

    def on_update_personal(req: httpx.Request, _m: object) -> httpx.Response:
        update_bodies["agent_personal"] = json.loads(req.content)
        return httpx.Response(200, json=personal_data)

    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda req, m: list_response([guild_owned_data, personal_data])
    )
    router.add(
        "GET", r"/v1/agents/agent_personal", lambda req, m: httpx.Response(200, json=personal_data)
    )
    router.add("POST", r"/v1/agents/agent_guildowned", on_update_guild)
    router.add("POST", r"/v1/agents/agent_personal", on_update_personal)

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert "agent_guildowned" not in update_bodies, (
        "already-guild-owned agent must be skipped, not updated"
    )
    assert "agent_personal" in update_bodies, "personal-stamped agent must be re-keyed"
    body = update_bodies["agent_personal"]
    assert body.get("name") == "demo-2", (
        "personal-stamped agent whose name is already guild-owned must be renamed "
        f"to demo-2, got {body.get('name')!r}"
    )
    meta = cast(dict[str, object], body.get("metadata"))
    assert meta.get("daimon_name") == "demo-2", (
        "renamed agent's metadata daimon_name must match the suffixed name"
    )
    assert meta.get("daimon_account") == str(guild_account), (
        "renamed agent must still be re-keyed to the guild account"
    )


@pytest.mark.asyncio
async def test_rekey_suffix_never_collides_with_later_bare_name(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Candidates 'demo' (newest), 'demo-2', 'demo' (oldest), all personal-stamped:
    the older 'demo' must NOT be assigned 'demo-2' (that bare name belongs to the
    literal demo-2 candidate) — it gets demo-3. All final names unique."""
    tenant_id, _guild_account = _tenant_and_account()
    user_account = _user_account_id()

    await provision_tenant(db_session_factory, platform="discord", workspace_id=_WORKSPACE_ID)

    def _candidate(agent_id: str, name: str, created_at: str) -> dict[str, object]:
        data = dict(
            _agent_json(
                agent_id=agent_id,
                name=name,
                version=1,
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": name,
                    "daimon_account": str(user_account),
                },
            )
        )
        data["created_at"] = created_at
        return data

    demo_newest = _candidate("agent_demo_new", "demo", "2026-05-29T12:00:00+00:00")
    demo_2_literal = _candidate("agent_demo2", "demo-2", "2026-05-15T00:00:00+00:00")
    demo_oldest = _candidate("agent_demo_old", "demo", "2026-05-01T00:00:00+00:00")

    update_bodies: dict[str, dict[str, object]] = {}

    def _on_update(
        agent_id: str, data: dict[str, object]
    ) -> Callable[[httpx.Request, object], httpx.Response]:
        def handler(req: httpx.Request, _m: object) -> httpx.Response:
            update_bodies[agent_id] = json.loads(req.content)
            return httpx.Response(200, json=data)

        return handler

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, m: list_response([demo_oldest, demo_2_literal, demo_newest]),
    )
    for agent_id, data in (
        ("agent_demo_new", demo_newest),
        ("agent_demo2", demo_2_literal),
        ("agent_demo_old", demo_oldest),
    ):
        router.add(
            "GET",
            rf"/v1/agents/{agent_id}",
            lambda req, m, _d=data: httpx.Response(200, json=_d),
        )
        router.add("POST", rf"/v1/agents/{agent_id}", _on_update(agent_id, data))

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    rt = _build_rt(db_session_factory, router)

    await agents_rekey(rt=rt, console=console, yes=True, dry_run=False)

    assert len(update_bodies) == 3, "all three personal-stamped agents must be re-keyed"
    assert update_bodies["agent_demo_new"].get("name") == "demo", (
        "newest 'demo' (max created_at) must keep the bare name"
    )
    assert update_bodies["agent_demo2"].get("name") == "demo-2", (
        "the literal 'demo-2' candidate must keep its bare name"
    )
    assert update_bodies["agent_demo_old"].get("name") == "demo-3", (
        "older 'demo' must skip demo-2 (claimed by the literal demo-2 candidate) "
        f"and take demo-3, got {update_bodies['agent_demo_old'].get('name')!r}"
    )
    final_names = {cast(str, b.get("name")) for b in update_bodies.values()}
    assert len(final_names) == 3, (
        f"post-rekey names must be unique within the tenant, got {sorted(final_names)}"
    )
