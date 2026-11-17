"""Operator view of the conversations kept for a monitored agent: counts and times only."""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import build_runtime
from daimon.core import monitoring_view as view
from daimon.core.config import load_settings
from daimon.core.monitoring_view import Where
from daimon.core.web_admin import audit
from pydantic import BaseModel
from rich.console import Console

monitoring_app = typer.Typer(help="Read what is kept for monitored agents.")


class _Line(BaseModel):
    conversation_id: str
    where: str
    account_id: uuid.UUID
    questions: int
    tool_calls: int
    first_at: datetime
    last_at: datetime


def _day(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{flag} must be a date like 2026-01-31") from exc


@monitoring_app.command("list")
def monitoring_list_command(
    tenant: uuid.UUID,
    agent: Annotated[str, typer.Option(help="The agent's id.")],
    where: Annotated[
        Where | None, typer.Option(help="direct_messages, web or coding_tools.")
    ] = None,
    account: Annotated[uuid.UUID | None, typer.Option(help="Only this person's account.")] = None,
    first: Annotated[str | None, typer.Option(help="First UTC day, YYYY-MM-DD.")] = None,
    last: Annotated[str | None, typer.Option(help="Last UTC day, YYYY-MM-DD.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List an agent's kept conversations, newest first. The read is audited."""
    first_day, last_day = _day(first, "--first"), _day(last, "--last")
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker.begin() as session:
            await audit(
                session,
                tenant_id=tenant,
                actor_account_id=None,
                action=view.AUDIT_LIST,
                target=agent,
                detail="Operator command line",
                at=datetime.now(UTC),
            )
            rows = await view.list_conversations(
                session,
                tenant_id=tenant,
                ma_agent_id=agent,
                where=where,
                account_id=account,
                first=first_day,
                last=last_day,
                limit=limit,
            )
            emit_rows(
                console,
                [
                    _Line(
                        conversation_id=view.conversation_id(r),
                        where=view.where_of(r.platform),
                        account_id=r.account_id,
                        questions=r.questions,
                        tool_calls=r.tools,
                        first_at=r.first_at,
                        last_at=r.last_at,
                    )
                    for r in rows
                ],
                as_json=as_json,
                columns=(
                    "where",
                    "account_id",
                    "questions",
                    "tool_calls",
                    "first_at",
                    "last_at",
                    "conversation_id",
                ),
            )

    run_cli(run(), console=console)
