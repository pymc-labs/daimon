"""What one read may see and how its answer goes out: the one rule every reading surface uses.

The caller resolves the audience (`daimon.core.data_audience`) from signed facts. `open_read`
turns it into the store's `ReadContext` and the read-time redaction: every string goes
through the tenant's answer-time rules, and, when a guest or a person marked redacted will read
the answer, its entity lists. What redaction cannot cover is refused (`Reading.redacted`):
caller SQL, file bytes, and a query that names a hidden term, whose hit or miss would tell
what the rules hide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.data_audience import DataAudience, scope_ceiling
from daimon.data import access, identity, redaction
from daimon.data.access import ReadContext, person
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow


@dataclass(frozen=True)
class Reading:
    ctx: ReadContext
    entities: redaction.Entities | None
    """The names hidden from this audience; None when none are."""
    answer: redaction.Redactor | None = None
    """The tenant's answer-time rules, for every audience; None when it has none."""

    @property
    def redacted(self) -> bool:
        """Whether this audience reads anything other than what is stored."""
        return self.entities is not None or self.answer is not None

    def show[T](self, value: T) -> T:
        """`value` (JSON-like) as this audience may read it."""
        shown = value if self.answer is None else self.answer(value)
        return shown if self.entities is None else self.entities(shown)

    def names_hidden(self, text: str) -> bool:
        """Whether `text` names a term this audience may not see."""
        return self.show(text) != text


async def open_read(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    audience: DataAudience,
    *,
    salt: str,
) -> Reading:
    """What `audience` may read together in `tenant_id`, and its redaction."""
    people = list(audience.people)
    holders = await identity.holders_of(conn, schemas, tenant_id, people)
    scopes_of = {
        account: frozenset(await access.effective_scopes(conn, schemas, tenant_id, held))
        for account, held in holders.items()
    }
    readers = [*map(person, people), *audience.guests] if audience.list_rows else []
    ctx = ReadContext(
        tenant_ids=(tenant_id,),
        scopes=tuple(sorted(scope_ceiling(audience, scopes_of))),
        readers=tuple(sorted(readers)),
        personal_ok=len(people) + len(audience.guests) <= 1,
        salt=salt,
    )
    hidden = audience.redact or bool(
        await redaction.redacted_among(conn, schemas, tenant_id, people)
    )
    entities = (
        await redaction.read_redactor(conn, schemas, tenant_id, ctx.scopes) if hidden else None
    )
    return Reading(ctx, entities, await redaction.answer_redactor(conn, schemas, tenant_id))
