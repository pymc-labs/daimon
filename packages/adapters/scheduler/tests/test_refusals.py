"""Every refused fire is an issue and tells its owner: one helper, found by AST."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from daimon.core import scheduler
from daimon.core.fire import admit, run
from daimon.core.ops_events import OpsEvent
from daimon.core.scheduler import drain_owner_notices, record_refusal
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MAIN = Path(run.__file__).read_text()
_ADMIT = Path(admit.__file__).read_text()


def _calls(name: str, source: str = _MAIN) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) == name
    ]


def _reason(call: ast.Call) -> str:
    arg = call.args[2] if len(call.args) > 2 else call.args[-1]
    return ast.unparse(arg)


def test_no_fire_records_an_error_without_the_refusal_helper() -> None:
    """A direct `record_result(error=...)` skipped Issues and the owner notice."""
    direct = [
        call.lineno
        for call in _calls("record_result")
        if not any(
            k.arg == "error" and isinstance(k.value, ast.Constant) and k.value.value is None
            for k in call.keywords
        )
    ]
    assert direct == [], f"record_result with an error at lines {direct}"


# Every refusal site in the fire, by the reason it records; a new one fails until listed.
REFUSALS = {"verdict.reason", "'routine_changed'", "'source_provenance_unknown'", "error.reason"}
# Every refusal admission decides, recorded through `verdict.reason` above.
ADMIT_REFUSALS = {
    "'no_owner'",
    "'tenant_not_found'",
    "'tenant_archived'",
    "'routine_dms_closed' if granted else 'owner_not_allowed'",
    "'routine_destination_parent_unknown'",
    "'routines_unsupported'",
    "error",
    "reason",
    "OWNER_LACKS_ROLE_ERROR",
    "decision.reason",
}


def test_every_refusal_site_is_listed() -> None:
    sites = {_reason(call) for call in _calls("refuse")}
    assert sites == REFUSALS, sites
    assert {_reason(call) for call in _calls("Refused", _ADMIT)} == ADMIT_REFUSALS
    # Every refusal goes through the store (`FireStore.refuse`), never around it.
    assert _calls("record_refusal") == [], "record_refusal called outside the store"


@pytest.mark.parametrize(
    ("fault", "logged", "severity"),
    [(False, "routine.fire_refused", "warning"), (True, "routine.fire_failed", None)],
)
async def test_a_refusal_is_an_issue_and_tells_the_owner_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fault: bool,
    logged: str,
    severity: str | None,
) -> None:
    """An owner-fixable refusal logs at info, apart from the faults alerting counts."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_1",
        agent_name="analyst",
        name="Weekly readout",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="summarise",
        next_fire_at=datetime.now(UTC),
        destination_kind="channel",
        destination_id="456",
    )
    await db_session.commit()
    reported: list[OpsEvent] = []

    def report(_sm: object, event: OpsEvent, *, logged: bool) -> None:
        reported.append(event)

    monkeypatch.setattr(scheduler, "report_ops_event", report)
    told: list[tuple[str, bool]] = []

    async def notify(row: RoutineRow, reason: str, channel_told: bool) -> None:
        told.append((reason, channel_told))

    with structlog.testing.capture_logs() as logs:
        for claimed in (row, row.model_copy(update={"last_error": "tenant_archived"})):
            async with db_session_factory() as s, s.begin():
                await record_refusal(
                    s, db_session_factory, claimed, "tenant_archived", fault=fault,
                    notify_owner=notify, expected_row=row,
                )  # fmt: skip
        await drain_owner_notices(timeout_s=5)

    async with db_session_factory() as s:
        saved = await get_routine(s, row.id, tenant_id=tenant.id)
    assert saved is not None and saved.last_error == "tenant_archived", "the refusal is saved"
    assert [(e.code, e.reason, e.severity) for e in reported] == [
        ("routine.fire_failed", "tenant_archived", severity)
    ] * 2, "each refusal is an issue"
    assert [log["event"] for log in logs if log["event"].startswith("routine.fire")] == [logged] * 2
    assert told == [("tenant_archived", False)], "the owner is told once per failing streak"


async def test_a_refusal_that_rolls_back_tells_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The owner hears of a refusal only once it is saved, so a retry does not tell twice."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="424243")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_1",
        agent_name="analyst",
        name="Weekly readout",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="summarise",
        next_fire_at=datetime.now(UTC),
        destination_kind="channel",
        destination_id="456",
    )
    await db_session.commit()
    told: list[str] = []

    async def notify(row: RoutineRow, reason: str, channel_told: bool) -> None:
        told.append(reason)

    with pytest.raises(RuntimeError):
        async with db_session_factory() as s, s.begin():
            await record_refusal(
                s, db_session_factory, row, "tenant_archived", notify_owner=notify, expected_row=row
            )
            raise RuntimeError("rolled back")
    await drain_owner_notices(timeout_s=5)

    assert told == [], "nobody is told of a refusal that was not saved"
