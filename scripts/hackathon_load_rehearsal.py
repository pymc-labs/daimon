"""Staging-only, disposable load rehearsal. Never run without an approved budget.

Run from the staging worker's Discord container (copy this file there first).
For --discord, supply DISCORD_QA_BOT_TOKEN at runtime from the staging secret
store; the script never prints it. Only guild IDs in DISCORD_QA_GUILDS are
accepted. The driver creates temporary channels, and deleting them removes
the Daimon-created threads. It restores the guild's previous turn cap.

Backend example:

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
The Discord phase measures spend from the QA guild's ledger since the start of
that phase. It uses the guild's existing default agent model and reports it.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import os
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
REALISTIC_PROMPTS = (
    (
        "This is a synthetic hackathon exercise. Generate a reproducible 60-row CSV "
        "with columns team, hours and score, using three teams and random seed 42. "
        "Run Python with pandas to report row count and mean score by team. "
        "Keep the CSV in your workspace and answer briefly with the numbers."
    ),
    (
        "This is a synthetic hackathon exercise. Generate 50 reproducible points "
        "for x and y=2+0.7*x plus noise with seed 42. Run Python with matplotlib "
        "to make a labeled scatter plot and fitted line, save it as PNG, and "
        "attach the chart here. Answer briefly."
    ),
    (
        "This is a synthetic hackathon exercise. Generate 30 reproducible observations "
        "from y=1+0.5*x plus normal noise with seed 42. Run a tiny PyMC linear "
        "regression with 1 chain, 50 tune and 50 draws, report posterior means of "
        "intercept and slope, and note that the sample is too small for inference. "
        "Answer briefly."
    ),
)
SYNTHETIC_PREFIX = "hackathon-load-"
DISCORD_QA_GUILDS = frozenset({"1435062989119295640"})
DISCORD_QA_BOT_ID = "1533049261032341668"
DISCORD_DAIMON_BOT_ID = "1530628070405308456"
DISCORD_API = "https://discord.com/api/v10"
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
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: Decimal | None = None


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
        self.rate_limit_min: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        for key, value in response.headers.items():
            if key.startswith("anthropic-ratelimit-") and key.endswith("-remaining"):
                try:
                    remaining = int(value)
                except ValueError:
                    continue
                self.rate_limit_min[key] = min(self.rate_limit_min.get(key, remaining), remaining)
        if response.status_code == 429 or response.status_code >= 500:
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
    p.add_argument("--discord", action="store_true")
    p.add_argument("--discord-precreated", action="store_true")
    p.add_argument("--prompt-set", choices=("simple", "realistic"), default="simple")
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
    p.add_argument("--discord-guild-id", action="append", default=[])
    p.add_argument("--discord-turns", type=int, default=20)
    p.add_argument("--discord-concurrency", type=int, default=20)
    p.add_argument("--discord-watch-seconds", type=int, default=300)
    p.add_argument("--discord-file-every", type=int, default=0)
    p.add_argument("--discord-require-model")
    return p


def plan(args: argparse.Namespace) -> str:
    return (
        f"run={args.run_id} tenants={args.tenants} install={args.install} "
        f"turns={args.turns if args.turn_load else 0} "
        f"model={args.model if args.install or args.turn_load else 'n/a'} "
        f"discord={args.discord} discord_turns={args.discord_turns if args.discord else 0} "
        f"discord_concurrency={args.discord_concurrency} discord_model=tenant-default "
        f"discord_watch_seconds={args.discord_watch_seconds} "
        f"discord_precreated={args.discord_precreated} prompt_set={args.prompt_set} "
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
    prompt: str,
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
    started_at = datetime.now(UTC)
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
            trigger_message=prompt,
            origin="routine",
            mcp_settings=None,
            account_id=account_id,
            usage_record_factory=recorder,
            tenant_id=tenant_id,
            agent_uuid=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id),
            session_factory=sm,
            deadline=datetime.now(UTC) + timedelta(seconds=240),
            tool_safety=ToolSafetyPolicy(enabled=True),
        )
        first = transport.first_event_at.get(label)
        result = Result(
            label, time.monotonic() - started, first_event_s=first - started if first else None
        )
        from daimon.core.stores.turn_outcomes import list_for_tenant

        outcome = None
        for attempt in range(10):
            async with sm() as session:
                outcomes = await list_for_tenant(session, tenant_id, limit=20)
            outcome = next((row for row in outcomes if row.started_at >= started_at), None)
            if outcome is not None:
                break
            if attempt < 9:
                await asyncio.sleep(0.2)
        if outcome is not None:
            result.status = str(outcome.reason)
            result.input_tokens = outcome.input_tokens
            result.output_tokens = outcome.output_tokens
            result.cache_read_input_tokens = outcome.cache_read_input_tokens
            result.cache_creation_input_tokens = outcome.cache_creation_input_tokens
            result.cost_usd = outcome.cost_usd
        return result
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
    print(
        f"\n{title}: name | duration_s | first_event_s | skills_429 | "
        "input | output | cache_read | cache_create | cost_usd | status",
        flush=True,
    )
    for row in results:
        first = "n/a" if row.first_event_s is None else f"{row.first_event_s:.2f}"
        print(
            f"{row.name} | {row.seconds:.2f} | {first} | {row.skills_429} | "
            f"{row.input_tokens} | {row.output_tokens} | {row.cache_read_input_tokens} | "
            f"{row.cache_creation_input_tokens} | {row.cost_usd} | {row.status}",
            flush=True,
        )


@dataclass
class DiscordResult:
    message_id: str
    thread_id: str | None = None
    first_s: float | None = None
    final_s: float | None = None
    turn_s: float | None = None
    status: str = "timeout"
    attachments: int = 0
    first_event_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: Decimal | None = None


def _percentile(values: list[float], percentile: float) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    value = ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (
        position - low
    )
    return f"{value:.2f}"


def _outcome_tokens(row: object) -> int:
    return sum(
        getattr(row, field) or 0
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


def _peak_tokens_per_minute(rows: list[tuple[datetime, int]]) -> int:
    """Peak rolling 60-second output plus input token total, by outcome completion."""
    ordered = sorted(rows)
    left = 0
    running = 0
    peak = 0
    for right, (ended, tokens) in enumerate(ordered):
        running += tokens
        while left <= right and (ended - ordered[left][0]).total_seconds() >= 60:
            running -= ordered[left][1]
            left += 1
        peak = max(peak, running)
    return peak


def _require_discord_model(actual: str, required: str | None) -> None:
    if required is not None and actual != required:
        raise RuntimeError(f"QA default agent model {actual} != {required}")


class DiscordREST:
    def __init__(self, token: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=DISCORD_API,
            headers={"Authorization": f"Bot {token}"},
            timeout=20,
        )
        self._routes: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self, method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object] | list[dict[str, object]]:
        # Discord's message bucket is per channel. Serialize each route and
        # honor retry_after rather than letting a burst lose trigger messages.
        route = path.split("?", 1)[0]
        lock = self._routes.setdefault(route, asyncio.Lock())
        async with lock:
            for _ in range(8):
                response = await self.client.request(method, path, json=body)
                if response.status_code == 429:
                    payload = response.json()
                    await asyncio.sleep(float(payload.get("retry_after", 1)))
                    continue
                response.raise_for_status()
                return cast(dict[str, object] | list[dict[str, object]], response.json())
        raise RuntimeError(f"Discord rate limit persisted: {method} {route}")


def _message_time(message: dict[str, object], started: datetime, *, latest: bool = False) -> float:
    timestamp = (message.get("edited_timestamp") if latest else None) or message["timestamp"]
    return max(0.0, (datetime.fromisoformat(str(timestamp)) - started).total_seconds())


async def _discord_watch(
    rest: DiscordREST,
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_id: str,
    message_id: str,
    started: datetime,
    created_threads: set[str],
    existing_thread_id: str | None = None,
    watch_seconds: int = 300,
) -> DiscordResult:
    from daimon.core.stores.turn_outcomes import list_for_tenant

    result = DiscordResult(message_id, thread_id=existing_thread_id)
    deadline = time.monotonic() + watch_seconds
    outcome_seen = False
    while time.monotonic() < deadline:
        # At 100 pre-created threads, two-second polling alone would consume
        # Discord's 50-request/second global bot allowance.
        await asyncio.sleep(4 if existing_thread_id else 2)
        if result.thread_id is None:
            response = await rest.client.get(f"/channels/{message_id}")
            if response.status_code == 200:
                thread = response.json()
                if str(thread.get("parent_id")) == channel_id:
                    result.thread_id = message_id
                    created_threads.add(message_id)
            elif response.status_code != 404:
                if response.status_code == 429:
                    await asyncio.sleep(float(response.json().get("retry_after", 1)))
                    continue
                response.raise_for_status()
        target = result.thread_id or channel_id
        messages = cast(
            list[dict[str, object]],
            await rest.request("GET", f"/channels/{target}/messages?after={message_id}&limit=100"),
        )
        bot_messages: list[dict[str, object]] = []
        for item in messages:
            author_value = item.get("author")
            if not isinstance(author_value, dict):
                continue
            author = cast(dict[str, object], author_value)
            if str(author.get("id")) == DISCORD_DAIMON_BOT_ID:
                bot_messages.append(item)
        if bot_messages:
            first = min(bot_messages, key=lambda item: str(item["id"]))
            result.first_s = _message_time(first, started)
            result.final_s = max(_message_time(item, started, latest=True) for item in bot_messages)
            edited = [item for item in bot_messages if item.get("edited_timestamp")]
            if edited:
                result.first_event_s = min(
                    _message_time(item, started, latest=True) for item in edited
                )
            result.attachments = sum(
                len(cast(list[object], item.get("attachments") or [])) for item in bot_messages
            )
            if any(
                "too many chats" in str(item.get("content") or "")
                or "at capacity" in str(item.get("content") or "")
                for item in bot_messages
            ):
                result.status = "shed"
                return result
        if result.thread_id:
            async with sm() as session:
                outcomes = await list_for_tenant(session, tenant_id, limit=200)
            done = next(
                (
                    row
                    for row in outcomes
                    if row.thread_id == result.thread_id and row.started_at >= started
                ),
                None,
            )
            if done is not None:
                result.status = str(done.reason)
                result.turn_s = max(0.0, (done.ended_at - started).total_seconds())
                result.input_tokens = done.input_tokens
                result.output_tokens = done.output_tokens
                result.cache_read_input_tokens = done.cache_read_input_tokens
                result.cache_creation_input_tokens = done.cache_creation_input_tokens
                result.cost_usd = done.cost_usd
                # Read Discord once more after the outcome to catch final edits and replies.
                if not outcome_seen:
                    outcome_seen = True
                    continue
                if result.final_s is not None:
                    return result
    return result


async def _discord_phase(
    args: argparse.Namespace,
    client: AsyncAnthropic,
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.tenants import get_tenant, set_turn_cap

    guild_ids = list(dict.fromkeys(args.discord_guild_id))
    if not guild_ids or any(guild not in DISCORD_QA_GUILDS for guild in guild_ids):
        raise RuntimeError("Discord guild must be explicitly selected from the QA-only allow-list")
    token = os.environ.get("DISCORD_QA_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_QA_BOT_TOKEN must be set at runtime")
    if settings.discord is None or DISCORD_QA_BOT_ID not in settings.discord.qa_bot_user_ids:
        raise RuntimeError("staging Discord config must allow the QA bot")
    print(f"Discord global cap: {settings.discord.max_concurrent_turns or 'unset'}", flush=True)
    rest = DiscordREST(token)
    previous: dict[uuid.UUID, int | None] = {}
    created_threads: set[str] = set()
    created_channels: set[str] = set()
    precreated: list[tuple[str, uuid.UUID]] = []
    tasks: set[asyncio.Task[DiscordResult]] = set()
    try:
        me = await rest.request("GET", "/users/@me")
        assert isinstance(me, dict)
        if str(me.get("id")) != DISCORD_QA_BOT_ID:
            raise RuntimeError("token does not belong to the staging QA bot")
        channels: list[tuple[str, uuid.UUID]] = []
        for guild_id in guild_ids:
            member = await rest.request(
                "GET", f"/guilds/{guild_id}/members/{DISCORD_DAIMON_BOT_ID}"
            )
            assert isinstance(member, dict)
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
            async with sm() as session:
                tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.provision_status != "ready" or tenant.archived_at:
                raise RuntimeError(f"QA guild {guild_id} has no ready tenant")
            agent = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
            if agent is None:
                raise RuntimeError(f"QA guild {guild_id} has no default agent")
            print(f"Discord QA guild {guild_id} default agent model: {agent.model.id}", flush=True)
            _require_discord_model(agent.model.id, args.discord_require_model)
            previous[tenant_id] = tenant.turn_cap
            for index in range(min(5, args.discord_concurrency)):
                channel = await rest.request(
                    "POST",
                    f"/guilds/{guild_id}/channels",
                    body={"name": f"qa-load-{args.run_id}-{index + 1}", "type": 0},
                )
                assert isinstance(channel, dict)
                channel_id = str(channel["id"])
                created_channels.add(channel_id)
                channels.append((channel_id, tenant_id))
        if args.discord_precreated:
            # Discord shares a 50-create/300s guild bucket across channels.
            # Eight per ten seconds also stays below the short burst allowance.
            made_at: list[float] = []
            for index in range(args.discord_turns):
                channel_id, tenant_id = channels[index % len(channels)]
                while True:
                    now = time.monotonic()
                    made_at = [t for t in made_at if now - t < 300]
                    recent = [t for t in made_at if now - t < 10]
                    if len(made_at) < 45 and len(recent) < 8:
                        break
                    await asyncio.sleep(1)
                starter = await rest.request(
                    "POST",
                    f"/channels/{channel_id}/messages",
                    body={"content": f"Synthetic load rehearsal {args.run_id} thread {index + 1}"},
                )
                assert isinstance(starter, dict)
                thread = await rest.request(
                    "POST",
                    f"/channels/{channel_id}/messages/{starter['id']}/threads",
                    body={"name": f"load-{args.run_id}-{index + 1}"},
                )
                assert isinstance(thread, dict)
                thread_id = str(thread["id"])
                created_threads.add(thread_id)
                precreated.append((thread_id, tenant_id))
                made_at.append(time.monotonic())
            print(f"Pre-created {len(precreated)} QA threads", flush=True)
        baseline = await _debits(sm, list(previous))
        stage_started = datetime.now(UTC)
        for tenant_id in previous:
            async with sm() as session, session.begin():
                await set_turn_cap(session, tenant_id=tenant_id, cap=args.discord_concurrency)
        print(
            f"Discord channels: {len(channels)}; tenant cap: {args.discord_concurrency}", flush=True
        )

        async def launch(index: int) -> DiscordResult:
            if args.discord_precreated:
                channel_id, tenant_id = precreated[index]
            else:
                channel_id, tenant_id = channels[index % len(channels)]
            prompt = (
                REALISTIC_PROMPTS[index % len(REALISTIC_PROMPTS)]
                if args.prompt_set == "realistic"
                else PROMPT
            )
            if args.discord_file_every and (index + 1) % args.discord_file_every == 0:
                prompt = (
                    "Create and attach a tiny text file named rehearsal.txt containing OK. "
                    "Reply briefly."
                )
            posted = await rest.request(
                "POST",
                f"/channels/{channel_id}/messages",
                body={
                    "content": f"<@{DISCORD_DAIMON_BOT_ID}> {prompt}",
                    "allowed_mentions": {"parse": [], "users": [DISCORD_DAIMON_BOT_ID]},
                },
            )
            assert isinstance(posted, dict)
            # Measure user-visible latency from Discord's accepted mention,
            # excluding the QA bot's own POST wait and rate-limit delay.
            started = datetime.fromisoformat(str(posted["timestamp"]))
            return await _discord_watch(
                rest,
                sm,
                tenant_id,
                channel_id,
                str(posted["id"]),
                started,
                created_threads,
                existing_thread_id=channel_id if args.discord_precreated else None,
                watch_seconds=args.discord_watch_seconds,
            )

        results: list[DiscordResult] = []
        for batch_start in range(0, args.discord_turns, args.discord_concurrency):
            spent = await _debits(sm, list(previous)) - baseline
            if not budget_allows(spent, args.max_usd):
                print(
                    f"Discord budget stop: QA ledger debits ${spent} >= ${args.max_usd}", flush=True
                )
                break
            tasks = {
                asyncio.create_task(launch(index))
                for index in range(
                    batch_start,
                    min(batch_start + args.discord_concurrency, args.discord_turns),
                )
            }
            results.extend(await asyncio.gather(*tasks))
            tasks.clear()
        created_threads.update(row.thread_id for row in results if row.thread_id)
        if not args.discord_precreated:
            notices = 0
            for channel_id in created_channels:
                messages = cast(
                    list[dict[str, object]],
                    await rest.request("GET", f"/channels/{channel_id}/messages?limit=100"),
                )
                for item in messages:
                    author = item.get("author")
                    if (
                        not isinstance(author, dict)
                        or str(author.get("id")) != DISCORD_DAIMON_BOT_ID
                    ):
                        continue
                    content = str(item.get("content") or "")
                    if content.startswith(("Opening your chat", "Your chat is ready:")):
                        notices += 1
            print(f"Discord opening notices: {notices}", flush=True)
        for row in results:
            print(
                f"Discord {row.message_id}: thread={row.thread_id or 'none'} "
                f"ack={row.first_s} first_event={row.first_event_s} "
                f"final={row.final_s} turn={row.turn_s} "
                f"status={row.status} attachments={row.attachments} "
                f"input={row.input_tokens} output={row.output_tokens} "
                f"cache_read={row.cache_read_input_tokens} "
                f"cache_create={row.cache_creation_input_tokens} cost=${row.cost_usd}",
                flush=True,
            )
        for name, values in (
            ("first_status", [r.first_s for r in results if r.first_s is not None]),
            ("first_event", [r.first_event_s for r in results if r.first_event_s is not None]),
            ("final", [r.final_s for r in results if r.final_s is not None]),
            ("turn", [r.turn_s for r in results if r.turn_s is not None]),
        ):
            print(
                f"Discord {name} p50={_percentile(values, 0.5)}s p95={_percentile(values, 0.95)}s",
                flush=True,
            )
        from daimon.core.stores.turn_outcomes import list_for_tenant

        async with sm() as session:
            outcome_rows = [
                row
                for tenant_id in previous
                for row in await list_for_tenant(session, tenant_id, limit=500)
                if row.started_at >= stage_started
            ]
        token_rate = _peak_tokens_per_minute(
            [(row.ended_at, _outcome_tokens(row)) for row in outcome_rows]
        )
        print(f"Discord peak completion-window tokens/min: {token_rate}", flush=True)
        print(
            f"Discord totals: launched={len(results)} "
            f"threads={sum(r.thread_id is not None for r in results)} "
            f"shed={sum(r.status == 'shed' for r in results)} "
            f"errors={sum(r.status not in ('completed', 'shed') for r in results)} "
            f"attachments={sum(r.attachments for r in results)} "
            f"spent=${await _debits(sm, list(previous)) - baseline}",
            flush=True,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        cleanup_errors: list[str] = []
        for channel_id in created_channels:
            try:
                await rest.request("DELETE", f"/channels/{channel_id}")
                response = await rest.client.get(f"/channels/{channel_id}")
                if response.status_code != 404:
                    cleanup_errors.append(
                        f"channel {channel_id}: HTTP {response.status_code} after delete"
                    )
            except Exception as exc:
                cleanup_errors.append(f"channel {channel_id}: {exc}")
        for tenant_id, cap in previous.items():
            async with sm() as session, session.begin():
                await set_turn_cap(session, tenant_id=tenant_id, cap=cap)
        await rest.close()
        print(
            f"Discord cleanup: channels={len(created_channels)} "
            f"threads={len(created_threads)} tenant_caps_restored={len(previous)}",
            flush=True,
        )
        if cleanup_errors:
            raise RuntimeError("Discord cleanup failed: " + "; ".join(cleanup_errors))


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
        print("upstream 429/5xx responses by endpoint:", transport.statuses)
        print("minimum Anthropic rate-limit remaining:", transport.rate_limit_min)
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
        seed_slots = asyncio.Semaphore(2)

        async def scheduled(index: int, offset: float) -> Result:
            await asyncio.sleep(max(0, start + offset - time.monotonic()))
            token = ACTIVE_TENANT.set(names[index])
            try:
                async with seed_slots:
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
                tenant = await get_tenant(session, tenant_id)
                if tenant is not None and tenant.provision_status == "ready":
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
        stage_started = datetime.now(UTC)
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
                        REALISTIC_PROMPTS[i % len(REALISTIC_PROMPTS)]
                        if args.prompt_set == "realistic"
                        else PROMPT,
                    )
                )
            )
        turn_results = await asyncio.gather(*turns)
        _print_results("turns", turn_results)
        from daimon.core.stores.turn_outcomes import list_for_tenant

        async with sm() as session:
            outcome_rows = [
                row
                for _, tenant_id, _, _ in resolved
                for row in await list_for_tenant(session, tenant_id, limit=20)
                if row.started_at >= stage_started
            ]
        token_rate = _peak_tokens_per_minute(
            [(row.ended_at, _outcome_tokens(row)) for row in outcome_rows]
        )
        print(f"Backend peak completion-window tokens/min: {token_rate}", flush=True)
        print(f"ledger debits: ${await _debits(sm, ids)}; DB pool waits: n/a")
    if args.discord:
        await _discord_phase(args, client, sm, settings)
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
        or not 1 <= args.discord_turns <= 150
        or not 1 <= args.discord_concurrency <= 150
        or not 1 <= args.discord_watch_seconds <= 900
        or args.discord_file_every < 0
    ):
        raise SystemExit("require tenants >= 1, turns 1-150, arrival >= 0, max-usd > 0")
    if not (args.install or args.turn_load or args.discord or args.cleanup):
        raise SystemExit("choose --install, --turn-load, --discord, or --cleanup")
    if args.discord_precreated and not args.discord:
        raise SystemExit("--discord-precreated requires --discord")
    if args.discord and (
        not args.discord_guild_id or any(g not in DISCORD_QA_GUILDS for g in args.discord_guild_id)
    ):
        raise SystemExit("--discord requires an allow-listed --discord-guild-id")
    print(plan(args), flush=True)
    if args.dry_run:
        print("dry run: no settings, database, or upstream calls")
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
