"""The short-lived credential a chat adapter sends the MCP service for one reaction action.

The chat process signs it with `mcp.jwt_secret`, which it already holds, so it never
holds the attached server's own key. It names one reactor, one message, one server
and one tool, and lives one minute. It has no `sub` and no registry row, so no other
verifier of that secret accepts it, and this verifier accepts no other token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast

import jwt as pyjwt

ISSUER: Final = "daimon-discord"
AUDIENCE: Final = "daimon-reaction-action"
PURPOSE: Final = "reaction_action"
TTL: Final = timedelta(seconds=60)
_FIELDS: Final = ("ws", "server", "tool", "agent", "user", "guild", "channel", "message")


class CredentialError(Exception):
    """The credential is missing, forged, expired or of the wrong shape."""


@dataclass(frozen=True, slots=True)
class ReactionCall:
    """What one credential allows. `parent` is the channel of a thread, else None."""

    workspace: str
    server: str
    tool: str
    agent: str
    user: str
    guild: str
    channel: str
    message: str
    parent: str | None = None


def mint(call: ReactionCall, *, secret: bytes, now: datetime) -> str:
    claims: dict[str, str | int] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "purpose": PURPOSE,
        "iat": int(now.timestamp()),
        "exp": int((now + TTL).timestamp()),
        "ws": call.workspace,
        "server": call.server,
        "tool": call.tool,
        "agent": call.agent,
        "user": call.user,
        "guild": call.guild,
        "channel": call.channel,
        "message": call.message,
    }
    if call.parent is not None:
        claims["parent"] = call.parent
    return pyjwt.encode(claims, secret, algorithm="HS256")


def verify(token: str, *, secret: bytes, now: datetime) -> ReactionCall:
    """The call a valid credential allows; `CredentialError` for anything else."""
    try:
        claims = cast(
            "dict[str, object]",
            pyjwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["exp", "iat", "aud", "iss"],
                },
            ),
        )
    except pyjwt.InvalidTokenError:
        raise CredentialError("rejected") from None
    expiry, issued = claims.get("exp"), claims.get("iat")
    if (
        claims.get("purpose") != PURPOSE
        or "sub" in claims
        or type(expiry) is not int
        or type(issued) is not int
        or not issued <= now.timestamp() < expiry <= issued + int(TTL.total_seconds())
        or not all(isinstance(claims.get(name), str) and claims[name] for name in _FIELDS)
    ):
        raise CredentialError("rejected")
    parent = claims.get("parent")
    if parent is not None and not isinstance(parent, str):
        raise CredentialError("rejected")
    text = cast("dict[str, str]", claims)
    return ReactionCall(
        workspace=text["ws"],
        server=text["server"],
        tool=text["tool"],
        agent=text["agent"],
        user=text["user"],
        guild=text["guild"],
        channel=text["channel"],
        message=text["message"],
        parent=parent,
    )
