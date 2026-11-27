"""What one read may see and how its answer goes out: the one rule every reading surface uses.

The caller resolves the audience (`daimon.core.data_audience`) from signed facts. `open_read`
turns it into the store's `ReadContext` and the read-time redaction: every string goes
through its source's answer-time rules, and, when a guest or a person marked redacted will read
the answer, its entity lists. What entity lists cannot cover is refused while they apply
(`Reading.redacted`): caller SQL, file bytes, and a query that names a hidden term, whose hit
or miss would tell what the rules hide. Answer-time rules reach SQL values and file text best
effort.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast

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
    answer: Mapping[uuid.UUID, redaction.Redactor] = field(
        default_factory=dict[uuid.UUID, redaction.Redactor]
    )
    """Per source, the rules its answers go through (`redaction.answer_redactors`)."""

    @property
    def redacted(self) -> bool:
        """Whether names are hidden from this audience."""
        return self.entities is not None

    def show[T](self, value: T, source_id: uuid.UUID | None = None, *, ids: bool = True) -> T:
        """`value` (JSON-like) as this audience may read it: the rules of `source_id`, or of
        the source a mapping's `source_id` names; every source's, outside one. `ids=False`
        trusts no mapping's `source_id`, as for caller SQL, whose rows can name any source."""
        shown = self._answer(value, source_id, ids)
        return shown if self.entities is None else self.entities(shown)

    def names_hidden(self, text: str) -> bool:
        """Whether `text` names a term this audience may not see."""
        return self.show(text) != text

    def _answer[T](self, value: T, source_id: uuid.UUID | None, ids: bool = True) -> T:
        if not self.answer:
            return value
        if source_id is not None:
            rules = self.answer.get(source_id)
            return value if rules is None else rules(value)
        out: object = value
        if isinstance(value, dict):
            items = cast("dict[object, object]", value)
            if ids and (source := _uuid(items.get("source_id"))) is not None:
                return self._answer(cast("T", items), source)
            out = {k: self._answer(v, None, ids) for k, v in items.items()}
        elif isinstance(value, list | tuple):
            out = [self._answer(v, None, ids) for v in cast("Iterable[object]", value)]
        elif isinstance(value, str):
            for rules in dict.fromkeys(self.answer.values()):
                out = rules(out)
        return cast("T", out)


def _uuid(value: object) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


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
    return Reading(ctx, entities, await redaction.answer_redactors(conn, schemas, tenant_id))
