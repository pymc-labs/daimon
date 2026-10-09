"""Turn a lead-exported content-free telemetry aggregate into pinned baseline JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HERE = Path(__file__).resolve().parent


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelBaseline(Strict):
    model: str = Field(min_length=1)
    usage_events: int = Field(ge=0)
    turns: int = Field(ge=0)
    uncached_input_tokens: int | None = Field(ge=0)
    cache_read_input_tokens: int | None = Field(ge=0)
    cache_write_input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    p50_total_ms: float | None = Field(ge=0, allow_inf_nan=False)
    p95_total_ms: float | None = Field(ge=0, allow_inf_nan=False)
    p50_first_token_ms: None
    p95_first_token_ms: None

    @model_validator(mode="after")
    def latency_coverage(self) -> ModelBaseline:
        buckets = (
            self.uncached_input_tokens,
            self.cache_read_input_tokens,
            self.cache_write_input_tokens,
            self.output_tokens,
        )
        if self.usage_events == 0 and any(v is not None for v in buckets):
            raise ValueError("unobserved usage must stay null")
        if self.usage_events > 0 and any(v is None for v in buckets):
            raise ValueError("observed legacy usage requires all token buckets")
        if self.turns == 0:
            if self.p50_total_ms is not None or self.p95_total_ms is not None:
                raise ValueError("latency without observed turns")
        elif self.p50_total_ms is None or self.p95_total_ms is None:
            raise ValueError("observed turns require total-latency percentiles")
        elif self.p50_total_ms > self.p95_total_ms:
            raise ValueError("p95 cannot be below p50")
        return self


class Telemetry(Strict):
    schema_version: Literal[1]
    provider: Literal["anthropic"]
    window_start: datetime
    window_end: datetime
    token_source: str
    latency_source: str
    first_token_status: str
    models: list[ModelBaseline]

    @model_validator(mode="after")
    def valid_window(self) -> Telemetry:
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("telemetry timestamps must have a timezone")
        if (self.window_end - self.window_start).total_seconds() != 14 * 86400:
            raise ValueError("baseline window must be exactly 14 days")
        if len({m.model for m in self.models}) != len(self.models):
            raise ValueError("duplicate model cohorts")
        return self


def convert(export: str, *, sdk_pin: str, baseline_date: date) -> dict[str, object]:
    if not re.fullmatch(
        r"anthropic==[0-9]+\.[0-9]+\.[0-9]+(?:[a-z]+[0-9]+)?(?:\+[a-zA-Z0-9.]+)?", sdk_pin
    ):
        raise ValueError("supply the exact deployed SDK pin, e.g. anthropic==0.117.0")
    telemetry = Telemetry.model_validate_json(export)
    if baseline_date != telemetry.window_end.astimezone(UTC).date():
        raise ValueError("baseline date must match the export window end in UTC")
    rows: list[dict[str, object]] = []
    for model in telemetry.models:
        buckets = (
            model.uncached_input_tokens,
            model.cache_read_input_tokens,
            model.cache_write_input_tokens,
        )
        total_input = (
            sum(v for v in buckets if v is not None)
            if all(v is not None for v in buckets)
            else None
        )
        rows.append(
            {
                **model.model_dump(),
                "total_input_tokens": total_input,
                "input_token_mix": {
                    "uncached": model.uncached_input_tokens / total_input
                    if total_input and model.uncached_input_tokens is not None
                    else None,
                    "cache_read": model.cache_read_input_tokens / total_input
                    if total_input and model.cache_read_input_tokens is not None
                    else None,
                    "cache_write": model.cache_write_input_tokens / total_input
                    if total_input and model.cache_write_input_tokens is not None
                    else None,
                },
            }
        )
    return {
        "schema_version": 1,
        "baseline_date": baseline_date.isoformat(),
        "sdk_pin": sdk_pin,
        "converted_at": datetime.now(UTC).isoformat(),
        "sql_sha256": hashlib.sha256((HERE / "telemetry.sql").read_bytes()).hexdigest(),
        "export_sha256": hashlib.sha256(export.encode()).hexdigest(),
        "telemetry": {**telemetry.model_dump(mode="json"), "models": rows},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument(
        "--sdk-pin", required=True, help="exact SDK version deployed during collection"
    )
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = convert(args.export.read_text(), sdk_pin=args.sdk_pin, baseline_date=args.date)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
