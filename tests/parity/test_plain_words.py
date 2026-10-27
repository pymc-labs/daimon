"""Text a person reads, or the model relays, uses everyday words.

A member of a team is not a developer: "no turn was started", "this backend is not
enabled" and "your tenant's session" mean nothing to them. This scans the source for
string literals in the places people and the model read (cards, buttons, refusals,
notices, tool results and shown errors) and fails on a handful of internal terms,
and on a " · " separator: say it as a short sentence or put it on its own line.

What is not scanned: docstrings, comments, log calls, `assert` and the message of any
exception that is not marked for people (unmarked errors stay private, see
`daimon.core.errors`), and `Field(...)` descriptions, which are operator settings
and tool parameter notes. `_ALLOWED_FILES` and `_ALLOWED_TEXT` list what may use the
terms, each with its reason.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

from .test_no_foreign_platform_names import (
    _REPO,  # pyright: ignore[reportPrivateUsage]
    _SCANNED,  # pyright: ignore[reportPrivateUsage]
    _SKIPPED_FILES,  # pyright: ignore[reportPrivateUsage]
    _docstring_ids,  # pyright: ignore[reportPrivateUsage]
    _in_log_call,  # pyright: ignore[reportPrivateUsage]
)

#: Whole-word internal terms. "turn" is a noun here; "turn it on" is the verb and fine.
#: Code spans, paths and identifiers (`mcp_servers`, `/mnt/session/outputs`) are not prose.
_BOUNDARY_BEFORE = r"(?<![/\w.-])"
_BOUNDARY_AFTER = r"(?![/\w-])"
_TERMS = re.compile(
    _BOUNDARY_BEFORE
    + r"(?:"
    + r"turns?(?!\s+(?:\S+\s+){0,2}?(?:on|off|back|into|around|to)\b)"
    + r"|backends?|tenants?|admission|resolvers?|forks?|sessions?|responders?|deployments?"
    + r"|access\s+polic(?:y|ies)|open\s+networks?"
    + r")"
    + _BOUNDARY_AFTER,
    re.IGNORECASE,
)
#: "workspace" is Slack's word: Discord says server and Teams says organisation. It is
#: allowed in Slack's own code (the slack adapter, `tools/slack/`, `*slack*.py`) and in a
#: string that names Slack.
_WORKSPACE = re.compile(_BOUNDARY_BEFORE + r"workspaces?" + _BOUNDARY_AFTER, re.IGNORECASE)
_MCP = re.compile(_BOUNDARY_BEFORE + r"MCPs?" + _BOUNDARY_AFTER, re.IGNORECASE)
_CODE_SPAN = re.compile(r"`[^`]*`")

#: Errors marked for people. Any other exception's message is private, so it is skipped.
_SHOWN_ERRORS = frozenset(
    {
        "UserFacingError",
        "ToolError",
        "SessionRetired",
        "SkillUnavailable",
        "SkillStillAttached",
        "AccessPolicyUnreadable",
    }
)

#: Whole files that may use the terms, and why. Keep this short: reword the text instead.
_ALLOWED_FILES: dict[str, str] = {
    "packages/core/daimon/core/agent_guidance.py": "the agent's instructions; the model reads them",
    "packages/core/daimon/core/checkpoint_prompt.py": "an instruction to the model; never shown",
    "packages/core/daimon/core/handoff_context.py": "an instruction to the model; never shown",
    "packages/core/daimon/core/platform_system.py": "the system prompt; the model reads it",
    "packages/core/daimon/core/platform_words.py": "the per-platform table: Slack's own words",
    "packages/core/daimon/core/reader_agent.py": "the report reader's instructions; the model's",
    "packages/core/daimon/core/session_preparation.py": "a note the model reads; never shown",
    "packages/core/daimon/core/smoke.py": "output of the operator's smoke check, run from the CLI",
    "packages/core/daimon/core/turn_origin.py": "the per-turn note to the model; never shown",
}
#: Single strings that may use the terms: (file, start of the string) and why.
_ALLOWED_TEXT: dict[tuple[str, str], str] = {
    (
        "packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py",
        "claude mcp add",
    ): "a shell command the person pastes",
    ("packages/adapters/discord/daimon/adapters/discord/context.py", "a turn starts when"): (
        "a hint to the model; never shown"
    ),
    ("packages/core/daimon/core/github_app_session.py", "session creation failed"): (
        "an audit-log reason, read by operators"
    ),
    ("packages/core/daimon/core/github_app_session.py", "session resource and vault"): (
        "an audit-log reason, read by operators"
    ),
    ("packages/core/daimon/core/github_app_session.py", "session token replaced"): (
        "an audit-log reason, read by operators"
    ),
    ("packages/core/daimon/core/anthropic_spend.py", "You have reached your specified"): (
        "the provider's own error text, matched as it is"
    ),
    ("packages/core/daimon/core/_erasure_guards.py", "mux_account_erased(NEW.applied"): (
        "SQL naming a JSON key of a stored usage record; never shown"
    ),
    ("packages/core/daimon/core/ma.py", "Refusing to empty"): (
        "an operator's refusal about a Managed Agents workspace"
    ),
    ("packages/core/daimon/core/ma.py", "sentinel cleanup"): "an operator's authorization label",
    ("packages/core/daimon/core/ma.py", "dev lane ownership"): "an operator's authorization label",
    (
        "packages/core/daimon/core/mcp_auth.py",
        "claude mcp add",
    ): "a shell command the person pastes",
    ("packages/core/daimon/core/memory_resource.py", "Persistent memory for"): (
        "the memory store's description in the provider's console"
    ),
}
#: Not seen by a person or the model: the operator's alert webhook.
_OPERATOR_CALLS = frozenset({"alert_ops", "platform_scope"})
#: Where nobody reads text. The per-platform tool packages are scanned, unlike the
#: foreign-platform scan, since the model relays their results.
_NOT_TEXT = ("/alembic/", "/tests/")


def _scanned_files() -> list[Path]:
    return [
        path
        for root in _SCANNED
        for path in sorted((_REPO / root).rglob("*.py"))
        if not any(part in path.as_posix() for part in _NOT_TEXT)
        and not path.as_posix().endswith(_SKIPPED_FILES)
    ]


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _bases(cls: ast.ClassDef) -> set[str]:
    return {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in cls.bases}


def _enclosing_class(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.ClassDef):
            return node
    return None


def _is_marker(node: ast.AST) -> bool:
    """`_X_MARKER = "..."`: text matched in an upstream error, never shown."""
    target = (
        node.targets[0]
        if isinstance(node, ast.Assign) and node.targets
        else node.target
        if isinstance(node, ast.AnnAssign)
        else None
    )
    return isinstance(target, ast.Name) and target.id.endswith("_MARKER")


def _is_private(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True where no person reads the text.

    An `assert`; a comparison with a code (`reason != "..."`); a `Field(...)`; an
    exception that is not marked for people, whether raised or built as `XError(...)`
    or by `super().__init__` in its class."""
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Assert | ast.Compare) or _is_marker(node):
            return True
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if (
                name in _OPERATOR_CALLS | {"Field"}
                or name.endswith("Error")
                and name not in _SHOWN_ERRORS
            ):
                return True
            if name == "__init__":
                owner = _enclosing_class(node, parents)
                return owner is not None and not (_bases(owner) & _SHOWN_ERRORS)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            return _call_name(node.exc) not in _SHOWN_ERRORS
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return False
    return False


def _internal_terms(text: str, *, slack: bool = False) -> list[str]:
    prose = _CODE_SPAN.sub(" ", text)
    found = [m.group(0).lower() for m in _TERMS.finditer(prose)]
    if not slack:
        found += [m.group(0).lower() for m in _WORKSPACE.finditer(prose)]
    found += [m.group(0).upper() for m in _MCP.finditer(prose)]
    return found


def _person_facing_text(path: Path) -> Iterator[tuple[int, str]]:
    """Each string literal in `path` that a person or the model may read."""
    tree = ast.parse(path.read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if (
            id(node) in skip
            or isinstance(parents.get(node), ast.Expr)  # an attribute docstring
            or not re.search(r"\s", node.value)  # a key or a name, not prose
            or re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b|\bIN \(", node.value)
            or _in_log_call(node, parents)
            or _is_private(node, parents)
        ):
            continue
        yield node.lineno, node.value


def _offences(path: Path) -> list[tuple[int, str, list[str]]]:
    slack_code = "slack" in path.name or "/slack/" in path.as_posix()
    found: list[tuple[int, str, list[str]]] = []
    for line, text in _person_facing_text(path):
        terms = _internal_terms(text, slack=slack_code or "Slack" in text)
        if terms:
            found.append((line, text, terms))
    return found


def _allowed_text(rel: str, text: str) -> bool:
    return any(rel == file and text.startswith(start) for file, start in _ALLOWED_TEXT)


def test_person_facing_text_uses_everyday_words() -> None:
    offences: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(_REPO).as_posix()
        if rel in _ALLOWED_FILES:
            continue
        offences += [
            f"{rel}:{line}: {sorted(set(terms))} in {text[:90]!r}"
            for line, text, terms in _offences(path)
            if not _allowed_text(rel, text)
        ]

    assert offences == [], (
        "text a person reads uses an internal term; say it in everyday words "
        "(reply or message, conversation, team or workspace, model provider, connection):\n"
        + "\n".join(offences)
    )


def test_person_facing_text_has_no_dot_separators() -> None:
    offences = [
        f"{path.relative_to(_REPO).as_posix()}:{line}: {text[:90]!r}"
        for path in _scanned_files()
        for line, text in _person_facing_text(path)
        if " · " in text or text.startswith("· ") or text.endswith(" ·")
    ]

    assert offences == [], (
        'text a person reads uses a " · " separator; write a short sentence '
        "or put each part on its own line:\n" + "\n".join(offences)
    )


def test_every_allowance_is_still_needed() -> None:
    stale_files = [rel for rel in _ALLOWED_FILES if not _offences(_REPO / rel)]
    stale_text = [
        f"{rel}: {start!r}"
        for rel, start in _ALLOWED_TEXT
        if not any(text.startswith(start) for _, text, _ in _offences(_REPO / rel))
    ]

    assert stale_files + stale_text == [], "no longer needed, remove them"


def test_the_terms_match_what_they_should() -> None:
    assert _internal_terms("No DM turn was started.")
    assert _internal_terms("This backend is not enabled here.")
    assert _internal_terms("Worst-case per turn")
    assert _internal_terms("the MCP server")
    assert _internal_terms("your tenant's session")
    assert _internal_terms("This deployment is not set up")
    assert _internal_terms("The access policy can't be read")
    assert _internal_terms("an environment with open network")
    assert _internal_terms("This conversation's responder is missing")
    assert _internal_terms("no agent found in this workspace")
    assert not _internal_terms("no agent found in this workspace", slack=True)
    assert not _internal_terms("An admin can turn them on")
    assert not _internal_terms("Only a server admin can turn Daimon DMs on or off here.")
    assert not _internal_terms("no narrower scope can turn it back on")
    assert not _internal_terms("saved under /mnt/session/outputs and `mcp_servers`")
    assert not _internal_terms("run `daimon mcp revoke-token`")
