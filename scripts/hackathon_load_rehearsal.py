"""Staging-only, disposable load rehearsal. Never run without an approved budget.

Run from the staging worker's Discord container (copy this file there first):

    docker exec daimon-discord-1 python /tmp/hackathon_load_rehearsal.py \
      --install --turn-load --cleanup --run-id rehearsal-1 --tenants 50 \
      --turns 20 --arrival-seconds 1800 --max-usd 5 \
      --staging-marker-id "$STAGING_MARKER_TENANT_ID" --i-am-staging

The marker must be an existing, staging-only tenant UUID, confirmed by the
operator before the run. The configured MCP public URL host must contain
"staging" (the staging runbook uses staging-daimon-mcp-*.run.app/mcp).
First use --dry-run (no settings, DB, or API access).
Use the same --run-id and --tenants with --cleanup to recover after interruption.
The budget gate checks posted ledger debits before starting each turn; turns
already in flight can still incur charges. Trial credit is split across the
synthetic tenants, and the normal prepaid admission check applies. The first
event timer observes the first SSE data frame. DB pool wait time is unavailable
through the current engine interface and is reported as n/a.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
        BetaManagedAgentsSpanModelRequestEndEvent,
    )
    from daimon.core.config import Settings
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROMPT = "Reply with OK only. Do not call tools."
SYNTHETIC_PREFIX = "hackathon-load-"
ACTIVE_TENANT: ContextVar[str | None] = ContextVar("rehearsal_tenant", default=None)


def arrival_offsets(count: int, seconds: float) -> tuple[float, ...]:
    """Spread starts evenly through the requested window; zero means burst."""
    if count < 1 or seconds < 0:
        raise ValueError("count must be positive and arrival seconds nonnegative")
    return tuple(i * seconds / count for i in range(count))


def budget_allows(debits_usd: Decimal, max_usd: Decimal) -> bool:
    return max_usd > 0 and debits_usd < max_usd


def staging_guard(
    *,
    acknowledged: bool,
    mcp_host: str | None,
    marker_exists: bool,
    marker_is_synthetic: bool,
) -> bool:
    return (
        acknowledged
        and mcp_host is not None
        and "staging" in mcp_host.lower()
        and marker_exists
        and not marker_is_synthetic
    )


def external_id(run_id: str, index: int) -> str:
    return f"{SYNTHETIC_PREFIX}{run_id}-{index:03d}"


@dataclass
class Result:
    name: str
    seconds: float = 0
    first_event_s: float | None = None
    status: str = "ok"
    skills_429: int = 0


class _FirstEventStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, mark: Callable[[], float]) -> None:
        self.inner = inner
        self.mark = mark

    async def __aiter__(self) -> AsyncIterator[bytes]:
        tail = b""
        marked = False
        async for chunk in self.inner:
            if not marked and b"data:" in tail + chunk:
                self.mark()
                marked = True
            tail = (tail + chunk)[-5:]
            yield chunk

    async def aclose(self) -> None:
        await self.inner.aclose()


class CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.statuses: dict[tuple[int, str], int] = {}
        self.skills_429: dict[str, int] = {}
        self.first_event_at: dict[str, float] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if response.status_code in (429, 529):
            key = (response.status_code, request.url.path)
            self.statuses[key] = self.statuses.get(key, 0) + 1
            tenant = ACTIVE_TENANT.get()
            if response.status_code == 429 and request.url.path.startswith("/v1/skills") and tenant:
                self.skills_429[tenant] = self.skills_429.get(tenant, 0) + 1
        label = ACTIVE_TENANT.get()
        if label and request.url.path.endswith("/events/stream"):
            response.stream = _FirstEventStream(
                cast(httpx.AsyncByteStream, response.stream),
                lambda: self.first_event_at.setdefault(label, time.monotonic()),
            )
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--install", action="store_true")
    p.add_argument("--turn-load", action="store_true")
    p.add_argument("--cleanup", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--i-am-staging", action="store_true")
    p.add_argument("--staging-marker-id", type=uuid.UUID)
    p.add_argument("--run-id", required=True)
    p.add_argument("--tenants", type=int, default=50)
    p.add_argument("--turns", type=int, default=20)
    p.add_argument("--arrival-seconds", type=float, default=1800)
    p.add_argument("--max-usd", type=Decimal, default=Decimal("5"))
    p.add_argument("--model", choices=("haiku", "event"), default="haiku")
    return p


def plan(args: argparse.Namespace) -> str:
    return (
        f"run={args.run_id} tenants={args.tenants} install={args.install} "
        f"turns={args.turns if args.turn_load else 0} model={args.model} "
        f"arrival_seconds={args.arrival_seconds} max_usd={args.max_usd} "
        f"cleanup={args.cleanup}"
    )


def expected_agent_model(model: str) -> str:
    """The model the seeded `daimon` agent runs on for this rehearsal mode."""
    from daimon.core.constants import DEFAULT_AGENT_MODEL

    return "claude-haiku-4-5" if model == "haiku" else DEFAULT_AGENT_MODEL


def _defaults_root(settings: Settings, model: str, temp: Path) -> Path:
    if model == "event":
        return settings.defaults_root
    root = temp / "defaults"
    shutil.copytree(settings.defaults_root, root)
    spec = root / "agents" / "daimon.yaml"
    source = spec.read_text()
    old = f"model: {expected_agent_model('event')}\n"
    if source.count(old) != 1:
        raise RuntimeError("default agent model changed; inspect the rehearsal model override")
    spec.write_text(source.replace(old, f"model: {expected_agent_model(model)}\n", 1))
    return root


async def _debits(sm: async_sessionmaker[AsyncSession], ids: list[uuid.UUID]) -> Decimal:
    from daimon.core.stores import tenant_ledger

    total = Decimal("0")
    async with sm() as session:
        for tenant_id in ids:
            for row in await tenant_ledger.list_for_tenant(session, tenant_id=tenant_id):
                if row.delta_usd < 0:
                    total -= row.delta_usd
    return total


async def _check_staging(
    sm: async_sessionmaker[AsyncSession],
    *,
    acknowledged: bool,
    mcp_host: str | None,
    marker_id: uuid.UUID | None,
) -> None:
    from daimon.core.stores.tenants import get_tenant

    print(f"staging guard MCP host: {mcp_host or '(unset)'}", flush=True)
    marker = None
    if marker_id is not None:
        async with sm() as session:
            marker = await get_tenant(session, marker_id)
    if not staging_guard(
        acknowledged=acknowledged,
        mcp_host=mcp_host,
        marker_exists=marker is not None,
        marker_is_synthetic=marker is not None and marker.external_id.startswith(SYNTHETIC_PREFIX),
    ):
        raise RuntimeError(
            "staging guard failed: require --i-am-staging, a staging MCP host, "
            "and a staging-only marker tenant"
        )


async def _install_one(
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    root: Path,
    name: str,
    public_url: str | None,
    credit: Decimal,
) -> Result:
    from anthropic import APIStatusError
    from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
    from daimon.core.stores.tenants import set_provision_status

    started = time.monotonic()
    try:
        tenant = await provision_tenant(
            sm, platform="discord", workspace_id=name, signup_credit=credit
        )
        report = await reconcile_tenant_defaults(
            client, sm, root, tenant_id=tenant.tenant_id, public_url=public_url
        )
        failed = [
            o
            for o in (*report.agents, *report.environments, *report.skills, *report.system_config)
            if o.action == "failed"
        ]
        await set_provision_status(
            sm,
            tenant_id=tenant.tenant_id,
            status="failed" if failed else "ready",
            reason="; ".join(o.error or o.name for o in failed) if failed else None,
            clear_reason=not failed,
        )
        return Result(
            name,
            time.monotonic() - started,
            status=f"failed: {len(failed)} resources" if failed else "ok",
        )
    except APIStatusError as exc:
        return Result(
            name,
            time.monotonic() - started,
            status=f"HTTP {exc.status_code} {exc.request.url.path}",
        )
    except Exception as exc:
        return Result(name, time.monotonic() - started, status=f"{type(exc).__name__}: {exc}")


async def _turn_one(
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: str,
    environment_id: str,
    name: str,
    label: str,
    transport: CountingTransport,
) -> Result:
    from anthropic import APIStatusError
    from daimon.core.billing import is_over_cap
    from daimon.core.headless_runner import run_turn
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.pricing import MODEL_PRICING
    from daimon.core.tenant_balance import is_over_balance
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.core.usage_recording import record_turn_usage

    started = time.monotonic()
    token = ACTIVE_TENANT.set(label)
    user_id = f"{name}-user"
    try:
        if await is_over_balance(sessionmaker=sm, tenant_id=tenant_id) or await is_over_cap(
            billing_config=None,
            sessionmaker=sm,
            tenant_id=tenant_id,
            user_id=user_id,
            now=datetime.now(UTC),
        ):
            return Result(label, status="admission refused")

        def recorder(session_id: str, model_id: str) -> Callable[..., Awaitable[None]]:
            bound = functools.partial(
                record_turn_usage,
                sessionmaker=sm,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                managed_session_id=session_id,
                model_id=model_id,
                markup=settings.billing.markup,
                pricing=MODEL_PRICING.get(model_id),
            )

            async def record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
                await bound(event=event)

            return record

        await run_turn(
            anthropic=client,
            agent_id=agent_id,
            environment_id=environment_id,
            trigger_message=PROMPT,
            origin="routine",
            mcp_settings=None,
            account_id=account_id,
            usage_record_factory=recorder,
            tenant_id=tenant_id,
            agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id),
            session_factory=sm,
            deadline=datetime.now(UTC) + timedelta(seconds=120),
            tool_safety=ToolSafetyPolicy(enabled=True),
        )
        first = transport.first_event_at.get(label)
        return Result(
            label, time.monotonic() - started, first_event_s=first - started if first else None
        )
    except APIStatusError as exc:
        first = transport.first_event_at.get(label)
        return Result(
            label,
            time.monotonic() - started,
            first_event_s=first - started if first else None,
            status=f"HTTP {exc.status_code} {exc.request.url.path}",
        )
    except Exception as exc:
        first = transport.first_event_at.get(label)
        return Result(
            label,
            time.monotonic() - started,
            first_event_s=first - started if first else None,
            status=f"{type(exc).__name__}: {exc}",
        )
    finally:
        ACTIVE_TENANT.reset(token)


async def _cleanup(
    client: AsyncAnthropic, sm: async_sessionmaker[AsyncSession], names: list[str]
) -> list[Result]:
    from anthropic import APIStatusError
    from daimon.core.defaults.ma_index import list_skills_strict
    from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT, strip_tenant_prefix
    from daimon.core.ma import delete_skill_and_versions
    from daimon.core.ma_identity import derive_tenant_uuid

    results: list[Result] = []
    agents = [agent async for agent in client.beta.agents.list(include_archived=False)]
    environments = [env async for env in client.beta.environments.list(include_archived=False)]
    skills = await list_skills_strict(client)
    for name in names:
        started = time.monotonic()
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=name)
        try:
            for skill in skills:
                if (
                    skill.source == "custom"
                    and strip_tenant_prefix(
                        tenant_id=tenant_id, display_title=skill.display_title or ""
                    )
                    is not None
                ):
                    await delete_skill_and_versions(client, skill.id)
            for env in environments:
                if env.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
                    try:
                        await client.beta.environments.delete(env.id)
                    except APIStatusError as exc:
                        if exc.status_code != 409:
                            raise
                        await client.beta.environments.archive(env.id)
            for agent in agents:
                if agent.metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id):
                    await client.beta.agents.archive(agent.id)
            subprocess.run(
                ["daimon", "tenants", "delete", "discord", name, "--cascade", "--yes"],
                check=True,
            )
            results.append(Result(name, time.monotonic() - started))
        except Exception as exc:
            results.append(
                Result(name, time.monotonic() - started, status=f"{type(exc).__name__}: {exc}")
            )
    return results


def _print_results(title: str, results: list[Result]) -> None:
    print(f"\n{title}: name | duration_s | first_event_s | skills_429 | status", flush=True)
    for row in results:
        first = "n/a" if row.first_event_s is None else f"{row.first_event_s:.2f}"
        print(
            f"{row.name} | {row.seconds:.2f} | {first} | {row.skills_429} | {row.status}",
            flush=True,
        )


async def _run(args: argparse.Namespace) -> None:
    from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
    from daimon.core.config import load_settings
    from daimon.core.constants import MA_MAX_RETRIES
    from daimon.core.db import build_engine, build_session_factory
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.skills.rate_limit import SkillsRateLimitedTransport

    settings = load_settings()
    engine = build_engine(str(settings.database.url))
    sm = build_session_factory(
        engine, crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys)
    )
    names = [external_id(args.run_id, i) for i in range(args.tenants)]
    ids = [derive_tenant_uuid(platform="discord", workspace_id=name) for name in names]
    try:
        await _check_staging(
            sm,
            acknowledged=args.i_am_staging,
            mcp_host=settings.mcp.public_url.host if settings.mcp.public_url else None,
            marker_id=args.staging_marker_id,
        )
        transport = CountingTransport(
            SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
        )
        with tempfile.TemporaryDirectory() as tmp:
            async with AsyncAnthropic(
                api_key=settings.anthropic.api_key.get_secret_value(),
                base_url=str(settings.anthropic.base_url),
                max_retries=MA_MAX_RETRIES,
                http_client=DefaultAsyncHttpxClient(transport=transport),
            ) as client:
                await _phases(args, client, sm, settings, names, ids, Path(tmp), transport)
        print("upstream 429/529 responses by endpoint:", transport.statuses)
    finally:
        await engine.dispose()


async def _phases(
    args: argparse.Namespace,
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    names: list[str],
    ids: list[uuid.UUID],
    temp: Path,
    transport: CountingTransport,
) -> None:
    from daimon.core.defaults.ma_index import (
        find_agent_by_daimon_tag,
        find_environment_by_daimon_tag,
    )
    from daimon.core.defaults.provisioning import derive_guild_account_uuid
    from daimon.core.stores.tenants import get_tenant

    root = _defaults_root(settings, args.model, temp)
    if args.install:
        start = time.monotonic()

        async def scheduled(index: int, offset: float) -> Result:
            await asyncio.sleep(max(0, start + offset - time.monotonic()))
            token = ACTIVE_TENANT.set(names[index])
            try:
                result = await _install_one(
                    client,
                    sm,
                    root,
                    names[index],
                    str(settings.mcp.public_url) if settings.mcp.public_url else None,
                    args.max_usd / args.tenants,
                )
                result.skills_429 = transport.skills_429.get(names[index], 0)
                return result
            finally:
                ACTIVE_TENANT.reset(token)

        installs = await asyncio.gather(
            *(
                scheduled(i, offset)
                for i, offset in enumerate(arrival_offsets(args.tenants, args.arrival_seconds))
            )
        )
        _print_results("install", list(installs))
    if args.turn_load:
        ready: list[tuple[str, uuid.UUID]] = []
        for name, tenant_id in zip(names, ids, strict=True):
            async with sm() as session:
                if await get_tenant(session, tenant_id) is not None:
                    ready.append((name, tenant_id))
        if not ready:
            raise RuntimeError("no synthetic tenants found for this run-id")
        resolved: list[tuple[str, uuid.UUID, str, str]] = []
        for name, tenant_id in ready[: args.turns]:
            agent = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
            environment = await find_environment_by_daimon_tag(
                client, tenant_id=tenant_id, name="default"
            )
            if agent is None or environment is None:
                print(f"skip {name}: defaults missing")
                continue
            expected_model = expected_agent_model(args.model)
            if agent.model.id != expected_model:
                print(f"skip {name}: agent model {agent.model.id} != {expected_model}")
                continue
            resolved.append((name, tenant_id, agent.id, environment.id))
        if not resolved:
            raise RuntimeError("no synthetic tenants have the selected model and defaults")
        turns: list[asyncio.Task[Result]] = []
        for i in range(args.turns):
            spent = await _debits(sm, ids)
            if not budget_allows(spent, args.max_usd):
                print(f"budget stop: ledger debits ${spent} >= ${args.max_usd}")
                break
            name, tenant_id, agent_id, environment_id = resolved[i % len(resolved)]
            turns.append(
                asyncio.create_task(
                    _turn_one(
                        client,
                        sm,
                        settings,
                        tenant_id,
                        derive_guild_account_uuid(tenant_id),
                        agent_id,
                        environment_id,
                        name,
                        f"{name}#{i}",
                        transport,
                    )
                )
            )
        _print_results("turns", await asyncio.gather(*turns))
        print(f"ledger debits: ${await _debits(sm, ids)}; DB pool waits: n/a")
    if args.cleanup:
        _print_results("cleanup", await _cleanup(client, sm, names))


def main() -> None:
    args = parser().parse_args()
    if not args.run_id.replace("-", "").isalnum() or not (1 <= len(args.run_id) <= 32):
        raise SystemExit("--run-id must be 1-32 letters, numbers, or hyphens")
    if (
        args.tenants < 1
        or not 1 <= args.turns <= 150
        or args.arrival_seconds < 0
        or args.max_usd <= 0
    ):
        raise SystemExit("require tenants >= 1, turns 1-150, arrival >= 0, max-usd > 0")
    if not (args.install or args.turn_load or args.cleanup):
        raise SystemExit("choose --install, --turn-load, or --cleanup")
    print(plan(args), flush=True)
    if args.dry_run:
        print("dry run: no settings, database, or upstream calls")
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
