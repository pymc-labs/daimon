"""`daimon workflows`: the operator's commands over the same service the web app uses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from daimon.adapters.cli.commands import workflows as cli
from daimon.adapters.cli.commands.routines import run_translate
from daimon.core.config import Settings
from daimon.core.errors import UserFacingError
from daimon.core.stores.routines import claim_due_fireable, list_routines_for_tenant
from daimon.core.workflows import engine, service
from daimon.core.workflows.access import Who
from daimon.testing import ma_agent, tenant_metadata
from daimon.testing.builders import new_routine
from daimon.testing.workflows import NOW, World, make_settings, make_world
from mux.testing.drivers.anthropic.ma import MARouter
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness import build_cli_runtime

AGENT: dict[str, Any] = {
    "a": {"kind": "agent", "instructions": "Do it", "next": "f"},
    "f": {"kind": "finish", "output": "${{ steps.a.output }}"},
}


def _rig(
    world: World, settings: Settings | None = None, router: MARouter | None = None
) -> tuple[Any, StringIO, Console]:
    out = StringIO()
    rt = build_cli_runtime(world.sm, settings=settings or world.settings, router=router)
    return rt, out, Console(file=out, width=300)


async def test_every_command_refuses_while_workflows_are_off(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    off = make_settings().model_copy(update={"features": {"workflows": "off"}})
    rt, _, console = _rig(world, off)
    with pytest.raises(UserFacingError, match="Workflows are off here"):
        await cli.list_workflows(rt, console, world.tenant_id, as_json=False)
    with pytest.raises(UserFacingError, match="Workflows are off here"):
        await cli.from_routine(rt, console, world.tenant_id, [], apply=False)


async def test_a_file_imports_as_a_draft_lists_shows_and_exports_the_same_spec(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    world = await make_world(db_session, AGENT, "a")
    rt, out, console = _rig(world)
    source = tmp_path / "flow.yaml"
    spec = {"name": "Imported", "start": "f", "nodes": {"f": {"kind": "finish"}}}
    source.write_text(yaml.safe_dump(spec))

    made = await cli.import_workflow(rt, console, world.tenant_id, source, world.owner)
    assert "Imported 'Imported' as a draft" in out.getvalue(), out.getvalue()
    env = service.Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW)
    assert (await service.get(env, made)).owner.id == str(world.owner), "the --owner owns it"

    await cli.list_workflows(rt, console, world.tenant_id, as_json=True)
    assert "Imported" in out.getvalue() and str(world.workflow_id) in out.getvalue(), "both listed"
    await cli.show_workflow(rt, console, world.tenant_id, made, None)
    assert "Imported (draft). Live version: none." in out.getvalue(), out.getvalue()

    await cli.export_workflow(
        rt, console, world.tenant_id, made, version=None, fmt="yaml", out=tmp_path
    )
    exported = yaml.safe_load((tmp_path / "imported.yaml").read_text())
    assert exported == {"schema": "daimon.workflow/v1", **spec}, "the file comes back as given"


async def test_import_refuses_an_owner_who_is_not_on_the_tenant(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    world = await make_world(db_session, AGENT, "a")
    rt, _, console = _rig(world)
    source = tmp_path / "flow.yaml"
    source.write_text("name: X\nstart: f\nnodes: {f: {kind: finish}}\n")
    with pytest.raises(UserFacingError, match="not an active member"):
        await cli.import_workflow(rt, console, world.tenant_id, source, uuid.uuid4())


async def test_runs_are_listed_redriven_cancelled_and_verified(db_session: AsyncSession) -> None:
    world = await make_world(db_session, AGENT, "a")
    rt, out, console = _rig(world)
    failed = await world.run()
    await world.execute(await world.claim(failed.id, "a"), engine.Failed("agent", "no"))

    await cli.list_runs(rt, console, world.tenant_id, None, limit=10, as_json=True)
    assert '"status": "failed"' in out.getvalue(), out.getvalue()
    await cli.control_run(rt, console, world.tenant_id, failed.id, service.redrive)
    assert "Run 1: running." in out.getvalue(), out.getvalue()
    await cli.control_run(rt, console, world.tenant_id, failed.id, service.cancel)
    assert "Run 1: cancelled." in out.getvalue(), out.getvalue()
    with pytest.raises(UserFacingError, match="already ended"):
        await cli.control_run(rt, console, world.tenant_id, failed.id, service.cancel)
    assert await cli.verify_run(rt, console, world.tenant_id, failed.id), "the ledger holds"


async def test_the_board_and_the_calendar_print_one_workflows_runs(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a")
    rt, out, console = _rig(world)
    await world.run()
    await cli.list_runs(
        rt, console, world.tenant_id, world.workflow_id, limit=10, as_json=True, view="board"
    )
    assert '"stage": "In progress"' in out.getvalue(), out.getvalue()
    with pytest.raises(UserFacingError, match="--workflow"):
        await cli.list_runs(
            rt, console, world.tenant_id, None, limit=10, as_json=True, view="board"
        )
    with pytest.raises(UserFacingError, match="YYYY-MM"):
        await cli.list_runs(
            rt,
            console,
            world.tenant_id,
            world.workflow_id,
            limit=10,
            as_json=True,
            view="calendar",
            month="soon",
        )


async def test_from_routine_previews_without_moving_then_moves_and_moves_back(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a")
    routine = await new_routine(
        db_session,
        world.tenant_id,
        created_by_user_id="owner-1",
        cron_expr="0 * * * *",
        channel_id="c1",
    )
    await db_session.commit()
    router = MARouter()
    router.add_agent_list(
        ma_agent(name="daimon", metadata=tenant_metadata(world.tenant_id, "daimon"))
    )
    rt, out, console = _rig(world, router=router)

    assert await cli.from_routine(rt, console, world.tenant_id, [], apply=False) == []
    assert f"Routine {routine.id} would move" in out.getvalue(), out.getvalue()
    assert "kind: agent" in out.getvalue() and "at: home" in out.getvalue(), "its agent step"
    with pytest.raises(UserFacingError, match="--routine"):
        await cli.from_routine(rt, console, world.tenant_id, [], apply=True)
    [made] = await cli.from_routine(rt, console, world.tenant_id, [routine.id], apply=True)
    await cli.from_routine(rt, console, world.tenant_id, [routine.id], apply=False)
    assert f"Routine {routine.id} stays:" in out.getvalue(), "a moved routine is not offered again"
    assert await cli.to_routine(rt, console, world.tenant_id, made) == routine.id


async def test_routines_translate_previews_each_routine_then_moves_them(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, AGENT, "a")
    ok = await new_routine(db_session, world.tenant_id, created_by_user_id="o", channel_id="c1")
    blank = await new_routine(db_session, world.tenant_id, trigger_message=" ", enabled=False)
    now = datetime.now(UTC)
    running = await new_routine(db_session, world.tenant_id, next_fire_at=now)
    await claim_due_fireable(db_session, now=now, exclude_ids=frozenset({ok.id, blank.id}))
    await db_session.commit()
    rt, out, console = _rig(world)
    tenants = [world.tenant_id]
    assert await run_translate(rt=rt, console=console, tenant_ids=tenants, dry_run=True) == 1
    assert f"{world.tenant_id} {ok.id}: exact" in out.getvalue(), out.getvalue()
    assert f"{blank.id}: paused" in out.getvalue(), "an empty message moves too"
    assert f"{running.id}: stays: This routine is running" in out.getvalue()
    assert "2 would move, 1 stay." in out.getvalue()
    async with world.sm() as s:
        assert len(await list_routines_for_tenant(s, tenant_id=world.tenant_id)) == 3, "dry"
    assert await run_translate(rt=rt, console=console, tenant_ids=tenants, dry_run=False) == 1
    assert "2 moved, 1 stay." in out.getvalue()
    async with world.sm() as s:
        [left] = await list_routines_for_tenant(s, tenant_id=world.tenant_id)
    assert left.id == running.id
