"""J6 b: a GPT or Gemini turn reads its own agent's skills, and only those.

The agent comes from the turn's credential, never from the model's arguments; a
skill is readable only when it is attached to that agent and titled for this
tenant. Each read is cut at 64 KB, and one turn's reads stop at 512 KB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.skill_reads import (
    SKILL_READ_MAX_BYTES,
    SKILL_READ_TURN_BYTES,
    _skill_list_impl,  # pyright: ignore[reportPrivateUsage]
    _skill_read_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.ingest import bundle_from_upload
from daimon.core.stores import mcp_tokens
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import NotHandled, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _zip(name: str, description: str, extra: dict[str, bytes] | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\nDo it.\n"
        )
        for path, body in (extra or {}).items():
            archive.writestr(f"{name}/{path}", body)
    # The same checks a real upload passes.
    bundle_from_upload(buffer.getvalue(), filename="SKILL.zip")
    return buffer.getvalue()


def _row(skill_id: str, title: str) -> dict:
    return SkillListResponse(
        id=skill_id,
        type="custom",
        display_title=title,
        latest_version="1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")


@pytest.fixture
async def world(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    other_tenant = uuid.uuid4()
    big = b"x" * (SKILL_READ_MAX_BYTES + 100)
    archives = {
        "sk_mine": _zip("research", "Research a topic.", {"notes.txt": big}),
        "sk_theirs": _zip("billing", "Bill a customer."),
        "sk_foreign": _zip("secrets", "Another tenant's skill."),
    }
    mine = ma_agent(
        id="agent_mine",
        name="mine",
        tenant_id=tenant.id,
        skills=[
            {"type": "custom", "skill_id": "sk_mine", "version": "1"},
            # Attached, but titled for another tenant: never this tenant's to read.
            {"type": "custom", "skill_id": "sk_foreign", "version": "1"},
        ],
    )
    theirs = ma_agent(
        id="agent_theirs",
        name="theirs",
        tenant_id=tenant.id,
        skills=[{"type": "custom", "skill_id": "sk_theirs", "version": "1"}],
    )
    rows = [
        _row("sk_mine", tenant_scoped_display_title(tenant_id=tenant.id, name="research")),
        _row("sk_theirs", tenant_scoped_display_title(tenant_id=tenant.id, name="billing")),
        _row("sk_foreign", tenant_scoped_display_title(tenant_id=other_tenant, name="secrets")),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == "/v1/agents":
            return list_response([mine.model_dump(mode="json"), theirs.model_dump(mode="json")])
        if method == "GET" and path == "/v1/skills":
            return list_response(rows)
        for skill_id, data in archives.items():
            if method == "GET" and path == f"/v1/skills/{skill_id}/versions":
                return list_response([{"id": f"{skill_id}_v1", "version": "1"}])
            if (
                method == "GET"
                and path.startswith(f"/v1/skills/{skill_id}/")
                and path.endswith("/content")
            ):
                return httpx.Response(200, content=data)
        raise NotHandled

    runtime = McpRuntime(
        db_session_factory,
        build_fake_anthropic(handler),
        MagicMock(),
        DeploymentDefault(),
    )
    jti = uuid.uuid4()
    async with db_session_factory() as session, session.begin():
        await mcp_tokens.create_mcp_token_row(
            session,
            jti=jti,
            account_id=account.id,
            tenant_id=tenant.id,
            agent_id=str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_mine")),
            label="provider-turn",
            created_at=datetime.now(UTC),
            surface="provider_turn",
        )

    def turn(**changes: object) -> AuthIdentity:
        values: dict[str, object] = {
            "account_id": account.id,
            "tenant_id": tenant.id,
            "role": Role.USER,
            "chat_agent_id": derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_mine"),
            "token_jti": jti,
            "no_confirmation_surface": True,
        }
        values.update(changes)
        return AuthIdentity(**values)  # type: ignore[arg-type]

    return runtime, turn, db_session_factory, jti


async def test_a_turn_lists_and_reads_only_its_own_agents_skills(world):
    runtime, turn, *_ = world
    listed = await _skill_list_impl(runtime, turn())
    assert [(entry.name, entry.description) for entry in listed] == [
        ("research", "Research a topic.")
    ]
    read = await _skill_read_impl(runtime, turn(), "research")
    assert read.path == "SKILL.md" and "Do it." in read.content and not read.truncated
    assert read.files == ["SKILL.md", "notes.txt"]


async def test_another_agents_skill_is_refused(world):
    runtime, turn, *_ = world
    with pytest.raises(ToolError, match="isn't one of this agent's skills"):
        await _skill_read_impl(runtime, turn(), "billing")


async def test_another_tenants_skill_is_refused_even_when_attached(world):
    runtime, turn, *_ = world
    with pytest.raises(ToolError, match="isn't one of this agent's skills"):
        await _skill_read_impl(runtime, turn(), "secrets")


@pytest.mark.parametrize(
    "changes",
    [
        {"chat_agent_id": None},
        {"agent_id": uuid.uuid4()},  # an agent key
        {"no_confirmation_surface": False},  # a chat turn with a confirmation card
        {"token_jti": None},
    ],
)
async def test_only_a_provider_turns_own_credential_reads(world, changes):
    runtime, turn, *_ = world
    with pytest.raises(ToolError, match="Nothing was read"):
        await _skill_list_impl(runtime, turn(**changes))
    with pytest.raises(ToolError, match="Nothing was read"):
        await _skill_read_impl(runtime, turn(**changes), "research")


async def test_a_read_is_cut_and_a_turn_stops_at_its_budget(world):
    runtime, turn, sessions, jti = world
    read = await _skill_read_impl(runtime, turn(), "research", "notes.txt")
    assert read.truncated and len(read.content.encode()) == SKILL_READ_MAX_BYTES
    reads = SKILL_READ_TURN_BYTES // SKILL_READ_MAX_BYTES
    for _ in range(reads - 1):
        await _skill_read_impl(runtime, turn(), "research", "notes.txt")
    with pytest.raises(ToolError, match="512 KB"):
        await _skill_read_impl(runtime, turn(), "research", "notes.txt")
    async with sessions() as session:
        row = await mcp_tokens.get_mcp_token(session, jti=jti)
    assert row is not None
    # Every byte returned was counted, and the refused read added none.
    from sqlalchemy import text

    async with sessions() as session:
        counted = (
            await session.execute(
                text("SELECT skill_bytes_read FROM mcp_tokens WHERE jti = :jti"), {"jti": jti}
            )
        ).scalar_one()
    assert counted == reads * SKILL_READ_MAX_BYTES


async def test_another_tenants_credential_never_draws_on_this_budget(world):
    runtime, turn, sessions, jti = world
    async with sessions() as session, session.begin():
        assert not await mcp_tokens.charge_skill_bytes(
            session, jti=jti, tenant_id=uuid.uuid4(), size=1, budget=SKILL_READ_TURN_BYTES
        )
