"""Chat accounts: which workspaces a proven chat account reaches, and the claim form an
OpenID Connect provider vouches for them in. Pure: the caller reads the rows."""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.microsoft_identity import canonical_uuid
from daimon.core.stores.domain import ChatPlatform, TenantRow

#: ID token claim an OpenID Connect provider vouches for chat accounts in.
CHAT_ACCOUNTS_CLAIM: Final = "chat_accounts"
#: Platforms whose user ids are the same person in every workspace. A Slack or Teams
#: id is only ever matched in the workspace the sign-in proves.
GLOBAL_USER_IDS: Final[frozenset[ChatPlatform]] = frozenset({"discord"})
_DISCORD_ID = re.compile(r"[0-9]{15,21}")
_SLACK_ID = re.compile(r"[A-Z0-9]{2,32}")
_FALLBACK_NAMES: Final[dict[ChatPlatform, str]] = {
    "discord": "Discord server",
    "slack": "Slack workspace",
    "teams": "Microsoft Teams",
}


@dataclass(frozen=True, slots=True)
class ChatAccount:
    """A chat platform user id, and the workspace its proof names (None: names none)."""

    platform: ChatPlatform
    user_id: str
    workspace_id: str | None


def parse_chat_account(value: str) -> ChatAccount | None:
    """`discord:<user id>`, `slack:<team id>:<user id>` or `teams:<tenant id>:<object id>`;
    None for anything else."""
    parts = value.split(":")
    match parts:
        case ["discord", user_id] if _DISCORD_ID.fullmatch(user_id):
            return ChatAccount("discord", user_id, None)
        case ["slack", team_id, user_id] if _SLACK_ID.fullmatch(team_id) and _SLACK_ID.fullmatch(
            user_id
        ):
            return ChatAccount("slack", user_id, team_id)
        case ["teams", tenant_id, object_id]:
            tid, oid = canonical_uuid(tenant_id), canonical_uuid(object_id)
            return None if tid is None or oid is None else ChatAccount("teams", oid, tid)
        case _:
            return None


def parse_chat_accounts(values: Iterable[str]) -> tuple[ChatAccount, ...]:
    """The well-formed accounts among `values`, once each, in order."""
    accounts: list[ChatAccount] = []
    for value in values:
        account = parse_chat_account(value)
        if account is not None and account not in accounts:
            accounts.append(account)
    return tuple(accounts)


@dataclass(frozen=True, slots=True)
class ReachedWorkspace:
    tenant: TenantRow
    name: str


def reached_workspaces(
    tenants: Sequence[TenantRow],
    *,
    platform: ChatPlatform,
    proven: Mapping[str, str],
    member_of: Collection[uuid.UUID],
    synced: Mapping[uuid.UUID, str],
) -> list[ReachedWorkspace]:
    """The ready workspaces a chat account reaches, by name.

    A workspace whose member list was loaded in full answers from that list alone, so
    a person who left loses it at once. One never loaded falls back to what the sign-in
    itself proves (`proven`: workspace id to name), so a fresh install is not locked out.
    """
    reached: list[ReachedWorkspace] = []
    for tenant in tenants:
        if tenant.platform != platform or tenant.archived_at is not None:
            continue
        if tenant.provision_status != "ready":
            continue
        if platform not in GLOBAL_USER_IDS and tenant.external_id not in proven:
            continue
        if tenant.id in synced:
            if tenant.id not in member_of:
                continue
        elif tenant.external_id not in proven:
            continue
        name = proven.get(tenant.external_id) or synced.get(tenant.id) or _FALLBACK_NAMES[platform]
        reached.append(ReachedWorkspace(tenant, name))
    reached.sort(key=lambda w: (w.name.lower(), w.tenant.external_id))
    return reached
