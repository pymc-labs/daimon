"""Every "may this agent use this relay server" decision goes through one rule.

`SignedIdentityServer.allows_agent` is the rule: a name or an agent id in `agents`.
A second copy that compared names only locked a renamed agent out of its server
(the relay route and the tool catalogues each had one). This test finds, from the
AST of the source (not the tests), each read of `.agents` that could be a relay
server's list, and fails for any outside `ALLOWED_READS`. Two ways of finding them:
a read on an expression named like a server (`server`, `relay_server`, ...) in any
package, and any `.agents` read in a module that imports `SignedIdentityServer` or
is one of the two relay-rule modules. A module with neither is not checked, so a
new reader that avoids both has to be caught in review; the list stays short.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RELAY_MODULES = {
    "core/daimon/core/mcp_tool_annotations.py",
    "adapters/mcp/daimon/adapters/mcp/identity_relay.py",
}

# (file under packages/, enclosing function, expression): the reason it is not a copy of the rule.
ALLOWED_READS: dict[tuple[str, str, str], str] = {
    ("core/daimon/core/config.py", "allows_agent", "self.agents"): "the rule itself",
    ("core/daimon/core/config.py", "_auth_fields", "self.agents"): "a bearer server needs a list",
    (
        "adapters/mcp/daimon/adapters/mcp/identity_relay.py",
        "_listed_as_builtin",
        "server.agents",
    ): "the seeded built-in's own rename, by name; it runs after the rule refused",
    (
        "core/daimon/core/agent_rename.py",
        "_refuse_relay_lockout",
        "server.agents",
    ): "asks whether the OLD name is listed, to guard a rename; not an access decision",
    **{
        (
            "adapters/web/daimon/adapters/web/nodes/data_server.py",
            function,
            "runtime.agents",
        ): "the web runtime's agent service, not a relay list"
        for function in ("connect", "disconnect")
    },
}

_SERVERISH = re.compile(r"(^|\.)\w*server\w*$", re.IGNORECASE)


def _sources() -> list[Path]:
    return [
        p
        for p in _ROOT.glob("*/**/*.py")
        if "tests" not in p.relative_to(_ROOT).parts and "node_modules" not in p.parts
    ]


def _reads(path: Path) -> set[tuple[str, str, str]]:
    source = path.read_text()
    if ".agents" not in source:
        return set()
    rel = path.relative_to(_ROOT).as_posix()
    tree = ast.parse(source)
    imports_server = (
        any(
            isinstance(n, ast.ImportFrom) and any(a.name == "SignedIdentityServer" for a in n.names)
            for n in ast.walk(tree)
        )
        or rel == "core/daimon/core/config.py"
    )
    strict = imports_server or rel in _RELAY_MODULES
    found: set[tuple[str, str, str]] = set()

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        if isinstance(node, ast.Attribute) and node.attr == "agents":
            base = ast.unparse(node.value)
            if strict or _SERVERISH.search(base):
                found.add((rel, function, f"{base}.agents"))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_every_relay_agent_list_read_is_the_one_rule_or_listed() -> None:
    sources = _sources()
    assert len(sources) > 200, "the scan found too few source files"
    found = {read for path in sources for read in _reads(path)}
    assert found - set(ALLOWED_READS) == set(), (
        "a read of a relay server's `agents` outside `SignedIdentityServer.allows_agent`"
    )
    assert set(ALLOWED_READS) - found == set(), "an allow-list entry no longer matches any read"
