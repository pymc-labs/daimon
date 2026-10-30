"""One skill an agent's MCP server serves, read and verified for `add_skill`.

Nothing here runs unless a person asks to add the skill: `add_skill` previews
what this returns and uploads it only after their approval. The server is
asked the way the agent reaches it: with its shared token, or, behind the
identity relay, with a signed token for the person asking. A server that signs
each person in separately has no token Daimon may use, so it is refused.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx
from daimon.core.agent_mcp_credentials import ResolvedMcpCredential
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.mcp_skills.manifest import (
    SkillEntry,
    SkillRejected,
    parse_entry,
    verify_file,
    verify_frontmatter,
)
from daimon.core.mcp_skills.protocol import (
    Endpoint,
    fetch_resource,
    fetch_serves_skills,
    fetch_skill_listing,
)
from daimon.core.skills.ingest import SkillIngestError
from pydantic import JsonValue

__all__ = ["ServerAccess", "ServerSkill", "fetch_server_skill", "server_access"]


@dataclass(frozen=True, slots=True)
class ServerAccess:
    """How Daimon itself reaches one of the agent's servers."""

    endpoint: Endpoint
    verify: bool | ssl.SSLContext = True


@dataclass(frozen=True, slots=True)
class ServerSkill:
    """A verified skill: its listing entry and every file, keyed by path."""

    entry: SkillEntry
    files: dict[str, bytes]

    def origin(self, server: str) -> str:
        """Where it came from, for the skill ledger: server, URI and manifest digest."""
        return f"mcp {server}: {self.entry.uri} sha256:{self.entry.manifest_digest}"


def server_access(
    *,
    server: str,
    url: str,
    credentials: Sequence[ResolvedMcpCredential],
    relay_base: str | None,
    relay_servers: Mapping[str, SignedIdentityServer],
    relay_token: Callable[[SignedIdentityServer], str | None],
) -> ServerAccess:
    """Reach `url` as the agent does: a relay URL with a signed token, else its shared token.

    Raises `SkillIngestError` for a server Daimon may not ask.
    """
    canonical = canonical_mcp_url(url)
    if relay_base is not None and canonical.startswith(relay_base):
        configured = relay_servers.get(canonical[len(relay_base) :])
        token = relay_token(configured) if configured is not None else None
        if configured is None or token is None:
            raise SkillIngestError(f"Daimon cannot sign requests to '{server}' from here.")
        verify: bool | ssl.SSLContext = True
        if configured.ca_certificate is not None:
            verify = ssl.create_default_context(cadata=configured.ca_certificate)
        return ServerAccess(Endpoint(url=configured.url, bearer=token), verify)
    by_url = {canonical_mcp_url(c.mcp_server_url): c for c in credentials}
    credential = by_url.get(canonical)
    if credential is None:
        raise SkillIngestError(
            f"'{server}' signs each person in separately, so Daimon cannot read its skills."
        )
    try:
        assert_public_https_url(url, what="MCP server URL")
    except McpUrlError as err:
        raise SkillIngestError(f"Daimon cannot read skills from '{server}': {err}") from err
    return ServerAccess(Endpoint(url=url, bearer=credential.token))


def _is_wanted(raw: JsonValue, skill: str) -> bool:
    """Whether a listing entry is `skill`, given by name or by its `SKILL.md` URI."""
    if not isinstance(raw, dict):
        return False
    frontmatter = raw.get("frontmatter")
    name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
    return skill in (raw.get("uri"), name)


async def fetch_server_skill(
    http: httpx.AsyncClient, endpoint: Endpoint, *, server: str, skill: str
) -> ServerSkill:
    """The skill named `skill` (a name or `skill://` URI), every file verified.

    Raises `SkillIngestError` when the server serves no such skill, or a check fails.
    """
    try:
        if not await fetch_serves_skills(http, endpoint):
            raise SkillIngestError(f"'{server}' does not serve skills.")
        listing = await fetch_skill_listing(http, endpoint)
        matches = [raw for raw in listing.entries if _is_wanted(raw, skill)]
        if not matches:
            raise SkillIngestError(f"'{server}' serves no skill '{skill}'.")
        if len(matches) > 1:
            raise SkillIngestError(
                f"'{server}' serves more than one skill named '{skill}'; pass its skill URI."
            )
        entry = parse_entry(matches[0])
        if entry is None:
            raise SkillIngestError(
                f"'{skill}' is built per request, so Daimon cannot check its files."
            )
        files: dict[str, bytes] = {}
        for file in entry.files:
            content = await fetch_resource(http, endpoint, file.uri, size=file.size)
            verify_file(file, content)
            files[file.path] = content
        await asyncio.to_thread(verify_frontmatter, entry, files["SKILL.md"])
    except SkillRejected as err:
        raise SkillRejected(f"'{skill}' from '{server}' failed a check: {err}.") from err
    except SkillIngestError:
        raise
    except (httpx.HTTPError, ValueError) as err:
        raise SkillIngestError(
            f"Could not read '{skill}' from '{server}' ({type(err).__name__}). Try again later."
        ) from err
    return ServerSkill(entry=entry, files=files)
