"""A provider step's files outlive their first post and go with their run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core.output_runtime import SessionOutput
from daimon.core.routine_files import (
    PREFIX,
    RoutinePayload,
    decode_payload,
    deliver_routine_files,
    read_file,
    stage_files,
)
from daimon.core.stores import workflow_runs
from daimon.core.stores.file_uploads import (
    delete_stale_unowned_uploads,
    get_upload,
    workflow_output_title,
)
from daimon.testing.workflows import make_world
from sqlalchemy.ext.asyncio import AsyncSession


class _Outputs:
    session_id = "ses_provider"
    settles = False

    async def list_outputs(self) -> tuple[SessionOutput, ...]:
        return (SessionOutput("a1", "report.csv", "text/csv", 4),)

    async def read_output(self, output_id: str) -> bytes:
        return b"a,b\n"

    async def delete_output(self, output_id: str) -> None:
        pass


async def test_staged_files_when_posted_twice_then_stay_until_their_run_is_deleted(
    db_session: AsyncSession,
) -> None:
    world = await make_world(db_session, {"f": {"kind": "finish"}}, "f")
    run = await world.run()
    files, _ = await stage_files(
        world.sm,
        _Outputs(),  # type: ignore[arg-type]
        tenant_id=world.tenant_id,
        title=workflow_output_title(run.id),
    )
    payload = PREFIX + RoutinePayload(text="", files=files).model_dump_json()
    sent: list[bytes] = []

    async def post(file: Any) -> None:
        sent.append(file.content)

    async def notice(text: str) -> None:
        raise AssertionError(text)

    async def may_post() -> bool:
        return True

    for _ in range(2):
        await deliver_routine_files(
            None,
            payload,
            max_bytes=10,
            post=post,
            notice=notice,
            may_post=may_post,
            sessions=world.sm,
            tenant_id=world.tenant_id,
            keep_staged=True,
        )
    assert sent == [b"a,b\n", b"a,b\n"], "a second post reads the same staged file"
    ref = decode_payload(payload).files[0]
    assert await read_file(None, ref, world.sm, world.tenant_id) == b"a,b\n", "so does the page"
    async with world.sm.begin() as s:
        swept = await delete_stale_unowned_uploads(
            s, created_before=datetime.now(UTC) + timedelta(days=400), limit=10
        )
    assert swept == 0, "the age sweep leaves a run's files"

    async with world.sm.begin() as s:
        await workflow_runs.delete_runs(s, [run.id])
    async with world.sm() as s:
        gone = await get_upload(s, tenant_id=world.tenant_id, handle_id=ref.file_id)
    assert gone is None, "the files go with the run"
