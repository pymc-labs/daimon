"""Hourly staging canary: one tiny real turn per staging backend, judged by its outcome row.

For each backend, the QA bot asks Daimon "Reply with the single word: done" in that
backend's staging QA channel, waits for the answer, then reads the turn's outcome row
from the staging database over IAP. A backend passes when Daimon answered "done" in
the thread it opened on the question, and that thread's single outcome row ended
`completed`, both in under `--limit` seconds. Every result is written to Cloud Logging as
`daimon-canary` (ERROR when it failed), which the staging alert policy
"staging-daimon: canary turn failed" watches. Exit status 1 if any backend failed.

Staging only: the project, channels and VM are staging's, and the QA bot token is
read from staging Secret Manager. Uses only the standard library and `gcloud`.

    python3 scripts/staging_canary.py                    # every backend
    python3 scripts/staging_canary.py --backend gemini   # one
    python3 scripts/staging_canary.py --force-fail       # proves the alert path
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

PROJECT = "pymc-daimon-staging"
ZONE = "us-east4-c"
WORKERS_VM = "staging-daimon-workers"
DAIMON_BOT = "1530628070405308456"
API = "https://discord.com/api/v10"
PROMPT = "Reply with the single word: done"

CHANNELS = {
    "anthropic": "1560946913925140613",
    "openai": "1565273362098290838",
    "gemini": "1565149447187603607",
}
"""Staging QA channels, each bound to one backend's agent (Haiku 5.5, GPT-6 Luna,
Gemini 3.8 Flash). Selfhosted is not deployed on staging."""

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
                "User-Agent": "daimon-staging-canary (1.0)",
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
    sent = discord.call(
        "POST",
        f"/channels/{check.channel}/messages",
        {"content": f"<@{DAIMON_BOT}> {PROMPT} ({label})"},
    )
    assert isinstance(sent, dict)
    check.sent, check.started = str(sent["id"]), time.time()
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
    """The outcome rows of exactly these canary threads, read in the staging Discord
    worker. Thread ids are Discord snowflakes, checked before they leave."""
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


def judge(check: Check, rows: list[dict[str, object]], limit_s: float) -> None:
    if check.problem is not None:
        return
    mine = [row for row in rows if str(row["thread_id"]) == check.thread]
    if len(mine) != 1:
        check.problem = f"{len(mine)} outcome rows for the canary thread, expected 1"
        return
    row = mine[0]
    check.reason = str(row["reason"])
    check.cost_usd = None if row["cost_usd"] is None else str(row["cost_usd"])
    duration_s = int(str(row["duration_ms"] or 0)) / 1000
    waited_s = check.seconds or 0.0
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


def report(check: Check, run_url: str | None) -> None:
    payload = {
        "event": "canary.turn",
        "env": "staging",
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


def run(checks: list[Check], limit: float, wait_s: float) -> None:
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
        rows = settle(checks)
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
    parser.add_argument(
        "--force-fail",
        action="store_true",
        help="judge against a 0 s limit, so every backend fails and the alert fires",
    )
    args = parser.parse_args()
    checks = [Check(b, CHANNELS[b]) for b in (args.backend or sorted(CHANNELS))]
    run(checks, 0.0 if args.force_fail else args.limit, args.limit)
    for check in checks:
        report(check, args.run_url)
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
