"""`daimon evals suites|cases|arms|items|bundle`: evals in a workspace, as the operator.

The operator acts as an admin past the `evals` switch. Documents are read and printed as
JSON. `daimon evals bundle validate` checks a bundle folder, zip or encrypted file offline:
it needs no database or settings.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, TENANT_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import TenantSelector, discover_tenant, resolve_tenant_override
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError
from daimon.core.evals.access import Share, Subject
from daimon.core.evals.bundle import open_path
from daimon.core.evals.draft import auxiliary_completer
from daimon.core.evals.format import Arm, JsonObject, to_document
from daimon.core.evals.from_thread import ThreadRecord, ThreadSource
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.offline import leftovers_of, load_scrubber, save_map, scrub_path
from daimon.core.evals.scrub.sweep import Leftover
from daimon.core.evals.service import (
    CaseRef,
    DraftResult,
    EvalRefused,
    EvalService,
    ExportRequest,
    Page,
    SuiteDraft,
)
from daimon.core.evals.validate import InvalidDocument, validate_bundle
from daimon.core.evals.web_thread import WebChatSource
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
_ENTITIES = typer.Option(
    "--entities",
    envvar="DAIMON_EVALS__SCRUB_ENTITIES_FILE",
    help="JSON or YAML file of the private names to replace.",
)
_KEY = typer.Option(
    "--key",
    envvar="DAIMON_EVALS__SCRUB_KEY",
    help="Scrub key, at least 16 characters. Prefer the environment variable.",
)
_MAP = typer.Option("--map", help="Fake map file, kept between runs (private; read and written).")
_NER = typer.Option("--ner-model", help="spaCy model for names; empty to use known names only.")


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
    _run_rt(ctx, lambda s, a, _rt: work(s, a))


def _run_rt(
    ctx: typer.Context,
    work: Callable[[EvalService, Subject, CliRuntime], Awaitable[object]],
) -> None:
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
                _print(await work(EvalService(rt.sessionmaker, rt.settings), actor, rt))
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


class _RecordFile:
    """A thread record read from a JSON file (a `ThreadRecord`), for threads from anywhere."""

    def __init__(self, path: Path) -> None:
        self._record = ThreadRecord.model_validate_json(path.read_bytes())

    async def thread(self, ref: str) -> ThreadRecord:
        return self._record


@cases_app.command("from-thread")
def cases_from_thread(
    ctx: typer.Context,
    suite_id: str,
    link: Annotated[str | None, typer.Argument(help="Link or id of a web chat.")] = None,
    record: Annotated[Path | None, typer.Option(help="A thread record file instead.")] = None,
) -> None:
    """Scrubbed cases from a thread, one per answered turn. Needs DAIMON_EVALS__SCRUB_KEY."""
    if (link is None) == (record is None):
        raise typer.BadParameter("Give a web chat link, or --record FILE.")

    async def work(s: EvalService, a: Subject, rt: CliRuntime) -> list[CaseRef]:
        source: ThreadSource = (
            WebChatSource(rt.sessionmaker, tenant_id=a.tenant_id)
            if record is None
            else _RecordFile(record)
        )
        return await s.cases_from_thread(a, source, link or str(record), suite_id)

    _run_rt(ctx, work)


@cases_app.command("draft")
def cases_draft(
    ctx: typer.Context,
    suite_id: str,
    case_ids: Annotated[list[str], typer.Argument(help="Case ids.")],
    yes: Annotated[bool, typer.Option("--yes", help="Draft now and bill the workspace.")] = False,
) -> None:
    """Draft expectations with a model. Without --yes only the cost estimate is shown."""

    async def work(s: EvalService, a: Subject, rt: CliRuntime) -> DraftResult:
        complete = (
            auxiliary_completer(
                rt.anthropic,
                rt.sessionmaker,
                tenant_id=a.tenant_id,
                platform=a.platform,
                user_id=None,
                model=rt.settings.evals.draft_model,
                markup=rt.settings.billing.markup,
            )  # fmt: skip
            if yes
            else None
        )
        return await s.draft_expectations(a, suite_id, case_ids, complete=complete, confirm=yes)

    _run_rt(ctx, work)


@cases_app.command("dedupe")
def cases_dedupe(
    ctx: typer.Context,
    suite_id: str,
    keep: Annotated[
        int | None, typer.Option(help="Keep this many per group; drop the rest.")
    ] = None,
    threshold: float = 0.8,
) -> None:
    """Mark near-duplicate cases with a group; with --keep, thin each group."""
    _run(ctx, lambda s, a: s.dedupe(a, suite_id, threshold=threshold, keep=keep))


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


def _scrubber(entities: Path | None, key: str | None, map_path: Path | None, ner: str) -> Scrubber:
    if entities is None or not key:
        raise typer.BadParameter("--entities and a scrub key (DAIMON_EVALS__SCRUB_KEY) are needed.")
    try:
        return load_scrubber(entities, key, map_path=map_path, ner_model=ner or None)
    except (InvalidDocument, OSError, ValueError) as failed:
        problems = failed.problems if isinstance(failed, InvalidDocument) else [str(failed)]
        raise _Failed("; ".join(problems)) from None


def _leftover_rows(found: Sequence[Leftover]) -> list[JsonObject]:
    return [{"where": x.where, "kind": x.kind, "count": x.count} for x in found]


@bundle_app.command("scrub")
def bundle_scrub(
    path: Path,
    entities: Annotated[Path | None, _ENTITIES] = None,
    key: Annotated[str | None, _KEY] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write here, not in place.")] = None,
    map_path: Annotated[Path | None, _MAP] = None,
    ner_model: Annotated[str, _NER] = "en_core_web_sm",
    passphrase: Annotated[str | None, _PASSPHRASE] = None,
) -> None:
    """Scrub a bundle folder, zip or encrypted file offline, in place or into --out.

    Prints counts and places only, never the text found. Exits 1 when something is left.
    """
    scrubber = _scrubber(entities, key, map_path, ner_model)
    try:
        report = scrub_path(path, out, scrubber, passphrase=passphrase)
    except (InvalidDocument, OSError) as failed:
        problems = failed.problems if isinstance(failed, InvalidDocument) else [str(failed)]
        raise _Failed("; ".join(problems)) from None
    if map_path is not None:
        save_map(scrubber, map_path)
    _print(
        {
            "status": report.status,
            "cases": report.cases,
            "replaced": report.replaced,
            "blobs": report.blobs,
            "names_checked": report.names_checked,
            "leftovers": _leftover_rows(report.leftovers),
        }
    )
    if report.leftovers:
        raise typer.Exit(code=1)


@bundle_app.command("leftovers")
def bundle_leftovers(
    path: Path,
    entities: Annotated[Path | None, _ENTITIES] = None,
    key: Annotated[str | None, _KEY] = None,
    map_path: Annotated[Path | None, _MAP] = None,
    ner_model: Annotated[str, _NER] = "en_core_web_sm",
    passphrase: Annotated[str | None, _PASSPHRASE] = None,
) -> None:
    """List what a scrub would still change in a bundle, by place and kind. Exits 1 if any."""
    scrubber = _scrubber(entities, key, map_path, ner_model)
    try:
        found = leftovers_of(path, scrubber, passphrase=passphrase)
    except (InvalidDocument, OSError) as failed:
        problems = failed.problems if isinstance(failed, InvalidDocument) else [str(failed)]
        raise _Failed("; ".join(problems)) from None
    _print({"leftovers": _leftover_rows(found)})
    if found:
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
