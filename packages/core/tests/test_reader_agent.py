from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_SPEC_HASH,
    MA_METADATA_KEY_TENANT,
    compute_spec_fingerprint,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.reader_agent import (
    READER_BLOCK,
    READER_SKILL_NAME,
    derive_reader_spec,
    ensure_reader_variant,
)
from daimon.core.specs import AgentSpec, SkillRef
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, list_response

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


def _agent_spec(**overrides: object) -> AgentSpec:
    defaults: dict[str, object] = {
        "name": "alpha",
        "model": "claude-sonnet-5",
        "system": "You are alpha, a data analyst.",
    }
    defaults.update(overrides)
    return AgentSpec.model_validate(defaults)


def test_derive_reader_spec_appends_reader_suffix_to_name() -> None:
    source = _agent_spec(name="alpha")

    derived = derive_reader_spec(source)

    assert derived.name == "alpha-reader"


def test_derive_reader_spec_marks_isolated_with_no_mcp_servers() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derived = derive_reader_spec(source)

    assert derived.isolated is True, "reader variant must be created via create_isolated_session"
    assert derived.mcp_servers is None, "reader variant must have no MCP surface"


def test_derive_reader_spec_drops_only_mcp_toolset_tool() -> None:
    tools: list[dict[str, Any]] = [
        {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"},
        {
            "type": "custom",
            "name": "lookup",
            "description": "Look something up.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=tools,
    )

    derived = derive_reader_spec(source)

    assert derived.tools is not None
    tool_types = [tool.get("type") for tool in derived.tools]
    assert tool_types == ["custom"], "only the mcp_toolset entry should be dropped"


def test_derive_reader_spec_collapses_tools_to_none_when_only_mcp_toolset_present() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derived = derive_reader_spec(source)

    assert derived.tools is None, (
        "an empty list and None are not equivalent downstream — must collapse to None"
    )


def test_derive_reader_spec_adds_report_reader_skill_alongside_existing_skills() -> None:
    source = _agent_spec(skills=[SkillRef(type="custom", skill_id="pymc-artifact-style")])

    derived = derive_reader_spec(source)

    skill_ids = [ref.skill_id for ref in derived.skills]
    assert skill_ids == ["pymc-artifact-style", READER_SKILL_NAME]


def test_derive_reader_spec_is_idempotent_on_skills_and_system() -> None:
    source = _agent_spec(skills=[SkillRef(type="custom", skill_id="pymc-artifact-style")])

    once = derive_reader_spec(source)
    twice = derive_reader_spec(once)

    reader_skill_count = sum(1 for ref in twice.skills if ref.skill_id == READER_SKILL_NAME)
    assert reader_skill_count == 1, "deriving twice must not duplicate the report-reader skill ref"
    assert twice.system is not None
    assert twice.system.count(READER_BLOCK) == 1, (
        "deriving twice must not duplicate the appended reader block"
    )


def test_derive_reader_spec_appends_reader_block_to_source_system() -> None:
    source = _agent_spec(system="You are alpha, a data analyst.")

    derived = derive_reader_spec(source)

    assert derived.system is not None
    assert derived.system.startswith(source.system or "")
    assert "cite the file and the column" in derived.system, (
        "reader block must carry the citation clause"
    )


def test_derive_reader_spec_does_not_mutate_source() -> None:
    source = _agent_spec(
        mcp_servers=[{"type": "url", "name": "daimon-mcp", "url": "https://x/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"}],
    )

    derive_reader_spec(source)

    assert source.mcp_servers is not None, "source spec must be untouched by derivation"
    assert source.isolated is False


# ---------------------------------------------------------------------------
# ensure_reader_variant — find, create or update, keyed by the source's shape
# ---------------------------------------------------------------------------


def _source_agent_dict(
    *,
    id_: str,
    name: str = "alpha",
    tenant_id: uuid.UUID = TENANT_ID,
    spec_hash: str | None = "src-hash-1",
    version: int = 1,
    system: str = "You are alpha, a data analyst.",
) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
    }
    if spec_hash is not None:
        metadata[MA_METADATA_KEY_SPEC_HASH] = spec_hash
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-sonnet-4-6"},
            "metadata": metadata,
            "description": None,
            "archived_at": None,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "version": version,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": system,
        }
    ).model_dump(mode="json")


def _reader_agent_dict(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    metadata_extra: dict[str, str],
    version: int = 1,
) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
        **metadata_extra,
    }
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-sonnet-4-6"},
            "metadata": metadata,
            "description": None,
            "archived_at": None,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-01T00:00:00Z",
            "version": version,
            "mcp_servers": [],
            "skills": [{"type": "custom", "skill_id": "sk_reader_resolved", "version": "1"}],
            "tools": [],
            "system": "",
        }
    ).model_dump(mode="json")


def _router(agents: list[dict[str, Any]]) -> MARouter:
    """List + per-id retrieve + a skills list that resolves the report-reader ref."""
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response(agents))

    def _retrieve(req: httpx.Request, match: re.Match[str]) -> httpx.Response:
        agent_id = match.group(1)
        for agent in agents:
            if agent["id"] == agent_id:
                return httpx.Response(200, json=agent)
        return httpx.Response(
            404,
            json={"type": "error", "error": {"type": "not_found_error", "message": "not found"}},
        )

    router.add("GET", r"/v1/agents/([^/]+)", _retrieve)
    canonical_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name=READER_SKILL_NAME)
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_reader_resolved",
                    type="custom",
                    display_title=canonical_title,
                    latest_version="1",
                    created_at="2026-09-01T00:00:00Z",
                    updated_at="2026-09-01T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    return router


def _add_create_route(router: MARouter, captured: dict[str, Any]) -> None:
    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json_body(req))
        return httpx.Response(
            200,
            json=_reader_agent_dict(
                id_="ag_reader",
                name="alpha-reader",
                tenant_id=TENANT_ID,
                metadata_extra=captured["metadata"],
            ),
        )

    router.add("POST", r"/v1/agents", on_create)


async def test_ensure_reader_variant_creates_when_no_existing_variant() -> None:
    """No existing variant -> exactly one agents.create, stamped isolated/unmanaged."""
    source = _source_agent_dict(id_="ag_src", spec_hash="src-hash-1")
    router = _router([source])
    created: dict[str, Any] = {}
    _add_create_route(router, created)
    client = build_fake_anthropic(router.dispatch)

    result = await ensure_reader_variant(
        client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    assert result.id == "ag_reader"
    md = created["metadata"]
    assert md[MA_METADATA_KEY_ISOLATED] == "true"
    assert MA_METADATA_KEY_MANAGED not in md, "a reader variant must never be stamped managed"
    assert md[MA_METADATA_KEY_READER_OF] == "src-hash-1"


async def test_ensure_reader_variant_reuses_on_unchanged_source() -> None:
    """Second call against an unchanged source issues zero create/update calls."""
    source = _source_agent_dict(id_="ag_src", spec_hash="src-hash-1")
    router = _router([source])
    created: dict[str, Any] = {}
    _add_create_route(router, created)
    client = build_fake_anthropic(router.dispatch)
    first = await ensure_reader_variant(
        client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    # No POST route registered at all on this router — any create or update
    # attempt raises AssertionError from MARouter, failing the test.
    reader_variant = _reader_agent_dict(
        id_="ag_reader", name="alpha-reader", tenant_id=TENANT_ID, metadata_extra=first.metadata
    )
    router2 = _router([source, reader_variant])
    client2 = build_fake_anthropic(router2.dispatch)

    second = await ensure_reader_variant(
        client2, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    assert second.id == "ag_reader"


async def test_ensure_reader_variant_updates_when_source_spec_hash_changes() -> None:
    """A changed source spec hash issues exactly one agents.update, zero agents.create."""
    source = _source_agent_dict(id_="ag_src", spec_hash="src-hash-1")
    router = _router([source])
    created: dict[str, Any] = {}
    _add_create_route(router, created)
    client = build_fake_anthropic(router.dispatch)
    first = await ensure_reader_variant(
        client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    changed_source = _source_agent_dict(id_="ag_src", spec_hash="src-hash-2")
    reader_variant = _reader_agent_dict(
        id_="ag_reader",
        name="alpha-reader",
        tenant_id=TENANT_ID,
        metadata_extra=first.metadata,
        version=first.version,
    )
    router2 = _router([changed_source, reader_variant])
    updated: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updated.update(json_body(req))
        return httpx.Response(
            200,
            json=_reader_agent_dict(
                id_="ag_reader",
                name="alpha-reader",
                tenant_id=TENANT_ID,
                metadata_extra=updated["metadata"],
                version=first.version + 1,
            ),
        )

    # No POST /v1/agents (create) route on this router — a stray create call
    # raises AssertionError from MARouter, failing the test.
    router2.add("POST", r"/v1/agents/ag_reader", on_update)
    client2 = build_fake_anthropic(router2.dispatch)

    second = await ensure_reader_variant(
        client2, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    assert updated["version"] == first.version, "update must use MA's version, not a stale one"
    assert updated["metadata"][MA_METADATA_KEY_READER_OF] == "src-hash-2"
    assert second.version == first.version + 1


async def test_ensure_reader_variant_reader_of_fallback_changes_with_source_version() -> None:
    """No daimon_spec_hash on the source -> reader_of falls back to (id, version); bumping
    version changes it, forcing an update even though nothing else about the source moved."""
    source_v1 = _source_agent_dict(id_="ag_src", spec_hash=None, version=1)
    router = _router([source_v1])
    created: dict[str, Any] = {}
    _add_create_route(router, created)
    client = build_fake_anthropic(router.dispatch)
    first = await ensure_reader_variant(
        client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    expected_reader_of_v1 = compute_spec_fingerprint({"agent_id": "ag_src", "version": 1})
    assert first.metadata[MA_METADATA_KEY_READER_OF] == expected_reader_of_v1

    source_v2 = _source_agent_dict(id_="ag_src", spec_hash=None, version=2)
    reader_variant = _reader_agent_dict(
        id_="ag_reader",
        name="alpha-reader",
        tenant_id=TENANT_ID,
        metadata_extra=first.metadata,
        version=first.version,
    )
    router2 = _router([source_v2, reader_variant])
    updated: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updated.update(json_body(req))
        return httpx.Response(
            200,
            json=_reader_agent_dict(
                id_="ag_reader",
                name="alpha-reader",
                tenant_id=TENANT_ID,
                metadata_extra=updated["metadata"],
                version=first.version + 1,
            ),
        )

    router2.add("POST", r"/v1/agents/ag_reader", on_update)
    client2 = build_fake_anthropic(router2.dispatch)

    await ensure_reader_variant(
        client2, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    expected_reader_of_v2 = compute_spec_fingerprint({"agent_id": "ag_src", "version": 2})
    assert expected_reader_of_v2 != expected_reader_of_v1
    assert updated["metadata"][MA_METADATA_KEY_READER_OF] == expected_reader_of_v2


async def test_ensure_reader_variant_raises_for_unknown_source() -> None:
    """Unknown source_name raises DaimonError and issues no create/update calls."""
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    with pytest.raises(DaimonError, match="ghost"):
        await ensure_reader_variant(
            client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="ghost"
        )


async def test_ensure_reader_variant_ignores_the_source_toolset_shape() -> None:
    """A live agent's toolset carries per-config fields the create shape rejects.

    Managed Agents returns each toolset config entry with a ``type`` next to
    its ``name`` (and ``enabled`` / ``permission_policy`` filled in). Feeding
    that back into an agent spec fails validation, which is exactly what
    happened on the first live publish. The reader must be created from the
    base toolset alone, whatever the source carries.
    """
    source = _source_agent_dict(id_="ag_src", spec_hash="src-hash-1")
    source["tools"] = [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [
                {
                    "type": name,
                    "name": name,
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                }
                for name in ("bash", "read", "edit", "grep", "glob", "write")
            ],
        }
    ]
    router = _router([source])
    created: dict[str, Any] = {}
    _add_create_route(router, created)
    client = build_fake_anthropic(router.dispatch)

    result = await ensure_reader_variant(
        client, tenant_id=TENANT_ID, account_id=ACCOUNT_ID, source_name="alpha"
    )

    assert result.id == "ag_reader"
    tools = created["tools"]
    assert [tool["type"] for tool in tools] == ["agent_toolset_20260401"], (
        "the reader must carry exactly the base agent toolset"
    )
    config_names = [config["name"] for config in tools[0]["configs"]]
    assert config_names == ["bash", "read", "edit", "grep", "glob", "write"], (
        "the base toolset's six tools, in the spec's own order"
    )
    assert all("type" not in config for config in tools[0]["configs"]), (
        "no per-config type may leak from the live response into the create call"
    )
    assert tools[0]["default_config"]["permission_policy"] == {"type": "always_allow"}, (
        "readers run headless, so the dump step must inject always-allow"
    )
