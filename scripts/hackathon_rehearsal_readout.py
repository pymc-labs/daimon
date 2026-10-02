"""Read-only staging rehearsal readout from Cloud Logging and Monitoring.

Run with: uv run python scripts/hackathon_rehearsal_readout.py --start ... --end ...
Optionally pass --database-url (a staging read-only Postgres URL) for outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

EVENTS = (
    "turn.failed",
    "turn.outcome_write_failed",
    "turn.skipped.concurrency_shed",
    "turn.skipped.global_concurrency_shed",
    "turn.anthropic_overloaded",
    "anthropic.spend_limit_reached",
    "guild_seed_failed",
    "guild_seed_unexpected",
    "guild_seed_default_agent_missing",
    "guild_seed_status_flip_failed",
    "guild_join_failed",
    "guild_reconcile_failed_ready_tenant",
    "slack.boot_sweep_tenant_failed",
    "slack.boot_sweep_default_agent_missing",
    "slack.boot_sweep_status_flip_failed",
    "slack.boot_sweep_reconcile_failed_ready_tenant",
    "defaults.reconcile_failed",
    "scheduler.github_installation_reconciliation.failed",
)
METRICS = {
    "sql backends": "cloudsql.googleapis.com/database/postgresql/num_backends",
    "sql cpu %": "cloudsql.googleapis.com/database/cpu/utilization",
    "mcp instances": "run.googleapis.com/container/instance_count",
    "mcp latency p95 ms": "run.googleapis.com/request_latencies",
    "vm memory %": "agent.googleapis.com/memory/percent_used",
    "vm cpu %": "agent.googleapis.com/cpu/utilization",
}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError(
            "timestamps must have an explicit UTC offset (Z or +00:00)"
        )
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def gcloud(*args: str) -> str:
    result = subprocess.run(["gcloud", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gcloud failed: {args[0]}")
    return result.stdout


def payload(entry: dict[str, Any]) -> dict[str, Any]:
    """gcplogs puts a JSON line inside jsonPayload.message; Cloud Run may not."""
    value = entry.get("jsonPayload")
    if isinstance(value, dict):
        value = cast(dict[str, Any], value)
        message = value.get("message")
        if isinstance(message, str):
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    return cast(dict[str, Any], parsed)
            except json.JSONDecodeError:
                pass
        if "event" in value:
            return value
    message = entry.get("textPayload")
    if isinstance(message, str):
        try:
            parsed = json.loads(message)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
        except json.JSONDecodeError:
            pass
    return {}


def logging_entries(project: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    names = ("runtime.health", *EVENTS)
    terms = " OR ".join(
        f'jsonPayload.event="{name}" OR jsonPayload.message:"{name}" OR textPayload:"{name}"'
        for name in names
    )
    query = f'timestamp>="{iso(start)}" AND timestamp<"{iso(end)}" AND ({terms})'
    raw = gcloud("logging", "read", query, f"--project={project}", "--format=json", "--limit=50000")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("Cloud Logging returned an unexpected shape")
    return cast(list[dict[str, Any]], data)


def monitoring(
    project: str, metric: str, start: datetime, end: datetime, token: str
) -> list[dict[str, Any]]:
    params = {
        "filter": f'metric.type="{metric}"',
        "interval.startTime": iso(start),
        "interval.endTime": iso(end),
        "view": "FULL",
        "pageSize": "1000",
    }
    if metric == METRICS["mcp latency p95 ms"]:
        params["aggregation.alignmentPeriod"] = "60s"
        params["aggregation.perSeriesAligner"] = "ALIGN_PERCENTILE_95"
    url = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
    series: list[dict[str, Any]] = []
    while True:
        request = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        series.extend(result.get("timeSeries", []))
        page = result.get("nextPageToken")
        if not page:
            return series
        params["pageToken"] = page


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    import math

    return ordered[math.ceil(len(ordered) * fraction) - 1]


def fmt(value: float | int | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def point_value(point: dict[str, Any]) -> float | None:
    value = point.get("value", {})
    for key in ("doubleValue", "int64Value"):
        if key in value:
            return float(value[key])
    return None


async def outcomes(
    url: str | None, start: datetime, end: datetime
) -> list[tuple[datetime, str, int]] | None:
    if not url:
        return None
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            rows = (
                await connection.execute(
                    text(
                        "SELECT started_at, reason, duration_ms FROM turn_outcomes "
                        "WHERE started_at >= :start AND started_at < :end"
                    ),
                    {"start": start, "end": end},
                )
            ).mappings()
            return [
                (cast(datetime, row["started_at"]), str(row["reason"]), int(row["duration_ms"]))
                for row in rows
            ]
    finally:
        await engine.dispose()


def table(
    stage: str,
    start: datetime,
    end: datetime,
    entries: list[dict[str, Any]],
    series: dict[str, list[dict[str, Any]]],
    turns: list[tuple[datetime, str, int]] | None,
) -> str:
    lines = [f"\n## {stage} ({iso(start)} to {iso(end)})", "metric | value", "--- | ---"]
    health: list[tuple[datetime, dict[str, Any]]] = []
    events: Counter[str] = Counter()
    for entry in entries:
        when = timestamp(entry["timestamp"])
        if not start <= when < end:
            continue
        item = payload(entry)
        event = item.get("event")
        if event == "runtime.health":
            health.append((when, item))
        elif event in EVENTS:
            events[event] += 1
    lines.append(f"health samples | {len(health)}")
    # Sliding minute across all process heartbeats; scale a partial minute.
    attempts: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    pool: tuple[int, int] | None = None
    lag_max = lag_p95 = inflight = per_tenant = 0.0
    prep_active = prep_waiting = 0
    remaining_min: dict[str, int] = {}
    discord_429 = 0
    discord_retry_max = 0.0
    for when, item in health:
        interval = float(item.get("interval_s") or 30)
        for endpoint, statuses in item.get("anthropic_responses", {}).items():
            attempts[endpoint].append((when, float(statuses.get("429", 0)), interval))
        db_pool = item.get("db_pool", {})
        checked = int(db_pool.get("checkedout", 0))
        size = int(db_pool.get("size", 0))
        if pool is None or checked > pool[0]:
            pool = (checked, size)
        gate = item.get("prep_gate", {})
        if isinstance(gate, dict):
            gate = cast(dict[str, Any], gate)
            prep_active = max(prep_active, int(gate.get("active", 0)))
            prep_waiting = max(prep_waiting, int(gate.get("waiting", 0)))
        for name, value in item.get("anthropic_ratelimit_remaining_min", {}).items():
            number = int(value)
            remaining_min[name] = min(remaining_min.get(name, number), number)
        for count in item.get("discord_ratelimits", {}).values():
            discord_429 += int(count.get("count", 0))
            discord_retry_max = max(discord_retry_max, float(count.get("max_retry_s", 0)))
        lag = item.get("loop_lag_ms", {})
        lag_max = max(lag_max, float(lag.get("max", 0)))
        lag_p95 = max(lag_p95, float(lag.get("p95", 0)))
        inflight = max(inflight, float(item.get("turns_in_flight", {}).get("global", 0)))
        per_tenant = max(
            per_tenant,
            float(item.get("turns_in_flight", {}).get("per_tenant_max") or 0),
        )
    rates: dict[str, float] = {}
    for endpoint, samples in attempts.items():
        peak = 0.0
        for when, _, _ in samples:
            recent = [
                (at, count, interval)
                for at, count, interval in samples
                if when - timedelta(seconds=60) < at <= when
            ]
            if recent:
                span = min(
                    60.0,
                    (
                        when - min(at - timedelta(seconds=interval) for at, _, interval in recent)
                    ).total_seconds(),
                )
                peak = max(peak, sum(count for _, count, _ in recent) * 60 / span)
        rates[endpoint] = peak
    rate_text = ", ".join(f"{key}={value:.1f}" for key, value in sorted(rates.items()))
    lines.extend(
        [
            f"peak 429/min by endpoint | {rate_text or 'none'}",
            f"peak pool checkedout/size | {f'{pool[0]}/{pool[1]}' if pool else 'n/a'}",
            f"peak loop lag max/p95 ms | {f'{fmt(lag_max)}/{fmt(lag_p95)}' if health else 'n/a'}",
            f"peak turns in flight | {int(inflight) if health else 'n/a'}",
            f"peak per-tenant turns in flight | {int(per_tenant) if health else 'n/a'}",
            f"prep gate active/waiting peaks | {prep_active}/{prep_waiting}"
            if health
            else "prep gate active/waiting peaks | n/a",
            f"Anthropic remaining minima | {dict(sorted(remaining_min.items())) or 'n/a'}",
            f"Discord 429 / max retry s | {discord_429}/{discord_retry_max:.2f}",
        ]
    )
    for event in EVENTS:
        lines.append(f"{event} | {events[event]}")
    stage_turns = [
        (reason, duration) for when, reason, duration in turns or [] if start <= when < end
    ]
    if turns is None:
        lines.append("turn outcomes / duration | n/a (set --database-url)")
    else:
        reasons = Counter(reason for reason, _ in stage_turns)
        durations = [float(duration) for _, duration in stage_turns]
        lines.append(f"turn outcomes by reason | {dict(sorted(reasons.items()))}")
        duration_text = f"{fmt(percentile(durations, 0.5))}/{fmt(percentile(durations, 0.95))}"
        lines.append(f"turn duration p50/p95 ms | {duration_text}")
    for label, rows in series.items():
        values: list[float] = []
        instances_by_time: dict[datetime, float] = defaultdict(float)
        for row in rows:
            resource = row.get("resource", {})
            metric_labels = row.get("metric", {}).get("labels", {})
            if label.startswith("mcp") and resource.get("type") != "cloud_run_revision":
                continue
            if label.startswith("mcp") and not str(
                resource.get("labels", {}).get("service_name", "")
            ).endswith("-daimon-mcp"):
                continue
            if label == "vm memory %" and metric_labels.get("state") != "used":
                continue
            if label == "vm cpu %" and metric_labels.get("cpu_state") != "idle":
                continue
            for point in row.get("points", []):
                when = timestamp(point["interval"]["endTime"])
                if start <= when < end and (value := point_value(point)) is not None:
                    if label == "vm cpu %":
                        value = 100 - value
                    if label == "sql cpu %":
                        value *= 100
                    if label == "mcp instances":
                        instances_by_time[when] += value
                    else:
                        values.append(value)
        if label == "mcp instances":
            values = list(instances_by_time.values())
        lines.append(f"peak {label} | {fmt(max(values) if values else None)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=timestamp, required=True)
    parser.add_argument("--end", type=timestamp, required=True)
    parser.add_argument("--stage", action="append", default=[], metavar="NAME=START/END")
    parser.add_argument("--project", default="pymc-daimon-staging")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DAIMON_DATABASE_URL"),
        help="Staging Postgres URL for turn outcomes; defaults to DAIMON_DATABASE_URL.",
    )
    args = parser.parse_args()
    if args.project.endswith("-prod") and not args.allow_production:
        parser.error("production project requires --allow-production")
    if args.end <= args.start:
        parser.error("--end must follow --start")
    stages: list[tuple[str, datetime, datetime]] = [("all", args.start, args.end)]
    if args.stage:
        stages = []
        for spec in args.stage:
            try:
                name, window = spec.split("=", 1)
                left, right = window.split("/", 1)
                begin, finish = timestamp(left), timestamp(right)
            except (ValueError, argparse.ArgumentTypeError) as exc:
                parser.error(f"invalid --stage {spec}: {exc}")
            if not name or not args.start <= begin < finish <= args.end:
                parser.error(f"--stage {spec} is outside the main window")
            stages.append((name, begin, finish))
    entries = logging_entries(args.project, args.start, args.end)
    token = gcloud("auth", "print-access-token").strip()
    series: dict[str, list[dict[str, Any]]] = {}
    for name, metric in METRICS.items():
        try:
            series[name] = monitoring(args.project, metric, args.start, args.end, token)
        except Exception as exc:
            print(f"monitoring {name}: {exc}", file=sys.stderr)
            series[name] = []
    try:
        turns = asyncio.run(outcomes(args.database_url, args.start, args.end))
    except Exception as exc:
        print(f"turn outcomes unavailable: {exc}", file=sys.stderr)
        turns = None
    print(f"Project: {args.project}; logs: {len(entries)}")
    for name, begin, finish in stages:
        print(table(name, begin, finish, entries, series, turns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
