"""Read-only CI deployment gate before staging observations."""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import cast

from pydantic import JsonValue

from qa.live.types import Message, Pending, obj, objects, utcnow

REPOSITORY = "pymc-labs/daimon"
DEPLOY_JOB = "Deploy to GCP (staging)"
COMPLETED_JOBS: dict[str, list[Message]] = {}


class DeployNotQuiet(Pending):
    """A known active/recent deploy exhausted the observation wait."""


class QuietGate:
    def __init__(self) -> None:
        # Completed runs are immutable; share them across scenarios and retries.
        self.completed_jobs = COMPLETED_JOBS
        self.last: Message = {}
        self.deadline: float | None = None

    def query(self, arguments: list[str]) -> JsonValue:
        try:
            timeout = 30 if self.deadline is None else min(30, self.deadline - time.monotonic())
            if timeout <= 0:
                raise Pending("deployment quiet query deadline exhausted")
            response = subprocess.run(
                ["gh", *arguments, "--repo", REPOSITORY],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if response.returncode:
                raise Pending("read-only GitHub deployment query unavailable")
            return cast(JsonValue, json.loads(response.stdout))
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise Pending(
                "read-only GitHub deployment query unavailable: " + type(exc).__name__
            ) from None

    def snapshot(self) -> Message:
        def inventory(value: JsonValue) -> list[Message]:
            if not isinstance(value, list) or any(
                not isinstance(row, dict) or not row.get("databaseId") or not row.get("status")
                for row in value
            ):
                raise Pending("main CI run inventory is incomplete")
            return objects(value)

        recent = inventory(
            self.query(
                [
                    "run",
                    "list",
                    "--branch",
                    "main",
                    "--workflow",
                    "CI",
                    "--limit",
                    "100",
                    "--json",
                    "databaseId,status",
                ]
            )
        )
        active: list[Message] = []
        for status in ("in_progress", "queued", "waiting", "requested", "pending"):
            rows = inventory(
                self.query(
                    [
                        "run",
                        "list",
                        "--branch",
                        "main",
                        "--workflow",
                        "CI",
                        "--status",
                        status,
                        "--limit",
                        "1000",
                        "--json",
                        "databaseId,status",
                    ]
                )
            )
            if len(rows) >= 1000:
                raise Pending("active main CI inventory may be incomplete")
            active.extend(rows)
        runs = {str(r["databaseId"]): r for r in recent + active if r.get("databaseId")}
        if not runs:
            raise Pending("main CI deployment history is unavailable")
        busy: list[JsonValue] = []
        completed: list[datetime] = []
        unread = [
            identity
            for identity, run in runs.items()
            if run.get("status") != "completed" or identity not in self.completed_jobs
        ]

        def read_jobs(identity: str) -> tuple[str, list[Message]]:
            data = obj(self.query(["run", "view", identity, "--json", "jobs"]))
            if not isinstance(data.get("jobs"), list):
                raise Pending("CI jobs response is incomplete")
            return identity, objects(data["jobs"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            observed = dict(pool.map(read_jobs, unread))
        for identity, run in runs.items():
            jobs = observed.get(identity, self.completed_jobs.get(identity, []))
            if identity in observed and run.get("status") == "completed":
                self.completed_jobs[identity] = jobs
            for job in jobs:
                name = str(job.get("name") or "")
                if name != DEPLOY_JOB and not name.startswith(DEPLOY_JOB + " / "):
                    continue
                if job.get("conclusion") == "skipped":
                    continue
                if job.get("status") != "completed":
                    busy.append({"run_id": identity, "status": job.get("status")})
                else:
                    if not isinstance(job.get("completedAt"), str):
                        raise Pending("deployment completion timestamp is missing")
                    try:
                        stamp = datetime.fromisoformat(
                            str(job["completedAt"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        raise Pending("deployment completion timestamp is invalid") from None
                    if stamp.tzinfo is None:
                        raise Pending("deployment completion timestamp lacks timezone")
                    completed.append(stamp)
        latest = max(completed) if completed else None
        if latest is None:
            raise Pending("no actual staging deployment found in recent main CI history")
        age = (utcnow() - latest).total_seconds() if latest else None
        self.last = {
            "quiet": not busy and age is not None and age >= 180,
            "busy": busy,
            "last_completed": latest.isoformat() if latest else None,
            "age_s": age,
        }
        return self.last

    def wait(self, *, timeout_s: float = 900) -> Message:
        deadline = time.monotonic() + timeout_s
        self.deadline = deadline
        snapshot: Message
        while True:
            try:
                snapshot = self.snapshot()
            except Pending as exc:
                # Missing tools/auth/history cannot improve by burning a whole
                # observation window; keep this alertable harness PENDING.
                raise Pending("GitHub deployment evidence unavailable: " + str(exc)) from None
            if snapshot.get("quiet") is True:
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployNotQuiet("staging deploy did not become quiet within 15 minutes")
            time.sleep(min(30, remaining))
            # The last snapshot proved a known busy/recent deploy. Once its
            # observation window expires, do not start a query with no budget
            # and turn that known-not-quiet outcome into an evidence outage.
            if time.monotonic() >= deadline:
                raise DeployNotQuiet("staging deploy did not become quiet within 15 minutes")
