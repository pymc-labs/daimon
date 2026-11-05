"""Server skill chat tools: the web routes' rules, as the caller, in the caller's workspace."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _card
from daimon.adapters.mcp.tools import server_skills as tools
from daimon.adapters.mcp.tools.server_skills import (
    _add_impl,  # pyright: ignore[reportPrivateUsage]
    _apply_impl,  # pyright: ignore[reportPrivateUsage]
    _approve_impl,  # pyright: ignore[reportPrivateUsage]
    _check_impl,  # pyright: ignore[reportPrivateUsage]
    _follow_impl,  # pyright: ignore[reportPrivateUsage]
    _fork_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_editing import AgentEditError
from daimon.core.mcp_skills.protocol import fetch_serves_skills
from daimon.core.server_skills.node import ServerSkillOption, ServesCache, SkillsNode
from daimon.core.skill_library_files import SkillRef
from daimon.core.stores import server_skills as store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import Role, ServerSkillRow, TenantRow
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_NODE = cast(SkillsNode, SimpleNamespace(label="Data", server=SimpleNamespace(name="data")))


def make_auth(
    *, admin: bool, account_id: uuid.UUID, tenant_id: uuid.UUID, **extra: Any
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        platform_user_id="u1",
        is_admin=admin,
        is_platform_admin=False,
        **extra,
    )


@pytest.fixture
def carded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every call a person confirms on a card; the tool names that asked."""
    asked: list[str] = []

    async def confirmed(*_a: Any, tool_name: str, **_k: Any) -> None:
        asked.append(tool_name)

    monkeypatch.setattr(tools, "require_card", confirmed)
    return asked


@pytest.fixture
def runtime(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    carded: list[str],
) -> McpRuntime:
    async def no_links(*_a: Any) -> None:
        return None

    def deps(*_a: object) -> object:
        return object()

    def node(*_a: object) -> SkillsNode:
        return _NODE

    monkeypatch.setattr(tools, "_edit_deps", deps)
    monkeypatch.setattr(tools, "_sync_deps", deps)
    monkeypatch.setattr(tools, "skills_node_of", node)
    monkeypatch.setattr(tools, "tenant_links", no_links)
    rt = SimpleNamespace(
        session_factory=db_session_factory,
        settings=object(),
        fernet=None,
        identity_key=None,
        node_held={},
    )
    return cast(McpRuntime, rt)


async def _member(
    db: AsyncSession, tenant: TenantRow, user: str, *, admin: bool = False, suspended: bool = False
) -> AuthIdentity:
    account = await make_account(db, tenant=tenant)
    await make_platform_principal(
        db, platform="discord", external_id=user, tenant=tenant, account=account
    )
    if suspended:
        person = await make_web_person(db, tenant=tenant, account=account)
        await people_store.update_person(db, person_id=person.id, status="suspended")
    await db.commit()
    return make_auth(admin=admin, account_id=account.id, tenant_id=tenant.id)


async def _mirror(db: AsyncSession, tenant: TenantRow, skill_id: str, **kw: Any) -> ServerSkillRow:
    row = await store.create_mirror(
        db,
        tenant_id=tenant.id,
        server="data",
        uri=f"skill://{skill_id}",
        name=skill_id,
        skill_id=skill_id,
        version_label=None,
        digest="d",
        manifest={},
        allowed_tools=None,
        added_by_account_id=None,
        now=_NOW,
    )
    if kw:
        await store.hold_for_approval(db, row.id, now=_NOW, **kw)
    await db.commit()
    return row


async def test_add_without_a_name_lists_what_the_server_offers_the_caller(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    asked: list[uuid.UUID] = []

    async def serves(_ex: object, _http: object, _node: object, asker: Any, _call: object) -> bool:
        asked.append(asker.account_id)
        return True

    async def listing(
        _ex: object, _http: object, _node: object, asker: Any, **kw: Any
    ) -> list[ServerSkillOption]:
        assert kw["actor"].tenant_id == tenant.id, "marked against the caller's own library"
        asked.append(asker.account_id)
        return [
            ServerSkillOption(uri="skill://p", name="p", description="", skill_id=None, taken=False)
        ]

    monkeypatch.setattr(tools, "ask_node", serves)
    monkeypatch.setattr(tools, "list_server_skills", listing)
    out = await _add_impl(runtime, auth, None, ServesCache())
    assert [o.name for o in out.offered] == ["p"] and out.added is None, "a menu, nothing added"
    assert asked == [auth.account_id, auth.account_id], "the server is asked as the caller"


async def test_add_by_name_mirrors_it_into_the_callers_library(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    fetched = object()
    seen: dict[str, Any] = {}

    async def node(_ex: object, _http: object, _node: object, _asker: object, call: Any) -> Any:
        return True if call is fetch_serves_skills else fetched

    async def add(_deps: object, **kw: Any) -> SkillRef:
        seen.update(kw)
        return SkillRef(id="sk_1", name="plots", version="1")

    monkeypatch.setattr(tools, "ask_node", node)
    monkeypatch.setattr(tools, "add_server_skill", add)
    out = await _add_impl(runtime, auth, "plots", ServesCache(), "ctx")
    assert out.added is not None and out.added.skill_id == "sk_1", "the library skill"
    assert (seen["fetched"], seen["server"]) == (fetched, "data"), "what the server served"
    assert seen["actor"].tenant_id == tenant.id, "into the caller's workspace"


async def test_add_needs_a_chat_account_and_an_unsuspended_person(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    other = await make_tenant(db_session, workspace_id="g-2")
    stranger = await _member(db_session, other, "u-9")

    async def never(*_a: object) -> None:
        pytest.fail("never asked")

    monkeypatch.setattr(tools, "ask_node", never)
    outsider = make_auth(admin=False, account_id=stranger.account_id, tenant_id=tenant.id)
    with pytest.raises(ToolError, match="serves only chat accounts"):
        await _add_impl(runtime, outsider, "plots", ServesCache(), "ctx")

    auth = await _member(db_session, tenant, "u-1", suspended=True)
    with pytest.raises(ToolError, match="suspended"):
        await _add_impl(runtime, auth, "plots", ServesCache(), "ctx")


async def test_check_is_for_admins_and_only_the_callers_skills(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    other = await make_tenant(db_session, workspace_id="g-2")
    await _mirror(db_session, tenant, "sk_mine")
    await _mirror(db_session, other, "sk_theirs")
    admin = await _member(db_session, tenant, "u-1", admin=True)
    member = await _member(db_session, tenant, "u-2")

    async def checked(_deps: object, _mirror_id: uuid.UUID) -> str:
        return "unchanged"

    monkeypatch.setattr(tools, "check_mirror", checked)
    out = await _check_impl(runtime, admin, "sk_mine")
    assert out.result == "unchanged" and out.server is not None, "checked, with its source"
    with pytest.raises(ToolError, match="No such server skill"):
        await _check_impl(runtime, admin, "sk_theirs")
    with pytest.raises(ToolError, match="admin"):
        await _check_impl(runtime, member, "sk_mine")
    with pytest.raises(ToolError, match="No update is waiting"):
        await _approve_impl(runtime, admin, "sk_mine", "digest", "ctx")


async def test_approve_applies_the_update_the_admin_reviewed(
    runtime: McpRuntime,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    carded: list[str],
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    await _mirror(db_session, tenant, "sk_1", digest="new", version_label=None, changes={})
    admin = await _member(db_session, tenant, "u-1", admin=True)
    seen: list[str] = []

    async def approve(_deps: object, _mirror_id: uuid.UUID, *, digest: str) -> str:
        seen.append(digest)
        return "updated"

    monkeypatch.setattr(tools, "approve_update", approve)
    out = await _approve_impl(runtime, admin, "sk_1", "new", "ctx")
    assert (out.result, seen) == ("updated", ["new"]), "that update, as reviewed"
    assert carded == ["approve_server_skill_update"], "a person confirmed it first"


async def test_fork_is_for_admins(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    member = await _member(db_session, tenant, "u-2")
    with pytest.raises(ToolError, match="admin"):
        await _fork_impl(runtime, member, "sk_1", "my-plots")

    async def fork(_deps: object, **kw: Any) -> SkillRef:
        assert kw["actor"].tenant_id == tenant.id, "in the caller's workspace"
        return SkillRef(id="sk_2", name=kw["name"], version="1")

    monkeypatch.setattr(tools, "fork_library_skill", fork)
    admin = await _member(db_session, tenant, "u-1", admin=True)
    out = await _fork_impl(runtime, admin, "sk_1", "my-plots")
    assert (out.skill_id, out.name) == ("sk_2", "my-plots"), "the editable copy"


async def test_follow_moves_the_agent_at_once_and_passes_on_refusals(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    moved: list[tuple[uuid.UUID, str, str]] = []

    async def follow(_deps: object, **kw: Any) -> None:
        if kw["agent_id"] == "ag_other":
            raise AgentEditError("Only an owner or an admin can edit this agent.", kind="forbidden")

    async def now(_deps: object, *, tenant_id: uuid.UUID, skill_id: str, agent_id: str) -> None:
        moved.append((tenant_id, skill_id, agent_id))

    monkeypatch.setattr(tools, "set_agent_follow", follow)
    monkeypatch.setattr(tools, "follow_now", now)
    out = await _follow_impl(runtime, auth, "ag_1", "sk_1", True)
    assert "latest version" in out, "the agent is moved now"
    assert moved == [(tenant.id, "sk_1", "ag_1")], "in the caller's workspace"
    with pytest.raises(ToolError, match="owner or an admin"):
        await _follow_impl(runtime, auth, "ag_other", "sk_1", False)


async def test_apply_hands_the_held_update_over_as_the_caller(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    seen: dict[str, Any] = {}

    async def apply(_deps: object, **kw: Any) -> None:
        seen.update(kw)
        if kw["agent_id"] == "ag_other":
            raise AgentEditError("Only an owner or an admin can edit this agent.", kind="forbidden")

    monkeypatch.setattr(tools, "apply_held_update", apply)
    out = await _apply_impl(runtime, auth, "ag_1", "sk_1")
    assert "has the update" in out, "applied"
    assert seen["actor"].account_id == auth.account_id, "as the caller"
    with pytest.raises(ToolError, match="owner or an admin"):
        await _apply_impl(runtime, auth, "ag_other", "sk_1")


async def test_add_and_approve_when_unattended_then_refused_before_any_change(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    await _mirror(db_session, tenant, "sk_1", digest="new", version_label=None, changes={})
    admin = await _member(db_session, tenant, "u-1", admin=True)
    routine = make_auth(
        admin=True, account_id=admin.account_id, tenant_id=tenant.id, routine_id=uuid.uuid4()
    )

    async def never(*_a: object, **_k: object) -> None:
        pytest.fail("nothing is asked or changed without a card")

    monkeypatch.setattr(tools, "require_card", _card.require_card)
    for name in ("ask_node", "approve_update", "add_server_skill"):
        monkeypatch.setattr(tools, name, never)
    with pytest.raises(ToolError, match="Nothing changed"):
        await _approve_impl(runtime, routine, "sk_1", "new", "ctx")
    with pytest.raises(ToolError, match="Nothing changed"):
        await _add_impl(runtime, routine, "plots", ServesCache(), "ctx")


async def test_add_without_a_name_needs_no_card(
    runtime: McpRuntime,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    carded: list[str],
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")

    async def serves(*_a: object) -> bool:
        return True

    async def listing(*_a: object, **_k: object) -> list[ServerSkillOption]:
        return []

    monkeypatch.setattr(tools, "ask_node", serves)
    monkeypatch.setattr(tools, "list_server_skills", listing)
    await _add_impl(runtime, auth, None, ServesCache())
    assert carded == [], "listing changes nothing, so it asks nobody"


async def test_add_by_a_name_two_skills_share_lists_their_uris(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    raw = [
        {
            "uri": f"skill://{where}/plots/SKILL.md",
            "frontmatter": {"name": "plots", "description": "d"},
            "resources": [
                {
                    "uri": f"skill://{where}/plots/SKILL.md",
                    "digest": "sha256:" + "a" * 64,
                    "size": 1,
                }
            ],
        }
        for where in ("a", "b")
    ]

    async def node(_ex: object, _http: object, _node: object, _asker: object, call: Any) -> Any:
        if call is fetch_serves_skills:
            return True
        return await call(object(), object())

    async def listing(_http: object, _endpoint: object, *, keep: object) -> list[dict[str, str]]:
        return raw

    async def never(*_a: object, **_k: object) -> None:
        pytest.fail("nothing is fetched or added while the name is ambiguous")

    monkeypatch.setattr(tools, "ask_node", node)
    monkeypatch.setattr(tools, "fetch_skill_listing", listing)
    monkeypatch.setattr(tools, "fetch_server_skill", never)
    monkeypatch.setattr(tools, "add_server_skill", never)
    with pytest.raises(ToolError, match="more than one skill named plots") as err:
        await _add_impl(runtime, auth, "plots", ServesCache(), "ctx")
    assert all(f"skill://{where}/plots/SKILL.md" in str(err.value) for where in ("a", "b")), (
        "the caller can pick one by its URI"
    )


async def test_add_remembers_whether_the_server_serves_skills(
    runtime: McpRuntime, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="g-1")
    auth = await _member(db_session, tenant, "u-1")
    asked: list[object] = []

    async def node(_ex: object, _http: object, _node: object, _asker: object, call: Any) -> Any:
        asked.append(call)
        return True if call is fetch_serves_skills else []

    async def listing(*_a: object, **_k: object) -> list[ServerSkillOption]:
        return []

    monkeypatch.setattr(tools, "ask_node", node)
    monkeypatch.setattr(tools, "list_server_skills", listing)
    cache = ServesCache()
    await _add_impl(runtime, auth, None, cache)
    await _add_impl(runtime, auth, None, cache)
    assert asked.count(fetch_serves_skills) == 1, "the second call reuses the answer"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("add_server_skill", {"skill": "x" * 2049}),
        ("add_server_skill", {"skill": ""}),
        ("check_server_skill", {"skill_id": "x" * 201}),
        (
            "approve_server_skill_update",
            {"skill_id": "s", "digest": "x" * 129, "origin_context_id": "o"},
        ),
        ("fork_server_skill", {"skill_id": "s", "name": "x" * 65}),
        ("set_server_skill_follow", {"agent_id": "x" * 201, "skill_id": "s", "follow": True}),
    ],
    ids=["long_uri", "empty_uri", "long_skill_id", "long_digest", "long_name", "long_agent_id"],
)
async def test_tools_when_an_input_is_out_of_bounds_then_refused(
    runtime: McpRuntime, tool: str, args: dict[str, Any]
) -> None:
    mcp = FastMCP("test")
    tools.register_server_skill_tools(mcp, runtime)
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(tool, args)
