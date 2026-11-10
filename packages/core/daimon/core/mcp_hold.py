"""How a turn's hold travels on its own Daimon MCP credential.

A held turn (`daimon.core.turn_hold.TurnHold`) mounts a chat credential whose
signed claim names the channel it is held to. The MCP server trusts the claim
only when it matches the registry row, so the three helpers here serve both
sides: the minter writes the claim, the vault and the session snapshot name the
credential by its key, and the verifier reads the claim back. Pure: no I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Final, cast

from daimon.core.turn_hold import TurnHold

#: Signed JWT claim that carries a held turn's hold: `{platform, channel_id, client}`.
HOLD_CLAIM: Final = "daimon_hold"

#: The search transform's two proxies. They carry no data of their own: a
#: proxied call re-enters the identity middleware under the inner tool's name,
#: where `held_tool_refusal` judges it.
HELD_PROXY_TOOLS: Final = frozenset({"search_tools", "call_tool"})

HELD_TOOL_ALLOWLIST: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Channel tools: they authorize against the channel policy, which applies
        # the hold, so a held turn reads and posts only inside its channel.
        "list_channels": "channel listing, filtered by the channel policy",
        "read_channel": "reads channel messages through the channel policy",
        "read_thread": "reads a thread through the channel policy",
        "get_message": "reads one message through the channel policy",
        "list_threads": "lists a channel's threads through the channel policy",
        "search_messages": "searches messages through the channel policy",
        "parse_link": "pure link parsing; reads nothing",
        "send_message": "posts to a channel through the channel policy",
        "create_thread": "opens a thread under a channel through the channel policy",
        "rename_thread": "renames a thread the channel policy lets the caller post in",
        "edit_message": "edits only a message the agent itself posted",
        "approve_reach": "asks the requester on a card; posts nothing itself",
        "set_never_tag": "sets only the caller's own never-tag choice; touches nothing else",
        "delete_message": "deletes only a message the agent itself posted",
        "delete_reply": "deletes only messages the agent itself posted, one at a time",
        "archive_thread": "archives only a thread the agent opened",
        "delete_thread": "removes only the agent's own messages from a thread it opened",
        # The conversation itself.
        "where_am_i": "describes the current place; applies the hold to what it lists",
        # Timers resume the same thread, as the person who asked.
        "create_timer": "resumes this same thread once, as the requester",
        "list_timers": "lists the caller's own timers",
        "cancel_timer": "cancels one of the caller's own timers",
        # The caller's own sessions and transcripts, read through the channel policy.
        "list_sessions": "lists the caller's own conversations",
        "get_session": "reads one of the caller's own conversations",
        "list_session_events": "reads the caller's own transcript through the read policy",
        # Publishing the conversation's own sandbox output waits for the requester's
        # approval card. These two only mint an upload URL for it: they create no
        # agent and no token. `publish_report` is not here: it publishes an agent's
        # reader, which copies the agent's prompt and skills and mints an agent token.
        "create_notebook_upload_url": "scratch notebook upload, only after the requester approves",
        "create_attachment_upload_url": "attachment upload, only after the requester approves",
        # Pure utilities.
        "now": "wall-clock time; reads nothing",
        "convert": "time-zone conversion; reads nothing",
    }
)


def held_catalogue(tools: Collection[str]) -> frozenset[str]:
    """The part of a captured Daimon MCP catalogue a held credential lists."""
    return frozenset(tools) & {*HELD_TOOL_ALLOWLIST, *HELD_PROXY_TOOLS}


_CLAIM_KEYS: Final = frozenset({"platform", "channel_id", "client"})


def hold_claim(hold: TurnHold) -> dict[str, str | bool]:
    """The value signed under `HOLD_CLAIM`."""
    return {"platform": hold.platform, "channel_id": hold.channel_id, "client": hold.client}


def parse_hold_claim(raw: object) -> TurnHold:
    """The hold a claim value names. Raises `ValueError` for anything but the exact shape."""
    if not isinstance(raw, Mapping):
        raise ValueError("hold claim is not an object")
    claim = cast("Mapping[str, object]", raw)
    platform, channel_id, client = (claim.get(key) for key in ("platform", "channel_id", "client"))
    if (
        frozenset(claim) != _CLAIM_KEYS
        or not isinstance(platform, str)
        or not isinstance(channel_id, str)
        or not isinstance(client, bool)
        or not platform
        or not channel_id
    ):
        raise ValueError("hold claim has the wrong shape")
    return TurnHold(platform=platform, channel_id=channel_id, client=client)


def hold_metadata_value(hold: TurnHold) -> str:
    """The hold as credential metadata, which holds strings only."""
    return json.dumps(hold_claim(hold), sort_keys=True, separators=(",", ":"))


def hold_from_metadata(value: str | None) -> TurnHold | None:
    """The hold recorded in credential metadata, or None for an unheld credential.

    Raises `ValueError` for a recorded hold that cannot be read: a caller must
    never read that as "unheld" and mint an unrestricted credential in its place.
    """
    if value is None:
        return None
    return parse_hold_claim(json.loads(value))


def hold_key(hold: TurnHold | None) -> str | None:
    """A stable name for one hold, recorded in credential metadata and the session snapshot."""
    if hold is None:
        return None
    return f"{hold.platform}:{hold.channel_id}:{'client' if hold.client else 'member'}"


def hold_vault_suffix(hold: TurnHold) -> str:
    """The part of a held vault's display name that tells holds apart.

    The channel id is hashed so a long Teams conversation id never makes the
    name unbounded; the name only has to be unique per hold, and the credential
    metadata keeps the exact hold.
    """
    digest = hashlib.sha256(hold.channel_id.encode()).hexdigest()[:20]
    return f":hold:{hold.platform}:{digest}:{'client' if hold.client else 'member'}"
