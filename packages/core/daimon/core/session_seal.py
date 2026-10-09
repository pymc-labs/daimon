"""The seal a session's transcript stays under, as its metadata records it.

A channel session is stamped with the channel and thread it runs in and, once
any of its turns ran sealed, `daimon_sealed`: the sealed ids that sealed it,
comma-separated. The transcript tools show such a session only to a turn inside
every one of those ids, whatever the current policy says, so unsealing a
channel never releases what was written under the seal.

The set only grows. A reused session gains the seal of each sealed turn it
serves, and a successor inherits its predecessor's -- the transcript, workspace
or checkpoint it carries was written under that seal. When the predecessor's
seal can't be read, the successor is sealed to its own thread.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import anthropic as anthropic_pkg
import structlog
from anthropic import AsyncAnthropic
from daimon.core.authz import SessionFacts
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_SEALED,
    MA_METADATA_KEY_THREAD,
)
from daimon.core.session_ports_compat import retrieve_session_record
from mux.contracts.ids import Scope

__all__ = [
    "format_seal_ids",
    "inherited_seal_ids",
    "is_private_conversation",
    "origin_stamp",
    "seal_ids",
    "session_facts",
]

log = structlog.get_logger(__name__)

_SEPARATOR = ","
# #337 stamped a bare "true" for a session sealed at creation without saying
# which id sealed it; it is read as the strictest: the stamped channel and
# thread both.
_LEGACY_SEALED = "true"
# Stands in for a seal nobody can be inside: a legacy "true" with no channel.
_UNMATCHABLE = "\x00unknown-seal"


def seal_ids(metadata: Mapping[str, str | None] | None) -> frozenset[str]:
    """The sealed ids a session's transcript stays under; empty when none."""
    metadata = metadata or {}
    raw = metadata.get(MA_METADATA_KEY_SEALED)
    if not raw:
        return frozenset()
    ids: set[str] = set()
    for part in raw.split(_SEPARATOR):
        if not part:
            continue
        if part == _LEGACY_SEALED:
            ids.add(metadata.get(MA_METADATA_KEY_CHANNEL) or _UNMATCHABLE)
            thread = metadata.get(MA_METADATA_KEY_THREAD)
            if thread:
                ids.add(thread)
        else:
            ids.add(part)
    return frozenset(ids) if ids else frozenset({_UNMATCHABLE})


def is_private_conversation(conversation_id: str) -> bool:
    """A DM scope, a Slack IM (``D…``) or a Teams personal chat (``a:…``).

    Sessions that ran there are private even without the private-DM stamp
    (sessions created before every DM turn was stamped).
    """
    # Discord ids are numeric and Teams channels start "19:", so a leading
    # "D" is only ever a Slack IM.
    return conversation_id.startswith(("dm:", "a:", "D"))


def session_facts(
    metadata: Mapping[str, str] | None, *, owned: bool, legacy_thread_id: str | None = None
) -> SessionFacts:
    """What a session's metadata says about where it ran, for `authorize(READ_SESSION)`."""
    metadata = metadata or {}
    channel = metadata.get(MA_METADATA_KEY_CHANNEL)
    private = (
        MA_METADATA_KEY_PRIVATE_DM in metadata
        or is_private_conversation(str(metadata.get(MA_METADATA_KEY_CHANNEL) or ""))
        or is_private_conversation(str(metadata.get(MA_METADATA_KEY_THREAD) or ""))
        or (legacy_thread_id is not None and is_private_conversation(legacy_thread_id))
    )
    return SessionFacts(
        channel=channel,
        thread=metadata.get(MA_METADATA_KEY_THREAD) if channel is not None else None,
        seal_ids=seal_ids(metadata),
        legacy_thread_id=legacy_thread_id,
        owned=owned,
        private=private,
    )


def format_seal_ids(ids: Collection[str]) -> str:
    return _SEPARATOR.join(sorted(ids))


def origin_stamp(
    *, channel_id: str, thread_id: str | None, seal: Collection[str] = ()
) -> dict[str, str]:
    """Where a channel conversation runs and the seal it stays under."""
    stamp = {MA_METADATA_KEY_CHANNEL: channel_id}
    if thread_id is not None:
        stamp[MA_METADATA_KEY_THREAD] = thread_id
    if seal:
        stamp[MA_METADATA_KEY_SEALED] = format_seal_ids(seal)
    return stamp


async def inherited_seal_ids(
    anthropic: AsyncAnthropic,
    *,
    predecessor_session_id: str,
    own_thread_id: str,
    tenant_seals_anything: bool,
    scope: Scope | None = None,
) -> frozenset[str]:
    """The seal a successor of `predecessor_session_id` must carry.

    The predecessor's recorded seal when it has one. A predecessor stamped
    with its channel but no seal never ran sealed. One without a channel stamp
    predates the stamp: it is treated the way the transcript tools treat it,
    sealed to this thread while the tenant seals anything. A predecessor that
    can't be read at all seals the successor to its own thread.
    """
    scope = scope or Scope.legacy_host_authorized(call_site="session_seal:inherited_seal_ids")
    try:
        predecessor = await retrieve_session_record(anthropic, predecessor_session_id, scope=scope)
    except anthropic_pkg.APIError as error:
        log.warning(
            "session_seal.predecessor_unreadable",
            predecessor_session_id=predecessor_session_id,
            error=type(error).__name__,
        )
        return frozenset({own_thread_id})
    metadata = predecessor.metadata or {}
    if MA_METADATA_KEY_CHANNEL in metadata:
        return seal_ids(metadata)
    return frozenset({own_thread_id}) if tenant_seals_anything else frozenset()
