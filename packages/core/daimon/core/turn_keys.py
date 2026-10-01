"""The names-only projection of an agent's stored keys, and its XML renderer.

This module defines the contract for naming an agent's stored keys on a turn.
The intended caller is each adapter, right after `bind_session`, reading the
live `thread_sessions` row it just bound: it passes that row's
`effective_config.env_sha256` to `list_mounted_key_names`, which names the
stored keys only while the mounted `.env` is still assembled from exactly
those rows. A session bound before the latest key change runs an older `.env`,
and its hash no longer matches; naming today's keys then would tell the model
a key is available that the answering session cannot read, so nothing is named.

Stored key names are configuration metadata, never evidence that the
answering session has those keys mounted. `list_turn_key_names` is not the
tool that describes an arbitrary agent's stored keys to a person (that is
the `list_agent_keys` chat tool, which resolves a target agent by name);
this function takes only the answering session's own agent id, as a
`uuid.UUID`, and resolves nothing.

XML vocabulary (fixed here for a later phase to fill, not redefine): when
there are names to render, `render_keys_element` emits a `<keys>` element
containing one self-closing `<key name="..."/>` child per name, in the order
given:

    <keys><key name="TOGGL_TOKEN"/><key name="OPENAI_API_KEY"/></keys>

A child element per name (not a delimited attribute) because a key name may
legally contain punctuation that would need escaping twice under a delimiter
scheme. An empty sequence renders as `""` — an agent with no keys contributes
no element at all, matching the rest of the per-turn context vocabulary
where an absent fact is an absent element.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from xml.sax.saxutils import quoteattr

from daimon.core.credential_env import assemble_env_bytes
from daimon.core.env_file import env_row_skip_reason
from daimon.core.session_snapshot import hash_env_bytes
from daimon.core.stores.agent_files import list_agent_files
from sqlalchemy.ext.asyncio import AsyncSession


async def list_turn_key_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> tuple[str, ...]:
    """Return the stored key names for this (tenant, agent), names only.

    `agent_id` is the answering session's own agent — never an arbitrary
    target resolved from a name. The store already returns rows ordered by
    key, so the result is sorted for free; this function adds no sort of its
    own and no deduplication (the store's primary key already guarantees
    uniqueness).

    The return type is `tuple[str, ...]`: no row, no mapping, no field
    through which a stored value is reachable. That is what makes the
    values-never-ride property a type guarantee rather than a convention.
    """
    rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    return tuple(row.key for row in rows)


async def list_mounted_key_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    env_sha256: str | None,
) -> tuple[str, ...]:
    """Stored key names, but only when the mounted `.env` is these exact rows.

    `env_sha256` is what the answering session froze
    (`thread_sessions.effective_config.env_sha256`). When it differs from
    `hash_env_bytes(assemble_env_bytes(rows))` the session runs an older `.env`
    and naming today's keys would tell the model a key is available that it
    cannot read — returns `()`.

    A session that froze no hash mounted no `.env` at all, so it has nothing
    to name either. Rows `assemble_env_bytes` leaves out are never named.
    """
    if env_sha256 is None:
        return ()
    rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    if not rows or hash_env_bytes(assemble_env_bytes(rows)) != env_sha256:
        return ()
    # A row the assembler leaves out (hard-denied name, NUL value) is not in the
    # mounted file; naming it would promise the model a key it cannot read.
    return tuple(row.key for row in rows if env_row_skip_reason(row.key, row.content) is None)


def render_keys_element(names: Sequence[str]) -> str:
    """Render the `<keys>` element for a turn, or `""` if there is nothing to say.

    Pure and total: same input, same output, no I/O. Each name is rendered
    inside a `<key name="..."/>` child with its attribute value XML-escaped,
    so a name containing an XML metacharacter cannot close the element or
    forge a sibling attribute.
    """
    if not names:
        return ""
    children = "".join(f"<key name={quoteattr(name)}/>" for name in names)
    return f"<keys>{children}</keys>"
