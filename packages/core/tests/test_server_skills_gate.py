"""What a server skill update changes, whether it waits for approval, and who moves."""

import pytest
from daimon.core.server_skills.gate import (
    AttachedAgent,
    Repin,
    diff_manifests,
    manifest_digest,
    needs_approval,
    plan_repins,
)


def test_digest_names_the_files_not_their_order() -> None:
    assert manifest_digest({"a": "1", "b": "2"}) == manifest_digest({"b": "2", "a": "1"})
    assert manifest_digest({"a": "1"}) != manifest_digest({"a": "2"})


def test_diff_lists_added_removed_and_changed_paths() -> None:
    changes = diff_manifests({"SKILL.md": "1", "old.md": "1"}, {"SKILL.md": "2", "new.md": "1"})
    assert changes.as_json() == {
        "added": ["new.md"],
        "removed": ["old.md"],
        "changed": ["SKILL.md"],
    }


def test_text_changes_apply_without_approval() -> None:
    changes = diff_manifests({"SKILL.md": "1"}, {"SKILL.md": "2", "notes.md": "1"})
    files = {"SKILL.md": b"x", "notes.md": b"y"}
    assert not needs_approval(changes, files, old_tools=None, new_tools=None)


@pytest.mark.parametrize(
    ("path", "body"), [("run.py", b"print(1)\n"), ("tool", b"#!/bin/sh\necho\n")]
)
def test_a_new_or_changed_script_waits(path: str, body: bytes) -> None:
    added = diff_manifests({"SKILL.md": "1"}, {"SKILL.md": "1", path: "1"})
    changed = diff_manifests({path: "1"}, {path: "2"})
    for changes in (added, changed):
        assert needs_approval(changes, {path: body}, old_tools=None, new_tools=None)


def test_a_removed_script_applies() -> None:
    changes = diff_manifests({"run.py": "1"}, {})
    assert not needs_approval(changes, {}, old_tools=None, new_tools=None)


def test_changed_allowed_tools_wait() -> None:
    changes = diff_manifests({"SKILL.md": "1"}, {"SKILL.md": "2"})
    assert needs_approval(changes, {"SKILL.md": b"x"}, old_tools=None, new_tools="Bash")


def test_repins_skip_pinned_agents_and_those_already_there() -> None:
    agents = [AttachedAgent("a", "1"), AttachedAgent("b", "1"), AttachedAgent("c", "2")]
    assert plan_repins(agents, version="2", pinned={"b"}) == [Repin("a", "2")]


@pytest.mark.parametrize("version", ["", "latest"])
def test_repins_need_a_concrete_version(version: str) -> None:
    with pytest.raises(ValueError, match="concrete"):
        plan_repins([AttachedAgent("a", "1")], version=version, pinned=())
