"""`daimon evals suites|cases|arms|items|bundle`: evals in a workspace, as the operator.

The operator acts as an admin past the `evals` switch. Documents are read and printed as
JSON. `daimon evals bundle validate` checks a bundle folder, zip or encrypted file offline:
it needs no database or settings.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, TENANT_OPTION
from daimon.adapters.cli.runtime import build_runtime
from daimon.adapters.cli.tenant import TenantSelector, discover_tenant, resolve_tenant_override
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError
from daimon.core.evals.access import Share, Subject
from daimon.core.evals.bundle import open_path
from daimon.core.evals.format import Arm, JsonObject, to_document
from daimon.core.evals.service import EvalRefused, EvalService, ExportRequest, Page, SuiteDraft
from daimon.core.evals.validate import InvalidDocument, validate_bundle
from daimon.core.stores.tenants import get_tenant
from pydantic import BaseModel, TypeAdapter
from rich.console import Console

evals_app = typer.Typer(help="Eval suites, cases, arms, run items and bundles.")
suites_app = typer.Typer(help="Eval suites.")
cases_app = typer.Typer(help="Cases of an eval suite.")
arms_app = typer.Typer(help="Arms: the agent setups a run compares.")
items_app = typer.Typer(help="Items of an eval run.")
bundle_app = typer.Typer(help="Validate, export and import bundles.")
for name, sub in (
    ("suites", suites_app),
    ("cases", cases_app),
    ("arms", arms_app),
    ("items", items_app),
    ("bundle", bundle_app),
):
    evals_app.add_typer(sub, name=name)

_console = Console(stderr=True)
_PASSPHRASE = typer.Option(
    "--passphrase", envvar="DAIMON_EVAL_PASSPHRASE", help="Passphrase of an encrypted bundle."
)
_SHARES = TypeAdapter(list[Share])


class _Failed(DaimonError):
    pass


@evals_app.callback()
def evals_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


def _print(value: object) -> None:
    if isinstance(value, BaseModel):
        value = to_document(value)
    elif isinstance(value, list):
        value = [to_document(v) if isinstance(v, BaseModel) else v for v in value]  # pyright: ignore[reportUnknownVariableType]
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _read_json(path: Path) -> JsonObject:
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise _Failed(f"{path}: expected a JSON object")
    return document  # pyright: ignore[reportUnknownVariableType]


def _run(ctx: typer.Context, work: Callable[[EvalService, Subject], Awaitable[object]]) -> None:
    selector: TenantSelector | None = ctx.obj

    async def go() -> None:
        async with build_runtime(load_settings()) as rt:
            async with rt.sessionmaker() as session:
                override = await resolve_tenant_override(session, selector)
                tenant_id: uuid.UUID = await discover_tenant(session, override=override)
                tenant = await get_tenant(session, tenant_id)
            if tenant is None:
                raise _Failed("no such workspace")
            actor = Subject(
                tenant_id=tenant_id,
                platform=tenant.platform,
                key=f"cli:{rt.settings.cli.local_user}",
                role="owner",
                operator=True,
            )
            try:
                _print(await work(EvalService(rt.sessionmaker, rt.settings), actor))
            except EvalRefused as exc:
                raise _Failed("; ".join((exc.message, *exc.problems))) from None

    run_cli(go(), console=_console)


@suites_app.command("list")
def suites_list(ctx: typer.Context) -> None:
    _run(ctx, lambda s, a: s.suites(a))


@suites_app.command("get")
def suites_get(ctx: typer.Context, suite_id: str, version: int | None = None) -> None:
    _run(ctx, lambda s, a: s.suite(a, suite_id, version))


@suites_app.command("save")
def suites_save(ctx: typer.Context, draft: Path) -> None:
    """Create a suite or save its next version from a JSON draft."""
    parsed = SuiteDraft.model_validate(_read_json(draft))
    _run(ctx, lambda s, a: s.save_suite(a, parsed))


@suites_app.command("share")
def suites_share(ctx: typer.Context, suite_id: str, shares: Path) -> None:
    """Replace who a suite is shared with, from a JSON list of shares."""
    parsed = _SHARES.validate_json(shares.read_bytes())

    async def work(s: EvalService, a: Subject) -> str:
        await s.share(a, suite_id, parsed)
        return "shared"

    _run(ctx, work)


@cases_app.command("list")
def cases_list(ctx: typer.Context, suite_id: str, version: int | None = None) -> None:
    _run(ctx, lambda s, a: s.cases(a, suite_id, version))


@cases_app.command("get")
def cases_get(ctx: typer.Context, suite_id: str, case_id: str, revision: int | None = None) -> None:
    _run(ctx, lambda s, a: s.case(a, suite_id, case_id, revision))


@cases_app.command("put")
def cases_put(ctx: typer.Context, suite_id: str, case: Path) -> None:
    """Add a case from a JSON file, or save its next revision."""
    document = _read_json(case)
    _run(ctx, lambda s, a: s.put_case(a, suite_id, document))


@cases_app.command("validate")
def cases_validate(case: Path) -> None:
    """Check a case file against the format, offline."""
    check = EvalService.validate_case(_read_json(case))
    _print(check)
    if check.problems:
        raise typer.Exit(code=1)


@arms_app.command("list")
def arms_list(ctx: typer.Context) -> None:
    _run(ctx, lambda s, a: s.arms(a))


@arms_app.command("save")
def arms_save(ctx: typer.Context, arm: Path) -> None:
    parsed = Arm.model_validate(_read_json(arm))
    _run(ctx, lambda s, a: s.save_arm(a, parsed))


@items_app.command("list")
def items_list(ctx: typer.Context, run_id: str, offset: int = 0, limit: int = 50) -> None:
    _run(ctx, lambda s, a: s.items(a, run_id, Page(offset=offset, limit=limit)))


@items_app.command("get")
def items_get(ctx: typer.Context, item_id: str) -> None:
    _run(ctx, lambda s, a: s.item(a, item_id))


@bundle_app.command("validate")
def bundle_validate(path: Path, passphrase: Annotated[str | None, _PASSPHRASE] = None) -> None:
    """Check a bundle folder, zip or encrypted file, offline. Exits 1 on any problem."""
    try:
        report = validate_bundle(open_path(path, passphrase=passphrase))
    except (InvalidDocument, OSError) as failed:
        problems = failed.problems if isinstance(failed, InvalidDocument) else [str(failed)]
        warnings, counts = [], {}
    else:
        problems, warnings = report.problems, report.warnings
        counts = {
            "suites": len(report.suites),
            "cases": sum(len(s.cases) for s in report.suites),
            "runs": len(report.runs),
            "items": sum(len(r.items) for r in report.runs),
            "scrub": report.manifest.scrub.status if report.manifest else None,
        }
    _print({"ok": not problems, **counts, "problems": problems, "warnings": warnings})
    if problems:
        raise typer.Exit(code=1)


@bundle_app.command("export")
def bundle_export(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option("--out", help="Where to write the bundle.")],
    suite: Annotated[list[str] | None, typer.Option("--suite", help="Suite id.")] = None,
    run: Annotated[list[str] | None, typer.Option("--run", help="Run id.")] = None,
    passphrase: Annotated[str | None, _PASSPHRASE] = None,
    include_identities: bool = False,
) -> None:
    """Export suites (latest versions) and runs as a bundle file."""
    request = ExportRequest.model_validate(
        {
            "suites": [{"id": s} for s in suite or []],
            "runs": run or [],
            "passphrase": passphrase,
            "include_identities": include_identities,
        }
    )

    async def work(s: EvalService, a: Subject) -> JsonObject:
        ref = await s.export_bundle(a, request)
        _, content = await s.blob(a, ref.sha256)
        out.write_bytes(content)
        return to_document(ref)

    _run(ctx, work)


@bundle_app.command("import")
def bundle_import(
    ctx: typer.Context,
    path: Path,
    passphrase: Annotated[str | None, _PASSPHRASE] = None,
    accept_unscrubbed: bool = False,
) -> None:
    """Import a bundle zip or encrypted file into the workspace."""
    data = path.read_bytes()

    async def chunks() -> AsyncIterator[bytes]:
        yield data

    _run(
        ctx,
        lambda s, a: s.import_bundle(
            a, chunks(), accept_unscrubbed=accept_unscrubbed, passphrase=passphrase
        ),
    )
