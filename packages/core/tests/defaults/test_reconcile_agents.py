from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from daimon.core.agent_guidance import (
    CREDENTIAL_GUIDANCE_BLOCK,
    apply_credential_guidance,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    tenant_scoped_display_title,
)
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import Action
from daimon.core.specs import AgentSpec, SkillRef
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_DEFAULT_MCP_URL = "https://daimon.example/mcp"
_OTHER_MCP_URL = "https://other.example/mcp"


def _agent_spec(
    skills: list[SkillRef] | None = None,
    mcp_servers: list[Any] | None = None,
) -> AgentSpec:
    tools: list[Any] = [{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}]
    # AgentSpec validates that mcp_servers implies a matching mcp_toolset.
    # The factory adds one per declared server so authoring tests don't trip
    # the validator (which exists to catch hand-authored mistakes, not these).
    for server in mcp_servers or []:
        tools.append({"type": "mcp_toolset", "mcp_server_name": server["name"]})
    return AgentSpec(
        name="daimon",
        model="claude-sonnet-4-6",
        system="You are daimon.",
        tools=tools,
        skills=skills or [],
        mcp_servers=mcp_servers,
    )


def _tagged_agent(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    version: int = 1,
    created_at: str = "2026-04-21T00:00:00Z",
    account_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
    }
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": metadata,
            "description": None,
            "archived_at": None,
            "created_at": created_at,
            "updated_at": created_at,
            "version": version,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": None,
        }
    ).model_dump(mode="json")


def _router_with_agents(agents: list[dict[str, Any]]) -> MARouter:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response(agents))
    return router


async def test_reconcile_agent_creates_when_not_on_ma() -> None:
    """No MA match → CREATE path; resolved skills and metadata sent in POST body."""
    spec = _agent_spec()
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id == "ag_new"
    md = created_payload["metadata"]
    assert md["daimon_tenant"] == str(TENANT_ID)
    assert md["daimon_name"] == "daimon"
    assert md["daimon_managed"] == "true", (
        "reconcile must stamp daimon_managed=true so sweep distinguishes defaults from user forks"
    )
    assert "daimon_spec_hash" in md, "reconcile must stamp daimon_spec_hash for L13 idempotency"
    assert len(md["daimon_spec_hash"]) == 16, "spec hash is 16 hex chars"


async def test_reconcile_agent_updates_when_on_ma() -> None:
    """MA match found → UPDATE path; version from MA response, not DB."""
    spec = _agent_spec()
    router = _router_with_agents(
        [_tagged_agent(id_="ag_1", name=spec.name, tenant_id=TENANT_ID, version=3)]
    )
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_1", name=spec.name, tenant_id=TENANT_ID, version=4),
        )

    router.add("POST", r"/v1/agents/ag_1", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED
    assert outcome.anthropic_id == "ag_1"
    assert updated_payload["version"] == 3, "version from MA match, not from DB"


async def test_reconcile_agent_dry_run_create() -> None:
    """dry_run=True with no MA match → CREATED action, no write calls, no anthropic_id."""
    spec = _agent_spec()
    # No POST handler — router raises if reconcile tries to write.
    router = _router_with_agents([])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id is None


async def test_reconcile_agent_dry_run_update() -> None:
    """dry_run=True with MA match → UPDATED action, no write calls, no anthropic_id."""
    spec = _agent_spec()
    # No POST handler for update — router raises if reconcile tries to write.
    router = _router_with_agents([_tagged_agent(id_="ag_1", name=spec.name, tenant_id=TENANT_ID)])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.UPDATED
    assert outcome.anthropic_id is None


async def test_reconcile_agent_creates_with_custom_skills() -> None:
    """Custom skill refs are resolved via MA skills list; resolved id sent in POST body."""
    spec = _agent_spec(skills=[SkillRef(type="custom", skill_id="brainstorming")])
    router = MARouter()
    # Skills list must return the skill so resolve_refs can find it.
    canonical_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="brainstorming")
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_resolved",
                    type="custom",
                    display_title=canonical_title,
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED
    assert created_payload["skills"] == [{"type": "custom", "skill_id": "sk_resolved"}], (
        "resolved MA skill id must be sent in the POST body"
    )


async def test_reconcile_agent_appends_default_mcp_when_public_url_set_on_create() -> None:
    """public_url set + no MA match → CREATE path; mcp_servers in POST body contains default URL."""
    spec = _agent_spec()
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.CREATED, "create path must fire when no MA match"
    mcp_servers = created_payload.get("mcp_servers")
    assert mcp_servers is not None, "POST body must contain mcp_servers when public_url is set"
    assert len(mcp_servers) == 1, "POST body must contain exactly one mcp_servers entry"
    assert mcp_servers[0].get("url") == _DEFAULT_MCP_URL, (
        "POST body mcp_servers entry must have the default URL"
    )
    tools = created_payload.get("tools")
    assert tools is not None, "POST body must contain tools when public_url is set"
    mcp_toolsets = [t for t in tools if t.get("type") == "mcp_toolset"]
    assert len(mcp_toolsets) == 1, f"POST body must contain exactly one mcp_toolset; got {tools!r}"
    assert mcp_toolsets[0].get("mcp_server_name") == "daimon-mcp", (
        "mcp_toolset must reference mcp_server_name='daimon-mcp'"
    )
    # dump_agent_spec injects permission_policy at the boundary; verify it landed.
    assert mcp_toolsets[0].get("default_config", {}).get("permission_policy") == {
        "type": "always_allow"
    }, "dump_agent_spec must inject always_allow permission_policy on the daimon-mcp toolset"


async def test_reconcile_agent_no_op_for_mcp_servers_when_public_url_is_none() -> None:
    """public_url=None + spec has no mcp_servers → POST body must NOT contain mcp_servers key."""
    spec = _agent_spec()  # mcp_servers=None by default
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=None
    )
    assert outcome.action is Action.CREATED, "create path must fire when no MA match"
    assert "mcp_servers" not in created_payload, (
        "POST body must NOT contain mcp_servers when public_url is None and spec has no mcp_servers"
    )
    tools = created_payload.get("tools") or []
    mcp_toolsets = [t for t in tools if t.get("type") == "mcp_toolset"]
    assert mcp_toolsets == [], (
        f"POST body must NOT contain a daimon-mcp mcp_toolset when public_url is None; got {tools!r}"
    )


async def test_reconcile_agent_preserves_author_mcp_and_appends_default_on_update() -> None:
    """spec has author mcp_servers; update branch fires; body has both entries in order."""
    spec = _agent_spec(mcp_servers=[{"name": "other-mcp", "type": "url", "url": _OTHER_MCP_URL}])
    router = _router_with_agents(
        [_tagged_agent(id_="ag_1", name=spec.name, tenant_id=TENANT_ID, version=2)]
    )
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_1", name=spec.name, tenant_id=TENANT_ID, version=3),
        )

    router.add("POST", r"/v1/agents/ag_1", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.UPDATED, "update path must fire when MA match found"
    mcp_servers = updated_payload.get("mcp_servers")
    assert mcp_servers is not None, "PATCH body must contain mcp_servers"
    assert len(mcp_servers) == 2, (
        "PATCH body must contain exactly 2 mcp_servers entries (author + default); "
        f"got {mcp_servers!r}"
    )
    assert mcp_servers[0].get("url") == _OTHER_MCP_URL, (
        "first mcp_servers entry must be the author entry"
    )
    assert mcp_servers[1].get("url") == _DEFAULT_MCP_URL, (
        "second mcp_servers entry must be the default URL"
    )
    tools = updated_payload.get("tools")
    assert tools is not None, "PATCH body must contain tools (default toolset injected)"
    mcp_toolsets = [t for t in tools if t.get("type") == "mcp_toolset"]
    assert any(t.get("mcp_server_name") == "daimon-mcp" for t in mcp_toolsets), (
        f"PATCH body tools must contain a daimon-mcp mcp_toolset; got {tools!r}"
    )
    # Existing agent_toolset_20260401 from _agent_spec must be preserved.
    agent_toolsets = [t for t in tools if t.get("type") == "agent_toolset_20260401"]
    assert len(agent_toolsets) == 1, "author agent_toolset must be preserved alongside daimon-mcp"


async def test_reconcile_agent_is_idempotent_when_default_already_in_spec() -> None:
    """spec already has the default URL in mcp_servers; POST body must have exactly one entry."""
    spec = _agent_spec(mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": _DEFAULT_MCP_URL}])
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.CREATED, "create path must fire when no MA match"
    mcp_servers = created_payload.get("mcp_servers", [])
    assert len(mcp_servers) == 1, (
        f"mcp_servers must have exactly one entry (no duplicate); got {mcp_servers!r}"
    )
    assert mcp_servers[0]["url"] == _DEFAULT_MCP_URL, (
        "the single mcp_servers entry must be the default URL"
    )
    tools = created_payload.get("tools") or []
    mcp_toolsets = [
        t
        for t in tools
        if t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "daimon-mcp"
    ]
    assert len(mcp_toolsets) == 1, (
        f"POST body must contain exactly one daimon-mcp mcp_toolset; got {tools!r}"
    )


async def test_reconcile_agent_stamps_daimon_account_when_provided() -> None:
    """account_id passed → POST body metadata must include daimon_account=str(account_id)."""
    spec = _agent_spec()
    account_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_acct", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, account_id=account_id
    )
    assert outcome.action is Action.CREATED
    metadata = created_payload.get("metadata")
    assert metadata is not None, "POST body must contain metadata"
    assert metadata.get(MA_METADATA_KEY_ACCOUNT) == str(account_id), (
        "account_id must be stamped as daimon_account on MA metadata (per-user roster)"
    )
    assert metadata.get(MA_METADATA_KEY_TENANT) == str(TENANT_ID)
    assert metadata.get(MA_METADATA_KEY_NAME) == spec.name


async def test_reconcile_agent_omits_daimon_account_when_not_provided() -> None:
    """account_id absent (default None) → POST body metadata must NOT contain daimon_account."""
    spec = _agent_spec()
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_noacct", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    metadata = created_payload.get("metadata", {})
    assert MA_METADATA_KEY_ACCOUNT not in metadata, (
        "no account_id → no daimon_account stamp (seeded-default 'everyone's agent' semantics)"
    )


async def test_reconcile_agent_does_not_duplicate_mcp_toolset_when_already_in_spec() -> None:
    """spec already has a daimon-mcp mcp_toolset; reconcile must not add a duplicate."""
    spec = AgentSpec(
        name="daimon",
        model="claude-sonnet-4-6",
        system="You are daimon.",
        tools=[
            {"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]},
            {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"},
        ],
        skills=[],
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": _DEFAULT_MCP_URL}],
    )
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_idem", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)
    await reconcile_agent(
        client,
        spec,
        tenant_id=TENANT_ID,
        dry_run=False,
        public_url=_DEFAULT_MCP_URL,
    )
    tools = created_payload.get("tools") or []
    mcp_toolsets = [
        t
        for t in tools
        if t.get("type") == "mcp_toolset" and t.get("mcp_server_name") == "daimon-mcp"
    ]
    assert len(mcp_toolsets) == 1, (
        f"reconcile must not duplicate the daimon-mcp mcp_toolset; got {tools!r}"
    )


async def test_reconcile_agent_archives_duplicates_keeps_canonical() -> None:
    """R5 dedup: when MA returns multiple agents with the same daimon_name,
    the canonical match (max created_at) is updated and the duplicates are
    archived inline. Without this, the duplicates accumulate forever — the
    smoke run found two live daimon agents in the staging tenant
    (2026-05-21).
    """
    spec = _agent_spec()
    archived: list[str] = []
    canonical = _tagged_agent(
        id_="ag_canonical",
        name=spec.name,
        tenant_id=TENANT_ID,
        created_at="2026-05-20T17:58:05Z",
    )
    duplicate = _tagged_agent(
        id_="ag_dup",
        name=spec.name,
        tenant_id=TENANT_ID,
        created_at="2026-05-17T22:39:01Z",
    )
    router = _router_with_agents([canonical, duplicate])

    def on_canonical_update(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(
            200,
            json=_tagged_agent(
                id_="ag_canonical",
                name=spec.name,
                tenant_id=TENANT_ID,
                version=2,
                created_at="2026-05-20T17:58:05Z",
            ),
        )

    def on_dup_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archived.append("ag_dup")
        return httpx.Response(
            200,
            json=_tagged_agent(
                id_="ag_dup", name=spec.name, tenant_id=TENANT_ID, created_at="2026-05-17T22:39:01Z"
            ),
        )

    router.add("POST", r"/v1/agents/ag_canonical$", on_canonical_update)
    router.add("POST", r"/v1/agents/ag_dup/archive", on_dup_archive)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED, (
        "canonical (max created_at) is updated, not the duplicate"
    )
    assert outcome.anthropic_id == "ag_canonical"
    assert archived == ["ag_dup"], (
        "the duplicate must be archived inline by reconcile, not left for the sweep "
        "(sweep can't tell duplicates apart by name)"
    )


async def test_reconcile_agent_dry_run_does_not_archive_duplicates() -> None:
    """Dedup respects dry_run — no MA writes happen on a dry-run apply."""
    spec = _agent_spec()
    archived: list[str] = []
    canonical = _tagged_agent(
        id_="ag_canonical",
        name=spec.name,
        tenant_id=TENANT_ID,
        created_at="2026-05-20T17:58:05Z",
    )
    duplicate = _tagged_agent(
        id_="ag_dup",
        name=spec.name,
        tenant_id=TENANT_ID,
        created_at="2026-05-17T22:39:01Z",
    )
    router = _router_with_agents([canonical, duplicate])

    def fail_if_called(req: httpx.Request, _m: object) -> httpx.Response:
        archived.append("called")
        return httpx.Response(500)

    router.add("POST", r"/v1/agents/ag_dup/archive", fail_if_called)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.UPDATED
    assert archived == [], "dry_run must not archive duplicates"


async def test_reconcile_agent_returns_skipped_when_spec_hash_matches() -> None:
    """L13 idempotency: when MA's metadata already carries the daimon_spec_hash
    that the current spec would compute, reconcile must short-circuit to
    SKIPPED without calling agents.update. Without this the version bumps
    on every defaults apply, which runs on every scheduler boot.
    """
    from daimon.core.defaults.metadata import compute_spec_fingerprint
    from daimon.core.specs import dump_agent_spec

    spec = _agent_spec()
    # reconcile folds the credential-guidance block into system before hashing,
    # so the expected hash must be computed from the guidance-applied spec —
    # mirror that transformation here or the SKIPPED short-circuit never fires.
    hashed_spec = spec.model_copy(update={"system": apply_credential_guidance(spec.system or "")})
    expected_hash = compute_spec_fingerprint(
        {
            "spec": dump_agent_spec(hashed_spec, mode="json"),
            "skills": [],
            "account_id": None,
        }
    )

    existing = BetaManagedAgentsAgent.model_validate(
        {
            "id": "ag_existing",
            "type": "agent",
            "name": spec.name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(TENANT_ID),
                MA_METADATA_KEY_NAME: spec.name,
                "daimon_managed": "true",
                "daimon_spec_hash": expected_hash,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-05-21T00:00:00Z",
            "updated_at": "2026-05-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": None,
        }
    ).model_dump(mode="json")

    update_called = False

    def fail_if_update_called(req: httpx.Request, _m: object) -> httpx.Response:
        nonlocal update_called
        update_called = True
        return httpx.Response(500)

    router = _router_with_agents([existing])
    router.add("POST", r"/v1/agents/ag_existing$", fail_if_update_called)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.SKIPPED, (
        "matching spec_hash must short-circuit reconcile to SKIPPED"
    )
    assert outcome.anthropic_id == "ag_existing"
    assert not update_called, "agents.update must NOT have been called"


async def test_reconcile_agent_preserves_user_attached_mcp_on_update() -> None:
    """Bug #12: an external MCP attached directly to the daimon agent (e.g.
    via SDK) must survive `defaults apply`. Before the fix, reconcile sent
    only the YAML-spec entries (plus the merged daimon-mcp), wiping any
    user additions on MA.

    Live repro: smoke #30 (2026-05-21 20:07Z) — attached context7 MCP,
    ran `daimon defaults apply`, context7 entry was gone.
    """
    spec = _agent_spec()  # spec has no mcp_servers, no skills

    existing = BetaManagedAgentsAgent.model_validate(
        {
            "id": "ag_existing",
            "type": "agent",
            "name": spec.name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(TENANT_ID),
                MA_METADATA_KEY_NAME: spec.name,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-05-21T00:00:00Z",
            "updated_at": "2026-05-21T00:00:00Z",
            "version": 2,
            "mcp_servers": [
                {"name": "context7-smoke", "type": "url", "url": "https://ctx7.example/mcp"},
            ],
            "skills": [
                {"skill_id": "sk_external", "type": "custom", "version": "1"},
            ],
            "tools": [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "context7-smoke",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
            "system": None,
        }
    ).model_dump(mode="json")

    router = _router_with_agents([existing])
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_existing", name=spec.name, tenant_id=TENANT_ID, version=3),
        )

    router.add("POST", r"/v1/agents/ag_existing$", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.UPDATED

    sent_mcp = updated_payload.get("mcp_servers", [])
    names = [m.get("name") for m in sent_mcp]
    assert "context7-smoke" in names, (
        f"user-attached MCP must be preserved on update; got mcp_servers={sent_mcp!r}"
    )
    assert "daimon-mcp" in names, (
        f"default daimon-mcp must still be merged when public_url is set; got {sent_mcp!r}"
    )

    sent_skills = updated_payload.get("skills", [])
    skill_ids = [s.get("skill_id") for s in sent_skills]
    assert "sk_external" in skill_ids, (
        f"user-pinned external skill must be preserved on update; got skills={sent_skills!r}"
    )

    sent_tools = updated_payload.get("tools", [])
    toolset_names = [t.get("mcp_server_name") for t in sent_tools if t.get("type") == "mcp_toolset"]
    assert "context7-smoke" in toolset_names, (
        f"user's external mcp_toolset must be preserved; got tools={sent_tools!r}"
    )
    assert "daimon-mcp" in toolset_names, (
        f"default daimon-mcp toolset must still be merged; got tools={sent_tools!r}"
    )


async def test_reconcile_agent_spec_wins_on_mcp_name_collision() -> None:
    """When YAML spec and MA both declare an mcp_server with the same name,
    the spec's entry wins (daimon is authoritative for what it seeds — a
    URL drift from a stale MA entry must be overwritten, not preserved).
    """
    drifted_url = "https://stale.example/mcp"
    spec = _agent_spec()  # spec has no explicit mcp_servers; daimon-mcp is merged in via public_url
    existing = BetaManagedAgentsAgent.model_validate(
        {
            "id": "ag_existing",
            "type": "agent",
            "name": spec.name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(TENANT_ID),
                MA_METADATA_KEY_NAME: spec.name,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-05-21T00:00:00Z",
            "updated_at": "2026-05-21T00:00:00Z",
            "version": 1,
            # MA has a daimon-mcp entry but at a stale URL — spec must overwrite.
            "mcp_servers": [
                {"name": "daimon-mcp", "type": "url", "url": drifted_url},
            ],
            "skills": [],
            "tools": [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
            "system": None,
        }
    ).model_dump(mode="json")

    router = _router_with_agents([existing])
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_existing", name=spec.name, tenant_id=TENANT_ID, version=2),
        )

    router.add("POST", r"/v1/agents/ag_existing$", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    sent_mcp = updated_payload.get("mcp_servers", [])
    daimon_entries = [m for m in sent_mcp if m.get("name") == "daimon-mcp"]
    assert len(daimon_entries) == 1, (
        f"exactly one daimon-mcp entry expected (no dup); got {sent_mcp!r}"
    )
    assert daimon_entries[0]["url"] == _DEFAULT_MCP_URL, (
        f"spec URL must win on name collision (stale MA URL must be overwritten); got {sent_mcp!r}"
    )


async def test_reconcile_agent_updates_when_spec_hash_mismatch() -> None:
    """When the existing daimon_spec_hash differs (or is absent), reconcile
    falls through to the normal UPDATE path. Verifies the SKIPPED branch
    only fires on a real match.
    """
    spec = _agent_spec()
    router = _router_with_agents(
        [
            BetaManagedAgentsAgent.model_validate(
                {
                    "id": "ag_existing",
                    "type": "agent",
                    "name": spec.name,
                    "model": {"id": "claude-opus-4-7"},
                    "metadata": {
                        MA_METADATA_KEY_TENANT: str(TENANT_ID),
                        MA_METADATA_KEY_NAME: spec.name,
                        "daimon_managed": "true",
                        "daimon_spec_hash": "stale" + "0" * 11,  # 16 chars, won't match
                    },
                    "description": None,
                    "archived_at": None,
                    "created_at": "2026-05-21T00:00:00Z",
                    "updated_at": "2026-05-21T00:00:00Z",
                    "version": 1,
                    "mcp_servers": [],
                    "skills": [],
                    "tools": [],
                    "system": None,
                }
            ).model_dump(mode="json")
        ]
    )

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(
            200, json=_tagged_agent(id_="ag_existing", name=spec.name, tenant_id=TENANT_ID)
        )

    router.add("POST", r"/v1/agents/ag_existing$", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED, "stale hash must fall through to UPDATE"


async def test_reconcile_agent_injects_credential_guidance_into_system_on_create() -> None:
    """The agent's pushed system prompt must carry the credential-guidance block
    so the agent knows where its secrets live (env file) vs MCP vault auth, and
    stops hallucinating "no key" / hunting for non-existent MCP keys. The block
    is prepended; the YAML-authored body is preserved beneath it.
    """
    spec = _agent_spec()  # system="You are daimon."
    router = _router_with_agents([])
    created_payload: dict[str, Any] = {}

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        created_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_new", name=spec.name, tenant_id=TENANT_ID),
        )

    router.add("POST", r"/v1/agents", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED
    system = created_payload.get("system")
    assert system is not None, "POST body must contain system"
    assert CREDENTIAL_GUIDANCE_BLOCK in system, (
        "reconcile must inject the credential-guidance block into the pushed system prompt"
    )
    assert system.endswith("You are daimon."), (
        "the YAML-authored system body must be preserved beneath the guidance block"
    )


async def test_reconcile_agent_dedup_skips_archive_when_duplicate_account_differs() -> None:
    """Defense in depth: when canonical and duplicate carry different daimon_account
    values, the duplicate must NOT be archived — only a warning is emitted. Cross-account
    archive would be cross-owner data loss.
    """
    spec = _agent_spec()
    account_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    account_b = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
    archive_calls: list[str] = []

    canonical = _tagged_agent(
        id_="ag_canonical",
        name=spec.name,
        tenant_id=TENANT_ID,
        account_id=account_a,
        created_at="2026-05-20T17:58:05Z",
    )
    duplicate = _tagged_agent(
        id_="ag_dup_b",
        name=spec.name,
        tenant_id=TENANT_ID,
        account_id=account_b,
        created_at="2026-05-17T22:39:01Z",
    )
    router = _router_with_agents([canonical, duplicate])

    def on_canonical_update(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(
            200,
            json=_tagged_agent(
                id_="ag_canonical",
                name=spec.name,
                tenant_id=TENANT_ID,
                account_id=account_a,
                version=2,
                created_at="2026-05-20T17:58:05Z",
            ),
        )

    def on_dup_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archive_calls.append("ag_dup_b")
        return httpx.Response(200, json=duplicate)

    router.add("POST", r"/v1/agents/ag_canonical$", on_canonical_update)
    router.add("POST", r"/v1/agents/ag_dup_b/archive", on_dup_archive)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED, (
        "reconcile must complete normally (canonical is updated)"
    )
    assert outcome.anthropic_id == "ag_canonical"
    assert archive_calls == [], (
        "cross-account duplicate must NOT be archived — daimon_account differs from canonical's"
    )


async def test_reconcile_agent_dedup_archives_duplicate_when_account_matches() -> None:
    """Same-account dedup: when both canonical and duplicate carry the same daimon_account,
    the duplicate IS archived (existing R5 behaviour must remain unbroken).
    """
    spec = _agent_spec()
    account_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    archive_calls: list[str] = []

    canonical = _tagged_agent(
        id_="ag_canonical",
        name=spec.name,
        tenant_id=TENANT_ID,
        account_id=account_a,
        created_at="2026-05-20T17:58:05Z",
    )
    duplicate = _tagged_agent(
        id_="ag_dup_same",
        name=spec.name,
        tenant_id=TENANT_ID,
        account_id=account_a,
        created_at="2026-05-17T22:39:01Z",
    )
    router = _router_with_agents([canonical, duplicate])

    def on_canonical_update(req: httpx.Request, _m: object) -> httpx.Response:
        return httpx.Response(
            200,
            json=_tagged_agent(
                id_="ag_canonical",
                name=spec.name,
                tenant_id=TENANT_ID,
                account_id=account_a,
                version=2,
                created_at="2026-05-20T17:58:05Z",
            ),
        )

    def on_dup_archive(req: httpx.Request, _m: object) -> httpx.Response:
        archive_calls.append("ag_dup_same")
        return httpx.Response(200, json=duplicate)

    router.add("POST", r"/v1/agents/ag_canonical$", on_canonical_update)
    router.add("POST", r"/v1/agents/ag_dup_same/archive", on_dup_archive)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(client, spec, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.UPDATED, (
        "canonical (max created_at) is updated, not the duplicate"
    )
    assert outcome.anthropic_id == "ag_canonical"
    assert archive_calls == ["ag_dup_same"], (
        "same-account duplicate must be archived (R5 dedup unbroken)"
    )


async def test_reconcile_agent_bypasses_skipped_and_repairs_corrupted_daimon_mcp() -> None:
    """When MA carries a daimon-mcp entry with a foreign URL, reconcile must
    proceed to Action.UPDATED even when daimon_spec_hash matches — the L13 short-
    circuit must be bypassed. The update body must contain exactly one daimon-mcp
    entry whose url equals public_url (the canonical entry).
    """
    from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
    from daimon.core.defaults.metadata import compute_spec_fingerprint
    from daimon.core.specs import dump_agent_spec

    spec = _agent_spec()
    # Mirror reconcile_agent's own hash computation (reconcile_agents.py:64-117):
    # apply credential guidance, then merge defaults (public_url present), then hash.
    guided = spec.model_copy(update={"system": apply_credential_guidance(spec.system or "")})
    merged_mcp = merge_default_mcp_server(guided.mcp_servers, _DEFAULT_MCP_URL)
    merged_tools = merge_default_mcp_toolset(guided.tools, _DEFAULT_MCP_URL)
    update: dict[str, object] = {}
    if merged_mcp is not guided.mcp_servers:
        update["mcp_servers"] = merged_mcp
    if merged_tools is not guided.tools:
        update["tools"] = merged_tools
    if update:
        guided = guided.model_copy(update=update)
    expected_hash = compute_spec_fingerprint(
        {
            "spec": dump_agent_spec(guided, mode="json"),
            "skills": [],
            "account_id": None,
        }
    )

    # Build the MA-side agent: hash matches, but mcp_servers carries a corrupted entry.
    corrupted_ma_agent = BetaManagedAgentsAgent.model_validate(
        {
            "id": "ag_corrupted",
            "type": "agent",
            "name": spec.name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(TENANT_ID),
                MA_METADATA_KEY_NAME: spec.name,
                "daimon_managed": "true",
                "daimon_spec_hash": expected_hash,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-05-21T00:00:00Z",
            "updated_at": "2026-05-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [{"name": "daimon-mcp", "type": "url", "url": _OTHER_MCP_URL}],
            "skills": [],
            "tools": [],
            "system": None,
        }
    ).model_dump(mode="json")

    router = _router_with_agents([corrupted_ma_agent])
    updated_payload: dict[str, Any] = {}

    def on_update(req: httpx.Request, _m: object) -> httpx.Response:
        updated_payload.update(json.loads(req.content))
        return httpx.Response(
            200,
            json=_tagged_agent(id_="ag_corrupted", name=spec.name, tenant_id=TENANT_ID, version=2),
        )

    router.add("POST", r"/v1/agents/ag_corrupted$", on_update)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.UPDATED, (
        "corrupted daimon-mcp entry must bypass the L13 SKIPPED short-circuit"
    )
    mcp_servers = updated_payload.get("mcp_servers") or []
    daimon_entries = [s for s in mcp_servers if s.get("name") == "daimon-mcp"]
    assert len(daimon_entries) == 1, (
        "update body must contain exactly one daimon-mcp entry after repair"
    )
    assert daimon_entries[0].get("url") == _DEFAULT_MCP_URL, (
        "the repaired daimon-mcp entry must carry the canonical public_url"
    )


async def test_reconcile_agent_stays_skipped_when_hash_matches_and_mcp_healthy() -> None:
    """No-churn companion: when MA carries the matching spec hash AND healthy
    mcp_servers (no corrupted daimon-mcp), reconcile must return Action.SKIPPED
    and make zero update calls.
    """
    from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
    from daimon.core.defaults.metadata import compute_spec_fingerprint
    from daimon.core.specs import dump_agent_spec

    spec = _agent_spec()
    # Mirror reconcile_agent's hash computation (same as repair test above).
    guided = spec.model_copy(update={"system": apply_credential_guidance(spec.system or "")})
    merged_mcp = merge_default_mcp_server(guided.mcp_servers, _DEFAULT_MCP_URL)
    merged_tools = merge_default_mcp_toolset(guided.tools, _DEFAULT_MCP_URL)
    update: dict[str, object] = {}
    if merged_mcp is not guided.mcp_servers:
        update["mcp_servers"] = merged_mcp
    if merged_tools is not guided.tools:
        update["tools"] = merged_tools
    if update:
        guided = guided.model_copy(update=update)
    expected_hash = compute_spec_fingerprint(
        {
            "spec": dump_agent_spec(guided, mode="json"),
            "skills": [],
            "account_id": None,
        }
    )

    healthy_ma_agent = BetaManagedAgentsAgent.model_validate(
        {
            "id": "ag_healthy",
            "type": "agent",
            "name": spec.name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(TENANT_ID),
                MA_METADATA_KEY_NAME: spec.name,
                "daimon_managed": "true",
                "daimon_spec_hash": expected_hash,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-05-21T00:00:00Z",
            "updated_at": "2026-05-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [{"name": "daimon-mcp", "type": "url", "url": _DEFAULT_MCP_URL}],
            "skills": [],
            "tools": [],
            "system": None,
        }
    ).model_dump(mode="json")

    update_called = False

    def fail_if_update_called(req: httpx.Request, _m: object) -> httpx.Response:
        nonlocal update_called
        update_called = True
        return httpx.Response(500)

    router = _router_with_agents([healthy_ma_agent])
    router.add("POST", r"/v1/agents/ag_healthy$", fail_if_update_called)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_agent(
        client, spec, tenant_id=TENANT_ID, dry_run=False, public_url=_DEFAULT_MCP_URL
    )
    assert outcome.action is Action.SKIPPED, (
        "matching hash + healthy mcp_servers must short-circuit to SKIPPED (no perpetual churn)"
    )
    assert outcome.anthropic_id == "ag_healthy"
    assert not update_called, "agents.update must NOT have been called for healthy agent"
