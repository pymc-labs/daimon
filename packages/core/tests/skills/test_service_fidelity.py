"""Real SDK byte proofs for the default Add-skill host edge.

The reference sends the pre-refactor requests directly through AsyncAnthropic.
The migrated helper must preserve the full ordered trace, including multipart
bytes, query and beta headers. Only the SDK's random MIME boundary is fixed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import cast

import httpx
import httpx._multipart
import pytest
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsSkillParams,
    BetaManagedAgentsURLMCPServerParams,
)
from anthropic.types.beta.agent_create_params import (
    Model,
    Tool,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_FORKED_FROM,
    MA_METADATA_KEY_ISOLATED,
    build_metadata,
    tenant_scoped_display_title,
)
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import list_response
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("with_toolset", (False, True))
async def test_add_skill_preserves_the_pre_refactor_sdk_request_bytes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    with_toolset: bool,
) -> None:
    tenant = await make_tenant(db_session)
    toolset: Tool = {
        "type": "agent_toolset_20260401",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    agent = ma_agent(
        id="ag_upload", name="agent", tenant_id=tenant.id, tools=[toolset] if with_toolset else []
    )
    bundle = bundle_from_markdown("---\nname: notes\ndescription: Write notes.\n---\nWrite them.\n")
    title = tenant_scoped_display_title(tenant_id=tenant.id, name="notes", agent_name="agent")
    skill = {
        "id": "sk_uploaded",
        "type": "skill",
        "source": "custom",
        "display_title": title,
        "latest_version": "1",
        "created_at": "2026-04-21T00:00:00Z",
        "updated_at": "2026-04-21T00:00:00Z",
    }

    def fixed_boundary(_n: int) -> bytes:
        return b"b" * 16

    monkeypatch.setattr(httpx._multipart, "os", SimpleNamespace(urandom=fixed_boundary))

    def script() -> ScriptedTransport:
        transport = ScriptedTransport()
        transport.queue(
            *(ScriptedReply("GET", "/v1/skills", list_response([])) for _ in range(3)),
            ScriptedReply(
                "GET",
                "/v1/agents/ag_upload",
                httpx.Response(200, json=agent.model_dump(mode="json")),
            ),
            ScriptedReply("POST", "/v1/skills", httpx.Response(200, json=skill)),
            ScriptedReply(
                "GET",
                "/v1/agents/ag_upload",
                httpx.Response(200, json=agent.model_dump(mode="json")),
            ),
            ScriptedReply("GET", "/v1/skills", list_response([skill])),
            ScriptedReply(
                "POST",
                "/v1/agents/ag_upload",
                httpx.Response(200, json=agent.model_dump(mode="json")),
            ),
        )
        return transport

    reference, migrated = script(), script()
    async with reference.client() as sdk:
        # Mount conflict, same-title lookup and attached mount-name guard.
        for _ in range(3):
            await sdk.beta.skills.list(limit=1000)
        await sdk.beta.agents.retrieve(agent.id)
        await sdk.beta.skills.create(
            display_title=title, files=[("SKILL.zip", bundle.zip_bytes, "application/zip")]
        )
        await sdk.beta.agents.retrieve(agent.id)
        await sdk.beta.skills.list(limit=1000)
        pins: list[BetaManagedAgentsSkillParams] = [{"type": "custom", "skill_id": "sk_uploaded"}]
        if with_toolset:
            await sdk.beta.agents.update(agent.id, skills=pins, version=agent.version)
        else:
            # Frozen base toolset order, directly from the old host request.
            base: Tool = {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": name} for name in ("bash", "read", "edit", "grep", "glob", "write")
                ],
            }
            await sdk.beta.agents.update(agent.id, skills=pins, tools=[base], version=agent.version)

    rechecks: list[str] = []

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        assert fresh.id == agent.id
        rechecks.append(agent.id)

    async with migrated.client() as sdk:
        result = await add_agent_skill(
            sdk,
            db_session_factory,
            tenant_id=tenant.id,
            agent=agent,
            agent_name="agent",
            bundle=bundle,
            origin="pasted",
            added_by_account_id=None,
            recheck=recheck,
        )
    reference.assert_consumed()
    migrated.assert_consumed()
    assert migrated.requests == reference.requests
    assert rechecks == [agent.id, agent.id]
    assert (result.action, result.skill_id, result.version, result.newly_attached) == (
        "created",
        "sk_uploaded",
        "1",
        True,
    )


async def test_fork_partition_preserves_sdk_trace_and_raw_shared_pins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.agent_fork import _split_scoped_skills  # pyright: ignore[reportPrivateUsage]

    tenant = await make_tenant(db_session)
    source = ma_agent(id="ag_source", name="source", tenant_id=tenant.id)
    rows = [
        {
            "id": id_,
            "type": "skill",
            "source": "custom",
            "display_title": tenant_scoped_display_title(tenant_id=tenant.id, name=title),
            "latest_version": None,
            "created_at": "2026-04-21T00:00:00Z",
            "updated_at": "2026-04-21T00:00:00Z",
        }
        for id_, title in (("shared", "shared-notes"), ("owned", "source/notes"))
    ]
    pins: list[dict[str, object]] = [
        {"type": "custom", "skill_id": "shared", "version": "2", "native_optional": None},
        {"type": "custom", "skill_id": "owned", "version": "2"},
        {"type": "anthropic", "skill_id": "catalog", "version": "latest"},
    ]

    def script() -> ScriptedTransport:
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply("GET", "/v1/skills", list_response(rows)),
            ScriptedReply("GET", "/v1/agents", list_response([source.model_dump(mode="json")])),
        )
        return transport

    reference, migrated = script(), script()
    async with reference.client() as sdk:
        page = await sdk.beta.skills.list(limit=1000)
        # Both actual SDK rows accept null native version metadata.
        assert all(row.latest_version is None for row in page.data)
        _agents = [row async for row in sdk.beta.agents.list(include_archived=False, limit=100)]
    async with migrated.client() as sdk:
        split = await _split_scoped_skills(
            sdk, db_session_factory, tenant_id=tenant.id, source_name="source", skills=pins
        )
    reference.assert_consumed()
    migrated.assert_consumed()
    assert migrated.requests == reference.requests
    assert split.kept == [pins[0], pins[2]]
    assert split.kept[0] is pins[0]  # Native fields/order are not reconstructed.
    assert [(own.skill_id, own.body, own.version) for own in split.own] == [
        ("owned", "source/notes", "2")
    ]
    assert split.dropped == []


@pytest.mark.parametrize("with_toolset", (False, True))
@pytest.mark.parametrize("public_url", (None, "https://daimon.example.com/mcp"))
async def test_fork_create_preserves_pre_refactor_sdk_bytes_and_face_metadata(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    with_toolset: bool,
    public_url: str | None,
) -> None:
    from daimon.core import agent_fork
    from daimon.core.authz import Subject
    from daimon.core.defaults.provisioning import derive_guild_account_uuid

    tenant = await make_tenant(db_session)
    base: Tool = {
        "type": "agent_toolset_20260401",
        "configs": [{"name": name} for name in ("bash", "read", "edit", "grep", "glob", "write")],
    }
    existing: Tool = {
        "type": "agent_toolset_20260401",
        "configs": [],
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
    }
    source = ma_agent(
        id="source",
        name="source",
        tenant_id=tenant.id,
        system="Preserve this prompt.",
        metadata={MA_METADATA_KEY_ISOLATED: "true"},
        tools=[existing] if with_toolset else [],
        skills=[{"type": "anthropic", "skill_id": "pdf", "version": "latest"}],
    )
    metadata = build_metadata(
        tenant_id=tenant.id, name="copy", account_id=derive_guild_account_uuid(tenant.id)
    )
    copied = ma_agent(id="copy", name="copy", tenant_id=tenant.id, metadata=metadata)
    tools: list[Tool] = [existing] if with_toolset else [base]
    servers: list[BetaManagedAgentsURLMCPServerParams] = []
    if public_url is not None:
        mcp: Tool = {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}
        tools = [existing, mcp] if with_toolset else [mcp, base]
        servers = [{"type": "url", "name": "daimon-mcp", "url": public_url}]

    def script() -> ScriptedTransport:
        transport = ScriptedTransport()
        transport.queue(
            ScriptedReply(
                "GET", "/v1/agents/source", httpx.Response(200, json=source.model_dump(mode="json"))
            ),
            ScriptedReply(
                "POST", "/v1/agents", httpx.Response(200, json=copied.model_dump(mode="json"))
            ),
        )
        return transport

    reference, migrated = script(), script()
    async with reference.client() as sdk:
        await sdk.beta.agents.retrieve("source")
        await sdk.beta.agents.create(
            name="copy",
            model=cast(Model, source.model.model_dump(mode="json")),
            description=None,
            system="Preserve this prompt.",
            # J1: a copy records the agent it was forked from.
            metadata=metadata | {MA_METADATA_KEY_FORKED_FROM: "source"},
            tools=tools,
            mcp_servers=servers,
            skills=[{"type": "anthropic", "skill_id": "pdf", "version": "latest"}],
        )
    faces: list[Mapping[str, str] | None] = []

    def face(*args: object, **kwargs: object) -> None:
        faces.append(cast("Mapping[str, str] | None", kwargs.get("metadata")))

    monkeypatch.setattr(agent_fork, "queue_agent_face", face)
    async with migrated.client() as sdk:
        result = await agent_fork.copy_agent(
            sdk,
            db_session_factory,
            tenant_id=tenant.id,
            source=source,
            new_name="copy",
            public_url=public_url,
            subject=Subject(is_admin=True),
            default_agent_name="daimon",
        )
    reference.assert_consumed()
    migrated.assert_consumed()
    assert migrated.requests == reference.requests
    assert faces == [copied.metadata]
    assert result.agent.id == "copy" and result.dropped_skills == ()
