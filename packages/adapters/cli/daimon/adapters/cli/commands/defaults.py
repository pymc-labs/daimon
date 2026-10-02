from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.defaults.apply import apply_defaults
from daimon.core.defaults.provisioning import verify_tenant_defaults
from daimon.core.defaults.report import ApplyReport, ResourceOutcome, classify_verification
from daimon.core.stores.tenants import list_tenants_by_platform
from rich.console import Console
from rich.table import Table

defaults_app = typer.Typer(help="System defaults reconciliation.")


def _outcome_dict(o: ResourceOutcome) -> dict[str, str | None]:
    d = dataclasses.asdict(o)
    d["action"] = o.action.value
    # Only skill imports set `refusal`; keep the operator JSON as it was.
    if d["refusal"] is None:
        del d["refusal"]
    return d


def _format_report_json(console: Console, report: ApplyReport) -> None:
    payload = {
        "agents": [_outcome_dict(o) for o in report.agents],
        "environments": [_outcome_dict(o) for o in report.environments],
        "skills": [_outcome_dict(o) for o in report.skills],
        "system_config": [_outcome_dict(o) for o in report.system_config],
    }
    console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)


def _format_report_table(console: Console, report: ApplyReport) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in ("kind", "name", "action", "anthropic_id", "error"):
        table.add_column(column)
    for bucket in (report.agents, report.environments, report.skills, report.system_config):
        for o in bucket:
            table.add_row(o.kind, o.name, o.action.value, o.anthropic_id or "", o.error or "")
    console.print(table)
    counts: dict[str, int] = {}
    for o in (
        *report.agents,
        *report.environments,
        *report.skills,
        *report.system_config,
    ):
        counts[o.action.value] = counts.get(o.action.value, 0) + 1
    console.print("  ".join(f"{n} {k}" for k, n in sorted(counts.items())))


@defaults_app.command("apply")
def defaults_apply_command(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
    defaults_root: Annotated[Path, typer.Option("--defaults-root")] = Path("defaults"),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await defaults_apply(
                rt=rt,
                console=console,
                dry_run=dry_run,
                as_json=as_json,
                defaults_root=defaults_root,
            )

    asyncio.run(_with_defaults())


async def defaults_apply(
    *,
    rt: CliRuntime,
    console: Console,
    dry_run: bool,
    as_json: bool,
    defaults_root: Path,
) -> None:
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    report = await apply_defaults(
        rt.sessionmaker,
        rt.anthropic,
        defaults_root,
        dry_run=dry_run,
        public_url=public_url,
    )
    if as_json:
        _format_report_json(console, report)
    else:
        _format_report_table(console, report)
    if report.is_failure():
        raise typer.Exit(code=1)


@dataclasses.dataclass(frozen=True)
class _TenantVerification:
    """One tenant's classification from a dry-run verify pass."""

    platform: str
    external_id: str
    status: str
    changed: tuple[dict[str, str], ...]


def _tenant_label(*, platform: str, external_id: str) -> str:
    return f"{platform}:{external_id}"


def _format_verify_table(
    console: Console, results: list[_TenantVerification], *, awaiting_reseed: int
) -> None:
    for v in results:
        label = _tenant_label(platform=v.platform, external_id=v.external_id)
        if v.status == "in_sync":
            console.print(f"{label}: in sync")
        elif v.status == "diverged":
            names = ", ".join(f"{c['kind']}:{c['name']}" for c in v.changed)
            console.print(f"{label}: diverged ({names})")
        else:
            console.print(f"{label}: unverifiable — could not compare against the shipped spec")
    console.print(
        f"{awaiting_reseed} tenant(s) skipped as awaiting re-seed (pending or failed "
        "provisioning, not a spec divergence)"
    )


def _format_verify_json(
    console: Console, results: list[_TenantVerification], *, awaiting_reseed: int
) -> None:
    payload = {
        "tenants": [
            {
                "platform": v.platform,
                "external_id": v.external_id,
                "status": v.status,
                "changed": list(v.changed),
            }
            for v in results
        ],
        "awaiting_reseed": awaiting_reseed,
        "ok": all(v.status == "in_sync" for v in results),
    }
    console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)


@defaults_app.command(
    "verify",
    help="Verify every ready install's live resources match the shipped defaults.",
)
def defaults_verify_command(
    as_json: Annotated[bool, JSON_OPTION] = False,
    defaults_root: Annotated[Path, typer.Option("--defaults-root")] = Path("defaults"),
) -> None:
    """Walk every ready install and confirm its live resources already match the
    shipped defaults tree, without writing anything.

    Each tenant is dry-run reconciled against `defaults_root`; an all-skipped
    result means that tenant's resources already match what would be seeded,
    exactly as a real reconcile would report. Exits nonzero if any tenant has
    drifted from the shipped spec, or if any tenant's comparison could not be
    made at all (both are treated as a failed verification — an install this
    command could not vouch for must not read as passing).
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await defaults_verify(
                rt=rt,
                console=console,
                as_json=as_json,
                defaults_root=defaults_root,
            )

    run_cli(_with_defaults(), console=console)


async def defaults_verify(
    *,
    rt: CliRuntime,
    console: Console,
    as_json: bool,
    defaults_root: Path,
) -> None:
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    tenants = await list_tenants_by_platform(rt.sessionmaker)

    results: list[_TenantVerification] = []
    awaiting_reseed = 0
    for tenant in tenants:
        if tenant.archived_at is not None:
            continue
        if tenant.platform == "cli":
            # cli:local is reconciled by `defaults apply` with account_id=None;
            # verify_tenant_defaults always derives a non-None account id from
            # the tenant, so its fingerprint can never match. It is not a real
            # install, just the local bootstrap row every deployment creates.
            continue
        if tenant.provision_status != "ready":
            # Pending/failed tenants are the boot sweep's job to re-seed, not a
            # propagation bug — counting them as diverged would make an unrelated
            # provisioning problem look like a shipped-spec/live-state mismatch.
            awaiting_reseed += 1
            continue
        # Sequential, deliberately: this runs in a deploy step against a provider
        # with rate limits, and the ready-tenant count here is small enough that
        # parallelizing would only add provider-side contention risk for no gain.
        report = await verify_tenant_defaults(
            rt.anthropic, rt.sessionmaker, defaults_root, tenant_id=tenant.id, public_url=public_url
        )
        outcome = classify_verification(report)
        results.append(
            _TenantVerification(
                platform=tenant.platform,
                external_id=tenant.external_id,
                status=outcome.status,
                changed=tuple({"kind": c.kind, "name": c.name} for c in outcome.changed),
            )
        )

    if as_json:
        _format_verify_json(console, results, awaiting_reseed=awaiting_reseed)
    else:
        _format_verify_table(console, results, awaiting_reseed=awaiting_reseed)

    failing = [v for v in results if v.status != "in_sync"]
    if not as_json:
        if failing:
            console.print(
                f"verify: FAILED — {len(failing)}/{len(results)} ready tenant(s) diverged "
                "or unverifiable"
            )
        else:
            console.print(
                f"verify: OK — {len(results)} ready tenant(s) in sync, "
                f"{awaiting_reseed} awaiting re-seed"
            )
    if failing:
        raise typer.Exit(code=1)
