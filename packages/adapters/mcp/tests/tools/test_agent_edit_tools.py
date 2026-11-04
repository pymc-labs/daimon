"""Agent and library skill edits: each calls core as the caller, links to the page and turns
a core refusal into a tool error."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.mcp.tools import agent_edits as edits
from daimon.adapters.mcp.tools import skill_library_edits as library
from daimon.core.agent_editing import AgentEditError
from daimon.core.config import WebSettings
from daimon.core.skill_library_files import SkillRef
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth


@pytest.fixture
def runtime(monkeypatch):
    for module in (edits, library):
        monkeypatch.setattr(module, "_edit_deps", lambda _rt: object(), raising=False)
        monkeypatch.setattr(module, "_edit_actor", lambda _a: object(), raising=False)
    monkeypatch.setattr(edits, "_change_deps", lambda _rt: object())
    rt = fake_runtime()
    web = WebSettings(public_url="https://web.test")  # type: ignore[call-arg]
    rt.settings = SimpleNamespace(web=web)  # type: ignore[misc]
    return rt


async def test_revert_when_core_writes_then_new_version_and_agent_link(runtime, monkeypatch):
    async def fake_revert(_deps, **kw: Any):
        assert (kw["ma_agent_id"], kw["version"]) == ("ag", 2)
        return SimpleNamespace(version=5)

    monkeypatch.setattr(edits.agent_changes, "revert", fake_revert)
    auth = make_auth(admin=False)
    out = await edits._revert_impl(runtime, auth, "ag", 2)
    assert (out.reverted_to, out.version) == (2, 5), "the version restored and the one written"
    assert out.web_url == f"https://web.test/w/{auth.tenant_id}/agents/ag", "the agent's page"


async def test_revert_when_core_refuses_then_tool_error(runtime, monkeypatch):
    async def refuse(*_a: Any, **_k: Any):
        raise AgentEditError("Only an owner or an admin can edit this agent.", kind="forbidden")

    monkeypatch.setattr(edits.agent_changes, "revert", refuse)
    with pytest.raises(ToolError, match="owner or an admin"):
        await edits._revert_impl(runtime, make_auth(admin=False), "ag", 1)


async def test_set_owners_when_admin_then_refs_reach_core(runtime, monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_set(_deps, **kw: Any) -> bool:
        seen.update(kw)
        return True

    monkeypatch.setattr(edits.agent_admin, "set_owners_by_ref", fake_set)
    out = await edits._owners_impl(runtime, make_auth(), "ag", "p1", ["p2"])
    assert (seen["owner"], seen["co_owners"]) == ("p1", ["p2"]), "refs pass through unresolved"
    assert out.changed is True


async def test_save_skill_when_head_moved_then_saved_but_not_applied(runtime, monkeypatch):
    async def fake_save(_deps, **_kw: Any):
        return SimpleNamespace(skill_id="notes", version="7")

    async def conflict(*_a: Any, **_k: Any):
        raise AgentEditError("The agent changed meanwhile.", kind="conflict")

    monkeypatch.setattr(edits.agent_skill_files, "save_skill_files", fake_save)
    monkeypatch.setattr(edits.agent_changes, "apply_change", conflict)
    out = await edits._save_skill_impl(runtime, make_auth(), "ag", "notes", "6", {"a.md": "x"})
    assert (out.skill_id, out.version, out.applied) == ("notes", "7", False), "a conflict is data"


async def test_library_save_when_core_refuses_then_tool_error(runtime, monkeypatch):
    async def refuse(*_a: Any, **_k: Any):
        raise AgentEditError("Only an admin can edit the library.", kind="forbidden")

    monkeypatch.setattr(library.skill_library_files, "save_library_files", refuse)
    with pytest.raises(ToolError, match="admin"):
        await library._save_impl(runtime, make_auth(admin=False), "notes", {"a.md": "x"}, "1")


async def test_library_restore_when_admin_then_name_version_and_link(runtime, monkeypatch):
    async def fake_restore(_deps, **kw: Any):
        assert kw["version"] == "3"
        return SkillRef(id="notes", name="Notes", version="9")

    monkeypatch.setattr(library.skill_library_files, "restore_library_version", fake_restore)
    out = await library._restore_impl(runtime, make_auth(), "notes", "3")
    assert (out.name, out.version) == ("Notes", "9"), "the version written, not the one restored"
