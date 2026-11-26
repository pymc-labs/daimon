"""Sensitive values in data tool results: emails, sign-in codes and links, redaction rules.

In the asker's direct message or web chat a value shows as it is. Elsewhere it comes back as a
placeholder sealed to the asker; only `open_placeholders` reads it, for that asker, when
`send_private_details` puts it in their direct message. Without encryption keys it is withheld.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from cryptography.fernet import InvalidToken, MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._web_admin import private_details
from pydantic import JsonValue

_PLACEHOLDER = re.compile(r"\[\[private:([A-Za-z0-9_=-]+)\]\]")
_TTL_SECONDS = 3600
WITHHELD = "[withheld]"


@dataclass(frozen=True)
class Privately:
    """How one call's sensitive values come back: as they are, or sealed to the asker."""

    auth: AuthIdentity
    fernet: MultiFernet | None
    shown: bool

    def __call__(self, value: JsonValue) -> JsonValue:
        if value is None or self.shown:
            return value
        if self.fernet is None:
            return WITHHELD
        text = value if isinstance(value, str) else json.dumps(value)
        owner = [str(self.auth.tenant_id), str(self.auth.account_id), text]
        return f"[[private:{self.fernet.encrypt(json.dumps(owner).encode()).decode()}]]"


async def privately(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> Privately:
    shown = await private_details(runtime, auth, origin_context_id) == "shown"
    return Privately(auth, runtime.fernet, shown)


def open_placeholders(text: str, auth: AuthIdentity, fernet: MultiFernet | None) -> tuple[str, int]:
    """`text` with the asker's own live placeholders opened, and how many; any other
    (someone else's, expired, altered) reads as withheld."""
    opened = 0

    def one(match: re.Match[str]) -> str:
        nonlocal opened
        try:
            tenant, account, value = json.loads(
                fernet.decrypt(match[1].encode(), ttl=_TTL_SECONDS) if fernet else b"[]"
            )
        except (InvalidToken, ValueError):
            return WITHHELD
        if (tenant, account) != (str(auth.tenant_id), str(auth.account_id)):
            return WITHHELD
        opened += 1
        return str(value)

    return _PLACEHOLDER.sub(one, text), opened


__all__ = ["WITHHELD", "Privately", "open_placeholders", "privately"]
