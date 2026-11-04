"""Links into the web app for tool results, limited to what the caller may open."""

from __future__ import annotations

from typing import Annotated

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.web_links import TenantLinks, WebLinks
from pydantic import Field

# A result's link to the web app; left out of the output when there is none.
WebUrl = Annotated[str | None, Field(exclude_if=lambda value: value is None)]


def tenant_links(runtime: McpRuntime, auth: AuthIdentity) -> TenantLinks | None:
    """The caller's workspace links, or None: no web app, or a caller with no workspace page."""
    links = WebLinks.from_settings(runtime.settings.web)
    return None if links is None else links.for_tenant(auth.tenant_id, admin=auth.is_admin)
