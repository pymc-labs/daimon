"""Rule contract: every Connect GitHub link opens on the root `connect_root` picks.

With the web app on, the links in the web panel, the chat tool, `/github connect`,
the CLI, request delivery and the pending resume all open its pages; without it,
the MCP host's. A builder that reads its own root drifts, so this walks every
production module for a `/oauth/github/connect/` link and fails unless an
enclosing function calls `connect_root` or the builder is listed below with why.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cryptography.fernet import Fernet
from daimon.core.config import Settings
from daimon.core.github_panel import connect_root

_PACKAGES = Path(__file__).resolve().parents[2] / "packages"
_LINK = "/oauth/github/connect/"
# (module path under packages/, function) -> why it may take its root elsewhere.
_OWN_ROOT = {
    ("core/daimon/core/agent_github.py", "connect_url"): (
        "root injected; the web panel passes connect_root"
    ),
    (
        "core/daimon/core/github_connect_flow.py",
        "callback",
    ): "the retry link stays on the flow's host",
    (
        "core/daimon/core/github_connect_flow.py",
        "_confirm",
    ): "the share link stays on the flow's host",
    (
        "adapters/mcp/daimon/adapters/mcp/oauth_github.py",
        "connect",
    ): "redirects old links to the web app",
}


def _builders() -> set[tuple[str, str]]:
    """(module, innermost function) of each link built with no `connect_root` around it."""
    found: set[tuple[str, str]] = set()
    for path in _PACKAGES.glob("**/daimon/**/*.py"):
        if "tests" in path.parts:
            continue
        module = path.relative_to(_PACKAGES).as_posix()

        def walk(node: ast.AST, stack: list[ast.AST], module: str = module) -> None:
            for child in ast.iter_child_nodes(node):
                inner = stack
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    inner = [*stack, child]
                if isinstance(child, ast.JoinedStr) and any(
                    isinstance(part, ast.Constant) and _LINK in str(part.value)
                    for part in child.values
                ):
                    calls = {
                        getattr(n.func, "id", getattr(n.func, "attr", None))
                        for fn in stack
                        for n in ast.walk(fn)
                        if isinstance(n, ast.Call)
                    }
                    if "connect_root" not in calls:
                        name = getattr(stack[-1], "name", "<module>") if stack else "<module>"
                        found.add((module, name))
                walk(child, inner)

        walk(ast.parse(path.read_text()), [])
    return found


def test_connect_links_take_their_root_from_connect_root() -> None:
    """Every link builder reaches `connect_root`, or is a listed exception."""
    assert _builders() == set(_OWN_ROOT), "a Connect GitHub link builds on its own root"


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        github_app={  # pyright: ignore[reportArgumentType]
            "app_id": "1",
            "app_slug": "app",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
            "client_id": "c",
            "client_secret": "s",
        },
        mcp={"public_url": "https://mcp.example.com/mcp"},  # pyright: ignore[reportArgumentType]
        crypto={"keys": [Fernet.generate_key().decode()]},  # pyright: ignore[reportArgumentType]
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def test_connect_root_prefers_the_web_app() -> None:
    """Links open in the web app when it runs, else on the MCP host."""
    web = _settings(web={"public_url": "https://web.example.com/"})
    assert connect_root(web) == "https://web.example.com", "the web app should host the flow"
    assert connect_root(_settings()) == "https://mcp.example.com", "MCP hosts it without web"
