"""Files on Slack messages, surfaced through the signed file proxy.

A Slack ``url_private`` needs the workspace bot token, so handing it to the
agent gives it nothing it can fetch. The Slack adapter solves this for the
turn context by minting a signed, expiring token and pointing the agent at
the MCP ``/slack/file/{token}`` route, which re-authenticates and streams
the bytes. The read tools mint the same URLs here, with the same signer
(``daimon.core.slack_file_token``) and the same secret the route verifies
with, so a file the agent sees in ``read_thread`` is reachable exactly the
way a file attached to the mention is.

When the deployment has no public URL or no signing secret, the route is not
mounted and no URL can be minted. Files are still listed, with ``url`` unset,
so the agent knows a file exists rather than seeing a message with only text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._models import SlackFileRow
from daimon.core.slack_file_token import mint_file_token

# Matches the Slack adapter's proxy-URL lifetime for files in the turn context.
_FILE_URL_TTL_S = 24 * 3600

# Slack keeps a placeholder entry for a deleted file or one hidden by the
# workspace's retention limit; neither has bytes behind it.
_UNREADABLE_MODES = frozenset({"tombstone", "hidden_by_limit"})


@dataclass(frozen=True)
class FileUrlMinter:
    base_url: str
    secret: str
    team_id: str
    now: int

    def url(self, file_id: str) -> str:
        token = mint_file_token(
            team_id=self.team_id,
            file_id=file_id,
            exp=self.now + _FILE_URL_TTL_S,
            secret=self.secret,
        )
        return f"{self.base_url.rstrip('/')}/slack/file/{token}"


def file_url_minter(runtime: McpRuntime, *, team_id: str, now: int) -> FileUrlMinter | None:
    """The minter for this deployment, or None when the proxy route is not mounted."""
    base_url = runtime.settings.mcp.app_root_url
    secret = runtime.settings.mcp.jwt_secret
    if base_url is None or secret is None:
        return None
    return FileUrlMinter(
        base_url=base_url, secret=secret.get_secret_value(), team_id=team_id, now=now
    )


def to_file_rows(
    raw_files: list[dict[str, Any]], minter: FileUrlMinter | None
) -> list[SlackFileRow]:
    rows: list[SlackFileRow] = []
    for f in raw_files:
        file_id = f.get("id")
        if not file_id or f.get("mode") in _UNREADABLE_MODES:
            continue
        size = f.get("size")
        rows.append(
            SlackFileRow(
                id=str(file_id),
                name=str(f.get("name") or f.get("title") or "file"),
                mimetype=str(f.get("mimetype") or "unknown"),
                size=int(size) if size is not None else None,
                url=minter.url(str(file_id)) if minter is not None else None,
            )
        )
    return rows
