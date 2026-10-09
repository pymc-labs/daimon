"""Resource specs keep omitted apart from empty, and patches key extensions by namespace."""

from __future__ import annotations

import json

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Page, PageRequest, SkillRef
from mux.contracts.resources import AgentPatch, AgentSpec, EnvironmentSpec, SkillUpload
from mux.profiles import MANAGED_AGENTS
from pydantic import ValidationError

MODEL = ModelRef(provider="anthropic", id="claude-opus-5-5")


def test_omitted_agent_fields_are_none_and_explicit_empties_survive() -> None:
    omitted = AgentSpec(name="a", model=MODEL)
    assert (omitted.tools, omitted.mcp_servers, omitted.skills, omitted.metadata) == (None,) * 4
    sent = omitted.model_dump(exclude_none=True)
    assert {"tools", "mcp_servers", "system", "description", "metadata"}.isdisjoint(sent)

    empty = AgentSpec(name="a", model=MODEL, tools=(), mcp_servers=(), metadata={})
    restored = AgentSpec.model_validate(json.loads(empty.model_dump_json(exclude_none=True)))
    assert (restored.tools, restored.mcp_servers) == ((), ())
    assert restored.metadata is not None and dict(restored.metadata) == {}


def test_omitted_environment_fields_are_not_sent() -> None:
    spec = EnvironmentSpec(name="e")
    assert set(spec.model_dump(exclude_none=True)) == {"name", "execution"}


def test_patch_extensions_must_be_keyed_by_their_namespace() -> None:
    config = ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={"x": 1})
    assert AgentPatch(extensions={"anthropic.agent_tools": config}).extensions is not None
    with pytest.raises(ValidationError, match="keyed"):
        AgentPatch(extensions={"anthropic.multiagent": config})


def test_skill_upload_carries_binary_bytes_through_json() -> None:
    upload = SkillUpload.model_validate(
        {"files": [{"path": "a.bin", "content": b"\x00\xff"}], "display_title": "s"}
    )
    assert SkillUpload.model_validate_json(upload.model_dump_json()) == upload


def test_anthropic_offers_the_agent_tools_and_platform_export_extensions() -> None:
    for namespace in ("anthropic.agent_tools", "anthropic.platform_export"):
        assert MANAGED_AGENTS.offered_extension(namespace, 1).namespace == namespace


def test_page_request_sends_nothing_unless_asked() -> None:
    assert PageRequest().model_dump(exclude_none=True) == {}
    assert PageRequest(limit=1000).model_dump(exclude_none=True) == {"limit": 1000}


def test_page_cursor_agrees_with_has_more() -> None:
    last = Page[SkillRef](data=(SkillRef(id="s"),) * 2, has_more=False)
    assert last.next_cursor is None and len(last.data) == 2
    with pytest.raises(ValidationError, match="has_more"):
        Page[SkillRef](data=(), has_more=False, next_cursor="c")
    with pytest.raises(ValidationError, match="has_more"):
        Page[SkillRef](data=(), has_more=True)


def test_skill_ref_may_leave_the_version_to_the_provider() -> None:
    ref = SkillRef(id="skill_1", source="anthropic")
    assert ref.version is None and ref.digest is None


def test_patch_metadata_can_delete_a_key() -> None:
    patch = AgentPatch(metadata={"stale": None, "kept": "v"})
    assert patch.model_dump(exclude_unset=True) == {"metadata": {"stale": None, "kept": "v"}}
