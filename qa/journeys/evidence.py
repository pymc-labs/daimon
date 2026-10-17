"""Small, explicit evidence contract shared by scripted and browser journeys."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image

Status = Literal["PASS", "FAIL", "PENDING"]
Boundary = Literal[
    "scripted_transport",
    "adapter_entry",
    "backend_effect",
    "browser_dom",
    "browser_pixel",
    "native_client",
    "provider",
]


@dataclass(frozen=True)
class Observation:
    boundary: Boundary
    required: bool
    observed: bool
    detail: str
    artifact: str | None = None


@dataclass
class Check:
    name: str
    status: Status
    observations: list[Observation] = field(default_factory=list[Observation])
    detail: str = ""

    def effective_status(self) -> Status:
        if self.status == "FAIL":
            return "FAIL"
        if self.status == "PASS" and not any(
            item.required and item.observed for item in self.observations
        ):
            return "PENDING"
        if any(item.required and not item.observed for item in self.observations):
            return "PENDING"
        return self.status


@dataclass
class RunReport:
    run_id: str
    journey: str
    layer: str
    started_at: datetime
    finished_at: datetime | None = None
    app_input_tokens: int = 0
    app_output_tokens: int = 0
    controller_input_tokens: int = 0
    controller_output_tokens: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    checks: list[Check] = field(default_factory=list[Check])
    status: Status = "PENDING"

    @property
    def minutes(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return max(0.0, (end - self.started_at).total_seconds() / 60)

    def finalize(self) -> Status:
        statuses = [check.effective_status() for check in self.checks]
        self.status = (
            "FAIL"
            if "FAIL" in statuses
            else "PASS"
            if statuses and all(item == "PASS" for item in statuses)
            else "PENDING"
        )
        return self.status


def _private_directory(path: Path) -> None:
    resolved = path.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise ValueError("QA artifacts must be outside every Git checkout")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def create_artifact_dir(run_id: str, root: Path | None = None) -> Path:
    """Create a private run directory outside the source checkout."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("run_id must be a plain path component")
    target_root = root or Path(
        os.environ.get("DAIMON_QA_ARTIFACT_ROOT", Path.home() / ".local/state/daimon/qa-journeys")
    )
    _private_directory(target_root)
    path = target_root / run_id
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def write_report(report: RunReport, artifact_dir: Path) -> Path:
    report.finalize()
    _private_directory(artifact_dir)
    path = artifact_dir / "report.json"
    payload = asdict(report)
    payload["minutes"] = report.minutes
    payload["checks"] = [
        {**asdict(check), "status": check.effective_status()} for check in report.checks
    ]
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        stream.write(json.dumps(payload, default=str, indent=2) + "\n")
    path.chmod(0o600)
    return path


@dataclass(frozen=True)
class ScreenshotDiff:
    step_id: str
    status: Status
    changed: bool | None
    changed_pixels: int | None
    artifact: str | None
    detail: str


def compare_screenshots(
    current: Path, accepted_baseline: Path | None, output_dir: Path, step_id: str
) -> ScreenshotDiff:
    """Write a baseline/current/difference panel only for a changed step.

    The caller selects an already accepted baseline. This function never
    accepts, updates, or silently creates one.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", step_id):
        raise ValueError("step_id must be a plain path component")
    if not current.is_file():
        return ScreenshotDiff(step_id, "PENDING", None, None, None, "current screenshot missing")
    if accepted_baseline is None or not accepted_baseline.is_file():
        return ScreenshotDiff(step_id, "PENDING", None, None, None, "accepted baseline missing")
    with Image.open(accepted_baseline) as baseline_image, Image.open(current) as current_image:
        baseline = baseline_image.convert("RGB")
        observed = current_image.convert("RGB")
    width = max(baseline.width, observed.width)
    height = max(baseline.height, observed.height)
    difference = Image.new("RGB", (width, height), "black")
    changed_pixels = 0
    for y in range(height):
        for x in range(width):
            same_bounds = (
                x < baseline.width
                and y < baseline.height
                and x < observed.width
                and y < observed.height
            )
            if same_bounds and baseline.getpixel((x, y)) == observed.getpixel((x, y)):
                continue
            changed_pixels += 1
            difference.putpixel((x, y), (255, 0, 0))
    if changed_pixels == 0:
        return ScreenshotDiff(step_id, "PASS", False, 0, None, "pixels match")
    panel = Image.new("RGB", (width * 3, height), (32, 32, 32))
    panel.paste(baseline, (0, 0))
    panel.paste(observed, (width, 0))
    panel.paste(difference, (width * 2, 0))
    _private_directory(output_dir)
    artifact = output_dir / f"{step_id}-diff.png"
    panel.save(artifact)
    artifact.chmod(0o600)
    detail = "pixels changed"
    if baseline.size != observed.size:
        detail += f"; dimensions baseline={baseline.size}, current={observed.size}"
    return ScreenshotDiff(step_id, "FAIL", True, changed_pixels, str(artifact), detail)
