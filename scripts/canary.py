"""Hourly canary: one tiny real turn per backend, judged by its reply and outcome row.

For each backend, the QA bot asks Daimon "Reply with the single word: done" in that
backend's QA channel and waits for the answer. A backend passes when Daimon answered
"done" in the thread it opened on the question in under `--limit` seconds and, unless
`--no-outcomes`, that thread's single outcome row (read from the database in the
workers VM over IAP) ended `completed` in under `--limit` too. Every result is written
to Cloud Logging as `daimon-canary` (ERROR when it failed), which an alert policy in
the project watches. Exit status 1 if any backend failed.

The environment names the deployment, so no cloud or Discord id lives here:
`CANARY_PROJECT` (its Secret Manager holds DISCORD_QA_BOT_TOKEN), `CANARY_BOT_ID`
(Daimon's Discord user), `CANARY_CHANNELS` (JSON, backend to QA channel id), and for
the outcome read `CANARY_ZONE` and `CANARY_WORKERS_VM`. Uses only the standard
library and `gcloud`.

    python3 scripts/canary.py                    # every backend
    python3 scripts/canary.py --backend gemini   # one
    python3 scripts/canary.py --no-outcomes      # judge by the reply alone, no VM access
    python3 scripts/canary.py --force-fail       # proves the alert path
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

PROJECT = os.environ.get("CANARY_PROJECT", "")
ZONE = os.environ.get("CANARY_ZONE", "")
WORKERS_VM = os.environ.get("CANARY_WORKERS_VM", "")
DAIMON_BOT = os.environ.get("CANARY_BOT_ID", "")
API = "https://discord.com/api/v10"
PROMPT = "Reply with the single word: done"

CHANNELS: dict[str, str] = json.loads(os.environ.get("CANARY_CHANNELS") or "{}")
"""QA channels, each bound to one backend's agent."""

PROGRESS = ("thinking", "working")
"""Daimon's placeholder lines before the answer settles."""


@dataclass
class Check:
    backend: str
    channel: str
    ok: bool = False
    reason: str | None = None
    seconds: float | None = None
    cost_usd: str | None = None
    reply: str | None = None
    problem: str | None = None
    sent: str | None = None
    thread: str | None = None
    started: float = field(default_factory=time.time)


def gcloud(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["gcloud", *args, f"--project={PROJECT}"],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def qa_token() -> str:
    token = gcloud("secrets", "versions", "access", "latest", "--secret=DISCORD_QA_BOT_TOKEN")
    return token.strip()


class Discord:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, body: dict[str, object] | None = None) -> object:
        request = urllib.request.Request(
            API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bot {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "daimon-canary (1.0)",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code != 429 or attempt == 3:
                    raise
                time.sleep(float(error.headers.get("Retry-After", "2")))
        raise AssertionError("unreachable")


def answered(text: str) -> bool:
    """The requested answer, and nothing else: a failure notice never matches."""
    return text.strip().strip(".!*`'\"").lower() == "done"


def ask(discord: Discord, check: Check, label: str, wait_s: float) -> None:
    """Post the canary question and wait for Daimon's settled answer in the thread
    Daimon opens on that message (its id is the message's id)."""
    # The clock starts before the post, so a slow post response counts too.
    check.started = time.time()
    sent = discord.call(
        "POST",
        f"/channels/{check.channel}/messages",
        {"content": f"<@{DAIMON_BOT}> {PROMPT} ({label})"},
    )
    assert isinstance(sent, dict)
    check.sent = str(sent["id"])
    check.thread = check.sent
    while time.time() - check.started < wait_s:
        time.sleep(5)
        try:
            replies = discord.call("GET", f"/channels/{check.thread}/messages?limit=50")
        except urllib.error.HTTPError as error:
            if error.code == 404:  # the thread is not open yet
                continue
            raise
        assert isinstance(replies, list)
        for reply in reversed(replies):
            text = str(reply.get("content", "")).strip()
            if reply["author"]["id"] != DAIMON_BOT or not text:
                continue
            if text.lower().startswith(PROGRESS):
                continue
            check.reply, check.seconds = text[:200], round(time.time() - check.started, 1)
            if not answered(text):
                check.problem = "Daimon's reply is not the requested answer"
            return
    check.problem = f"no answer within {wait_s:.0f} s"


QUERY = """
import asyncio, json
from daimon.core.config import load_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
WANTED = json.loads({wanted!r})
SQL = (
    "SELECT channel_id, thread_id, reason, duration_ms, cost_usd::text AS cost_usd, started_at "
    "FROM turn_outcomes WHERE platform = 'discord' AND thread_id = ANY(:threads) "
    "ORDER BY started_at"
)
async def main():
    url = load_settings().database.url
    url = url.get_secret_value() if hasattr(url, "get_secret_value") else str(url)
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await connection.execute(text(SQL), {{"threads": WANTED}})).mappings().all()
    print("CANARY_ROWS " + json.dumps([dict(r) for r in rows], default=str))
asyncio.run(main())
"""


def outcomes(threads: list[str]) -> list[dict[str, object]]:
    """The outcome rows of exactly these canary threads, read in the Discord worker.
    Thread ids are Discord snowflakes, checked before they leave."""
    assert threads and all(t.isdigit() for t in threads)
    code = base64.b64encode(QUERY.format(wanted=json.dumps(threads)).encode()).decode()
    remote = (
        "sudo docker exec -i $(sudo docker ps -q --filter name=daimon-discord | head -1) "
        f"python -c \"import base64;exec(base64.b64decode('{code}'))\""
    )
    out = gcloud(
        "compute",
        "ssh",
        WORKERS_VM,
        f"--zone={ZONE}",
        "--tunnel-through-iap",
        "--quiet",
        f"--command={remote}",
    )
    for line in out.splitlines():
        if line.startswith("CANARY_ROWS "):
            return json.loads(line.removeprefix("CANARY_ROWS "))
    raise RuntimeError("the outcome query printed no rows line")


def settle(checks: list[Check], wait_s: float = 60.0) -> list[dict[str, object]]:
    """Read until every answered canary turn has its outcome row (it is committed
    after the reply), or `wait_s` passes."""
    threads = [c.thread for c in checks if c.thread is not None]
    if not threads:
        return []
    deadline = time.time() + wait_s
    while True:
        rows = outcomes(threads)
        have = {str(r["thread_id"]) for r in rows}
        pending = [c for c in checks if c.problem is None and c.thread not in have]
        if not pending or time.time() > deadline:
            return rows
        time.sleep(10)


def judge(check: Check, rows: list[dict[str, object]] | None, limit_s: float) -> None:
    """`rows` is None under `--no-outcomes`: the reply alone decides."""
    if check.problem is not None:
        return
    waited_s = check.seconds or 0.0
    if rows is None:
        if waited_s < limit_s:
            check.ok = True
        else:
            check.problem = f"the answer took {waited_s:.0f} s, not under {limit_s:.0f} s"
        return
    mine = [row for row in rows if str(row["thread_id"]) == check.thread]
    if len(mine) != 1:
        check.problem = f"{len(mine)} outcome rows for the canary thread, expected 1"
        return
    row = mine[0]
    check.reason = str(row["reason"])
    check.cost_usd = None if row["cost_usd"] is None else str(row["cost_usd"])
    duration_s = int(str(row["duration_ms"] or 0)) / 1000
    if row["channel_id"] != check.channel:
        check.problem = "the outcome row names another channel"
    elif check.reason != "completed":
        check.problem = f"the turn ended {check.reason}, not completed"
    elif not (duration_s < limit_s and waited_s < limit_s):
        check.problem = (
            f"the answer took {waited_s:.0f} s (turn {duration_s:.0f} s), not under {limit_s:.0f} s"
        )
    else:
        check.ok = True


def report(check: Check, env: str, run_url: str | None) -> None:
    payload = {
        "event": "canary.turn",
        "env": env,
        "run_url": run_url,
        **{k: v for k, v in asdict(check).items() if k != "started"},
    }
    print(json.dumps(payload))
    gcloud(
        "logging",
        "write",
        "daimon-canary",
        json.dumps(payload),
        "--payload-type=json",
        f"--severity={'INFO' if check.ok else 'ERROR'}",
    )


def run(checks: list[Check], limit: float, wait_s: float, read_outcomes: bool) -> None:
    """Every failure lands on a check, so each backend is still reported."""
    try:
        discord = Discord(qa_token())
    except Exception as error:
        for check in checks:
            check.problem = f"canary setup failed: {type(error).__name__}"
        return
    label = "canary " + datetime.now(UTC).strftime("%H:%MZ")

    def one(check: Check) -> None:
        try:
            ask(discord, check, label, wait_s)
        except Exception as error:
            check.problem = f"asking failed: {type(error).__name__}"

    with ThreadPoolExecutor(len(checks)) as pool:
        list(pool.map(one, checks))
    try:
        rows = settle(checks) if read_outcomes else None
    except Exception as error:
        for check in checks:
            check.problem = check.problem or f"outcome read failed: {type(error).__name__}"
        return
    for check in checks:
        judge(check, rows, limit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", action="append", choices=sorted(CHANNELS))
    parser.add_argument("--limit", type=float, default=180.0, help="seconds an answer may take")
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--env", default="staging", help="the environment named in each log entry")
    parser.add_argument(
        "--no-outcomes",
        action="store_true",
        help="judge by the reply alone, for a deployment the canary cannot reach over IAP",
    )
    parser.add_argument(
        "--force-fail",
        action="store_true",
        help="judge against a 0 s limit, so every backend fails and the alert fires",
    )
    args = parser.parse_args()
    if not (PROJECT and DAIMON_BOT and CHANNELS) or not (args.no_outcomes or ZONE and WORKERS_VM):
        parser.error("set the CANARY_* environment variables the docstring names")
    checks = [Check(b, CHANNELS[b]) for b in (args.backend or sorted(CHANNELS))]
    run(checks, 0.0 if args.force_fail else args.limit, args.limit, not args.no_outcomes)
    reported = True
    for check in checks:
        try:
            report(check, args.env, args.run_url)
        except Exception as error:
            print(f"report failed for {check.backend}: {type(error).__name__}", file=sys.stderr)
            reported = False
    return 0 if reported and all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
