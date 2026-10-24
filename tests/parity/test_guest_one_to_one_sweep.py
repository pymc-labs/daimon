"""Every text that mentions a 1:1 chat is accounted for, so a guest is never sent to one.

Teams opens no 1:1 chat across organisations. A string that names a 1:1 chat in the MCP
tools, the Teams adapter or core is either given a guest variant, or is shown only where
a guest cannot be (or only describes a place), and says why here. A new string that names
a 1:1 chat fails this test until it is classified, so it cannot slip past a guest.
"""

from __future__ import annotations

import ast

from .test_no_foreign_platform_names import (
    _REPO,  # pyright: ignore[reportPrivateUsage]
    _docstring_ids,  # pyright: ignore[reportPrivateUsage]
)

_ROOTS = (
    "packages/adapters/mcp/daimon",
    "packages/adapters/teams/daimon",
    "packages/core/daimon",
)
_MCP = "packages/adapters/mcp/daimon/adapters/mcp/tools/"
_TEAMS = "packages/adapters/teams/daimon/adapters/teams/"
_CORE = "packages/core/daimon/core/"

_VARIANT = "a guest gets its own text"
_SHOWN_AFTER = "reached only when a 1:1 chat was already used, which a guest has none of"

#: (file, start of the string) -> why a guest is not sent to a 1:1 chat by it.
_CLASSIFIED: dict[tuple[str, str], str] = {
    (_MCP + "github_connect.py", "I couldn't send the Connect GitHub link to your 1:1"): _VARIANT,
    (_MCP + "github_connect.py", "went to the requester's 1:1 chat"): _SHOWN_AFTER,
    (_MCP + "github_connect.py", "I sent the Connect GitHub link to our 1:1"): _SHOWN_AFTER,
    (_MCP + "microsoft/_person.py", " can only be used in the person's 1:1 chat"): _VARIANT,
    (_MCP + "microsoft/connect.py", "in the person's 1:1 chat with Daimon"): _VARIANT,
    (_MCP + "microsoft/connect.py", "The sign-in card could not reach"): _VARIANT,
    (_MCP + "microsoft/graph.py", "This file's download link is given only"): _VARIANT,
    (_MCP + "microsoft/graph.py", " in the person's 1:1 chat with Daimon."): _VARIANT,
    (_MCP + "slack/_files.py", "that file is not shared anywhere"): "Slack only",
    (_MCP + "teams/_direct.py", "the recipient is from another organisation"): (
        "explains why a recipient from another organisation is out of reach; no next step"
    ),
    (_MCP + "teams/_files.py", "Teams takes files in a channel or a 1:1"): _VARIANT,
    (_MCP + "teams/_read.py", "a 1:1 chat cannot be read back"): "describes a place to read",
    (_MCP + "teams/_send.py", "a form goes in a channel, a thread or a 1:1"): _VARIANT,
    (_TEAMS + "answer_access.py", "(an answer in a 1:1 chat)"): "a label for where an answer is",
    (_TEAMS + "commands.py", "Commands work in our 1:1 chat."): (
        "a guest is answered with NO_CHAT_ACROSS_ORGS first"
    ),
    (_TEAMS + "commands.py", "I've answered `{name}` in our 1:1 chat."): (
        "a guest is answered with NO_CHAT_ACROSS_ORGS first"
    ),
    (_TEAMS + "context.py", "Files can't be attached in a group chat. Mention it only if"): (
        "a guest gets _GROUP_FILES_HINT_FOREIGN"
    ),
    (_TEAMS + "context.py", "Files can't be attached in a group chat. This person is from"): (
        "the guest's own hint: it tells the model not to suggest a 1:1 chat"
    ),
    (_TEAMS + "installations.py", "The documented deep link"): "operator text, not shown",
    (_TEAMS + "private_replies.py", "Private questions aren't supported yet."): (
        "a guest gets NOT_YET_PRIVATE_FOREIGN"
    ),
    (_TEAMS + "setup_panel.py", "Open setup from our 1:1 chat"): "shown only inside a 1:1 chat",
    (_TEAMS + "support.py", "(sent in the 1:1 chat)"): "`support` is a command a guest cannot run",
    (_CORE + "agent_guidance.py", "## Your credentials & capabilities"): (
        "the shipped prompt; Teams sessions run platform_system's swapped passage instead"
    ),
    (_CORE + "platform_system.py", "In a Teams channel or group chat"): (
        "the shipped passage, which the Teams swap replaces with one that defers to the "
        "files hint; Discord and Slack drop it"
    ),
    (_CORE + "platform_words.py", "wherever it has been added"): "where Daimon works, a fact",
    (_CORE + "platform_words.py", "1:1 chat"): "the table; a guest's words replace it per turn",
}


def _mentions() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for root in _ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text())
            docstrings = _docstring_ids(tree)
            rel = path.relative_to(_REPO).as_posix()
            found += [
                (rel, node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "1:1" in node.value
            ]
    return found


def _is_classified(rel: str, text: str) -> bool:
    return any(rel == file and text.startswith(start) for file, start in _CLASSIFIED)


def test_every_string_naming_a_one_to_one_chat_is_accounted_for() -> None:
    unclassified = [
        f"{rel}: {text[:80]!r}" for rel, text in _mentions() if not _is_classified(rel, text)
    ]

    assert unclassified == [], (
        "a text names a 1:1 chat; give a guest its own text, or classify it in _CLASSIFIED "
        "with the reason a guest is not sent there:\n" + "\n".join(unclassified)
    )


def test_every_classification_is_still_needed() -> None:
    mentions = _mentions()
    stale = [
        f"{file}: {start!r}"
        for file, start in _CLASSIFIED
        if not any(rel == file and text.startswith(start) for rel, text in mentions)
    ]

    assert stale == [], "no longer needed, remove them:\n" + "\n".join(stale)
