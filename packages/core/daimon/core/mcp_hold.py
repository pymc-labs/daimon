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
from collections.abc import Mapping
from typing import Final, cast

from daimon.core.turn_hold import TurnHold

#: Signed JWT claim that carries a held turn's hold: `{platform, channel_id, client}`.
HOLD_CLAIM: Final = "daimon_hold"

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
