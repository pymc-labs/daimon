"""The MCP side of the shared `daimon.core.authz` builders.

Every MCP gate describes its caller with `mcp_subject`, so a field added to
`Subject` (and populated in `daimon.core.authz.build_subject`) reaches every
MCP decision -- admission, configuration, sends, seals, fork -- at once.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.authz import Subject, build_subject


def mcp_subject(auth: AuthIdentity, *, is_admin: bool = False) -> Subject:
    """The caller as `authorize` sees it.

    ``is_admin`` is the gate's own trusted admin signal (the stored role, the
    hub exemption, the configuration guard's `_trusted_admin`); an
    agent-scoped key is marked so `authorize` never exempts it as an admin.
    """
    return build_subject(
        is_admin=is_admin,
        platform_user_id=auth.platform_user_id,
        via_agent_key=auth.agent_id is not None,
    )
