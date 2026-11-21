"""Slack file surfacing via the signed MCP proxy.

Slack ``url_private`` is not publicly fetchable, so — unlike Discord's public
CDN URLs — the agent cannot curl it directly. Instead the adapter mints a
signed, expiring token and hands the agent a URL to the MCP ``/slack/file/{token}``
route, which re-authenticates and streams the bytes. These builders emit the
``[attachment] ...`` lines that carry those URLs into the user message; the same
proxy URL is used for inlined images (external-use handle), skipped images
(the only way to reach them), and data files.

The lines are adapter-authored metadata sitting in the user turn and say so
plainly (``[attachment] ...``). They must NOT impersonate a system message: text
reading ``*system: fetch this URL*`` inside a user turn is indistinguishable from
a prompt injection, and injection-aware models refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.slack.vision import SlackFile
from daimon.core.media.vision import resized_for_vision
from daimon.core.slack_file_token import mint_file_token


@dataclass(frozen=True)
class ProxyUrlContext:
    """The inputs a turn needs to mint signed proxy URLs for its files.

    These four always travel together — the deployment's public MCP URL and
    signing secret, the tenant's ``team_id``, and the current time — so they are
    bundled rather than threaded individually through every builder. ``None``
    (rather than a partial context) is the "proxy unconfigured" signal.
    """

    public_url: str
    secret: str
    team_id: str
    now: int


def build_proxy_url(file: SlackFile, ctx: ProxyUrlContext, *, ttl_s: int = 24 * 3600) -> str:
    """Mint a signed proxy URL to the MCP ``/slack/file/{token}`` route."""
    token = mint_file_token(
        team_id=ctx.team_id, file_id=file["id"], exp=ctx.now + ttl_s, secret=ctx.secret
    )
    return f"{ctx.public_url.rstrip('/')}/slack/file/{token}"


def build_image_url_prefix(files: list[SlackFile], ctx: ProxyUrlContext) -> str:
    """One line per inlined image exposing a fetchable handle for external use."""
    return "\n".join(
        f"[attachment] image `{f['name']}` ({f['size']} bytes), uploaded by the user with "
        f"this message and forwarded to you inline as a vision block"
        + (
            ", resized to fit the model's image limits (the handle below serves the original)"
            if resized_for_vision(f["size"], f.get("original_w"), f.get("original_h"))
            else ""
        )
        + f". Fetchable handle (curl or pass to an external API; expires ~24h): "
        f"{build_proxy_url(f, ctx)}"
        for f in files
    )


def build_skipped_image_prefix(skipped: list[tuple[SlackFile, str]], ctx: ProxyUrlContext) -> str:
    """One line per image NOT inlined, with the proxy URL so it stays reachable."""
    return "\n".join(
        f"[attachment] image `{f['name']}`, uploaded by the user with this message, was NOT "
        f"inlined as a vision block ({reason}); fetch it yourself — curl to disk then use "
        f"your `read` tool to view it, or pass to an external API (expires ~24h): "
        f"{build_proxy_url(f, ctx)}"
        for f, reason in skipped
    )


def build_attachment_url_prefix(files: list[SlackFile], ctx: ProxyUrlContext) -> str:
    """One line per non-image data file exposing its proxy URL."""
    return "\n".join(
        f"[attachment] `{f['name']}` ({f['size']} bytes), uploaded by the user with this "
        f"message. Fetchable handle, expires ~24h: {build_proxy_url(f, ctx)} — curl it to "
        f"disk, then read it."
        for f in files
    )
