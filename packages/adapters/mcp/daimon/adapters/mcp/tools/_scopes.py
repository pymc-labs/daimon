"""Operator-token scopes on MCP tools: the tags that grant a scope, and the re-check.

A tool an operator token may call carries ``scope_tags(<scope>)`` among its
tags and calls ``require_scope`` before its own gate. Other callers are
unaffected: the tag only re-enables the tool for an operator token holding
the scope (``IdentityMiddleware`` disables every other tool for it), and
``require_scope`` lets any other identity through to ``_require_admin`` or
whatever the tool checks next.

To open a later ``channels:write`` tool (isolation, environment) to
operator tokens: tag it
``{"admin", *scope_tags("channels:write")}``, call
``require_scope(auth, "channels:write")`` first, and list it under the scope
in docs/architecture.md. The verifier, the middleware filter and the audit
row need no change.

``promo:create`` tools are operator-only: they carry no ``admin`` tag, a
baseline in server.py hides them from everyone, and
``require_operator_scope`` refuses any other caller.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.operator_tokens import OperatorScope, scope_tag
from daimon.core.security_audit import record_scope_decision
from fastmcp.exceptions import ToolError


def scope_tags(*scopes: OperatorScope) -> set[str]:
    return {scope_tag(scope) for scope in scopes}


def require_scope(auth: AuthIdentity, scope: OperatorScope) -> None:
    """Refuse an operator token without ``scope``; any other caller passes."""
    if not auth.is_operator:
        return
    allowed = scope in auth.scopes
    record_scope_decision(scope, allowed=allowed)
    if not allowed:
        raise ToolError(f"This operator token does not have the {scope} scope.")


def require_operator_scope(auth: AuthIdentity, scope: OperatorScope) -> None:
    """Allow only an operator token holding ``scope``."""
    if not auth.is_operator:
        record_scope_decision(scope, allowed=False)
        raise ToolError(f"Only an operator token with the {scope} scope can call this tool.")
    require_scope(auth, scope)
