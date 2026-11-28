"""Move an agent's name inside workflow specs when the agent is renamed.

Steps name agents by key (`slug` of the name) in spec JSON: a draft, every published
version (with its `agent_tools` keys), a pending publish request and a live poll or
mention trigger. A routine moved to a workflow follows the same rename as the routine
did. Rewriting a published node changes its hash, so grants pinned to it follow.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any
from typing import cast as typed

from daimon.core._models import Base
from daimon.core.workflows.spec import node_hash, parse_spec, slug, spec_hash
from pydantic import ValidationError
from sqlalchemy import ColumnElement, Row, Table, Text, Update, cast, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

_KEYS = ("agent", "as_agent")


def _objects(*candidates: object) -> Iterator[dict[str, Any]]:
    for candidate in candidates:
        if isinstance(candidate, dict):
            yield typed("dict[str, Any]", candidate)


def _holders(spec: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The objects of a spec that name an agent: defaults, triggers, steps, a For each's
    inner step and a Wait's poll."""
    yield from _objects(spec.get("defaults"), *typed("list[object]", spec.get("triggers") or []))
    for node in _objects(*typed("dict[str, object]", spec.get("nodes") or {}).values()):
        yield from _objects(node, node.get("do"), node.get("poll"))


def _renamed_keys(holder: dict[str, Any], *, old: str, new: str) -> bool:
    changed = False
    for key in _KEYS:
        value = holder.get(key)
        if isinstance(value, str) and slug(value) == slug(old):
            holder[key] = slug(new)
            changed = True
    return changed


def renamed_spec(spec: Mapping[str, Any], *, old: str, new: str) -> dict[str, Any] | None:
    """`spec` with agent `old` named `new` throughout; None when it names no such agent."""
    copy = deepcopy(dict(spec))
    changed = [_renamed_keys(h, old=old, new=new) for h in _holders(copy)]
    return copy if any(changed) else None


def _mentions(
    table: Table, column: ColumnElement[Any], tenant_id: uuid.UUID, old: str
) -> list[ColumnElement[bool]]:
    """Rows of the tenant whose JSON text could name `old` (`my-agent` also as `my agent`)."""
    pattern = "%" + "%".join(slug(old).split("-")) + "%"
    return [table.c.tenant_id == tenant_id, cast(column, Text).ilike(pattern)]


def _node_hashes(spec: Mapping[str, Any]) -> dict[str, str]:
    try:
        return {k: node_hash(n) for k, n in parse_spec(spec).nodes.items()}  # pyright: ignore[reportArgumentType]
    except ValidationError:  # a spec this release cannot read keeps its old hashes
        return {}


async def _plan(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> tuple[list[Update], list[Update]]:
    """The updates that move a row's spec, and those that re-pin the grants of a moved node."""
    t = Base.metadata.tables
    moves: list[Update] = []
    repins: list[Update] = []

    async def rows(table: Table, column: str, *extra: ColumnElement[bool]) -> Sequence[Row[Any]]:
        where = _mentions(table, table.c[column], tenant_id, old)
        return (await session.execute(select(table).where(*where, *extra))).all()

    drafts = t["workflows"]
    for row in await rows(drafts, "draft_spec"):
        if (spec := renamed_spec(row.draft_spec, old=old, new=new)) is not None:
            moves.append(update(drafts).where(drafts.c.id == row.id).values(draft_spec=spec))

    requests = t["workflow_publish_requests"]
    for row in await rows(requests, "spec", requests.c.status == "pending"):
        if (spec := renamed_spec(row.spec, old=old, new=new)) is not None:
            moves.append(update(requests).where(requests.c.id == row.id).values(spec=spec))

    triggers = t["workflow_triggers"]
    for row in await rows(triggers, "config"):
        live = row.config.get("spec")
        if isinstance(live, dict) and (
            spec := renamed_spec(typed("dict[str, Any]", live), old=old, new=new)
        ):
            config = {**row.config, "spec": spec}
            moves.append(update(triggers).where(triggers.c.id == row.id).values(config=config))

    versions, grants = t["workflow_versions"], t["workflow_grants"]
    for row in await rows(versions, "spec"):
        if (spec := renamed_spec(row.spec, old=old, new=new)) is None:
            continue
        values: dict[str, Any] = {"spec": spec}
        if row.agent_tools is not None:
            values["agent_tools"] = {
                (slug(new) if slug(k) == slug(old) else k): v for k, v in row.agent_tools.items()
            }
        before, after = _node_hashes(row.spec), _node_hashes(spec)
        if after:
            values["spec_hash"] = spec_hash(parse_spec(spec))  # pyright: ignore[reportArgumentType]
        key = (versions.c.workflow_id == row.workflow_id, versions.c.number == row.number)
        moves.append(update(versions).where(*key).values(**values))
        repins += [
            update(grants)
            .where(
                grants.c.workflow_id == row.workflow_id,
                grants.c.version == row.number,
                grants.c.node_id == node_id,
                grants.c.pins["node_hash"].astext == was,
            )
            .values(pins=grants.c.pins.op("||")(literal({"node_hash": after[node_id]}, JSONB)))
            for node_id, was in before.items()
            if after.get(node_id, was) != was
        ]
    return moves, repins


async def rename_workflow_agents(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> int:
    """Point the tenant's workflow specs at `new` instead of `old`; the rows that moved."""
    moves, repins = await _plan(session, tenant_id=tenant_id, old=old, new=new)
    for statement in (*moves, *repins):
        await session.execute(statement)
    return len(moves)


async def count_workflow_agents(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> int:
    """How many rows `rename_workflow_agents` would move; read-only."""
    return len((await _plan(session, tenant_id=tenant_id, old=old, new=new))[0])
