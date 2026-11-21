"""`self_knowledge` reads: web hiding, the page index and confinement to the roots."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from daimon.core.features.model_input import SessionToolset
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot
from daimon.core.features.tool_gate import SELF_KNOWLEDGE_TOOLS, server_gate_key
from daimon.core.self_knowledge import (
    MAX_MATCHES,
    SELF_KNOWLEDGE_HINT,
    Library,
    SelfKnowledgeError,
    visible_text,
)
from daimon.core.tool_safety import ToolSafetyPolicy, classify_tool

_INDEX = """# Daimon

> Daimon runs agents in chat.

## Start

- [How Daimon works](how-daimon-works.md): the overview
- [Slack](slack.md): connect Slack

## Web

- [Web app](web.md): the web app
- [Escape](../outside.md): never listed
"""
_SLACK = """# Slack

Install the app.
<!-- knowledge: web -->
Open the web app settings.
<!-- /knowledge -->
Invite the bot.
"""


def _library(tmp_path: Path, *, web: bool) -> Library:
    docs, source = tmp_path / "docs", tmp_path / "app"
    for folder in (
        docs,
        source / "packages/core",
        source / "packages/adapters/web",
        source / "defaults",  # an operator's own, never served
    ):
        folder.mkdir(parents=True)
    (docs / "llms.txt").write_text(_INDEX)
    (docs / "how-daimon-works.md").write_text("# How Daimon works\n\nAgents answer in chat.\n")
    (docs / "slack.md").write_text(_SLACK)
    (docs / "web.md").write_text("---\nknowledge: web\n---\n# Web app\n\nSign in.\n")
    (tmp_path / "outside.md").write_text("# Outside\n")
    (source / "packages/core/config.py").write_text("A = 1\nneedle = '[a-z]+'\n")
    (source / "packages/core/.env").write_text("SECRET=1\n")
    (source / "defaults/agents.yaml").write_text("prompt: needle\n")
    (source / "packages/adapters/web/app.py").write_text("needle = 2\n")
    (source / "pyproject.toml").write_text("needle\n")
    (tmp_path / "secret.py").write_text("needle\n")
    (source / "packages/core/link.py").symlink_to(tmp_path / "secret.py")
    (source / "packages/core/dotlink.py").symlink_to(source / "packages/core/.env")
    return Library(
        docs, source, "https://docs.example.com/", "https://git.example.com/d", "abc123", web
    )


def test_visible_text_hides_web_pages_and_blocks_only_when_web_is_off() -> None:
    page = "---\nknowledge: web\n---\n# Web\n"
    assert visible_text(page, web=False) is None, "a web page is hidden with web off"
    assert visible_text(page, web=True) == page.rstrip("\n"), "and shown with web on"
    assert visible_text(_SLACK, web=False) == "# Slack\n\nInstall the app.\nInvite the bot."
    assert "Open the web app settings." in (visible_text(_SLACK, web=True) or ""), "block kept"
    assert "knowledge" not in (visible_text(_SLACK, web=True) or ""), "markers never shown"
    unclosed = "a\n<!-- knowledge: web -->\nb\n"
    assert visible_text(unclosed, web=False) == "a", "an unclosed block hides the rest"
    fenced = "a\n```md\n<!-- knowledge: web -->\n```\nb\n"
    assert visible_text(fenced, web=False) == fenced.rstrip("\n"), "a fenced marker is text"


def test_overview_lists_visible_pages_and_names_the_release(tmp_path: Path) -> None:
    off = _library(tmp_path, web=False).read_docs()
    assert "abc123" in off and "https://git.example.com/d" in off, "names the repo and commit"
    assert "Agents answer in chat." in off and "Daimon runs agents in chat." in off
    assert "- slack.md (Slack): connect Slack" in off, "lists a page"
    assert "web.md" not in off and "### Web" not in off, "a web page is left out with web off"
    assert "outside.md" not in off, "an entry outside the docs root is never listed"
    assert "- web.md (Web app)" in _library(tmp_path / "on", web=True).read_docs()


def test_pages_and_search_follow_web_awareness(tmp_path: Path) -> None:
    library = _library(tmp_path, web=False)
    with pytest.raises(SelfKnowledgeError):
        library.read_docs("web.md")
    assert "Open the web app" not in library.read_docs("slack.md"), "web block stripped"
    assert "web.md" not in library.search_docs("sign in"), "hidden pages are not searched"
    assert "slack.md:4: Invite the bot." in library.search_docs("INVITE"), "case-insensitive"
    with pytest.raises(SelfKnowledgeError):
        library.read_source("packages/adapters/web/app.py")
    assert "adapters/web" not in library.search_source("needle"), "web source hidden"
    on = _library(tmp_path / "on", web=True)
    assert "packages/adapters/web/app.py:1" in on.search_source("needle"), "shown with web on"


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../secret.py", "packages/../../secret.py", "packages/core/.env",
     "packages/core/link.py", "packages/core/dotlink.py", "pyproject.toml", "docs",
     "defaults", "defaults/agents.yaml"],
)  # fmt: skip
def test_source_reads_stay_inside_packages(tmp_path: Path, path: str) -> None:
    with pytest.raises(SelfKnowledgeError):
        _library(tmp_path, web=True).read_source(path)


def test_source_lists_reads_and_searches_text_literally(tmp_path: Path) -> None:
    library = _library(tmp_path, web=True)
    assert library.read_source().endswith("./\npackages/"), "packages only, never defaults"
    listing = library.read_source("packages/core")
    assert "config.py" in listing and ".env" not in listing, "dotfiles are never listed"
    assert "    2  needle = '[a-z]+'" in library.read_source("packages/core/config.py", 2)
    found = library.search_source("[a-z]+")
    assert "packages/core/config.py:2" in found, "a query is plain text, not a pattern"
    assert "secret" not in library.search_source("needle"), "no symlink out of the root"
    assert "defaults" not in library.search_source("needle"), "defaults/ is never searched"
    with pytest.raises(SelfKnowledgeError):
        library.search_source("x")


def test_search_stops_at_the_match_cap(tmp_path: Path) -> None:
    library = _library(tmp_path, web=True)
    (library.source / "packages/core/many.md").write_text("hit\n" * (MAX_MATCHES + 5))
    found = library.search_source("hit", "packages/core")
    assert found.count("packages/core/many.md:") == MAX_MATCHES, "capped"
    assert "search for something narrower" in found


def test_self_knowledge_off_hides_and_refuses_its_read_only_tools() -> None:
    place = FeaturePlace(uuid.UUID(int=1), "slack", "C1", "T1")
    off = FeatureSnapshot(
        place, 0, {"self_knowledge": ResolvedFeature("self_knowledge", "off", "default")}
    )
    on = FeatureSnapshot(
        place, 0, {"self_knowledge": ResolvedFeature("self_knowledge", "on", "agent")}
    )
    assert SessionToolset.of(off, {}).hidden_daimon_tools >= SELF_KNOWLEDGE_TOOLS, "Off hides"
    assert not SessionToolset.of(on, {}).hidden_daimon_tools & SELF_KNOWLEDGE_TOOLS, "On offers"
    assert all(server_gate_key(tool) == "self_knowledge" for tool in SELF_KNOWLEDGE_TOOLS)
    policy = ToolSafetyPolicy()
    effects = {
        classify_tool(policy, server_name="daimon-mcp", tool_name=t) for t in SELF_KNOWLEDGE_TOOLS
    }
    assert effects == {"read"}, "every tool reads"
    assert all(tool in SELF_KNOWLEDGE_HINT for tool in SELF_KNOWLEDGE_TOOLS), "the hint names them"
