"""Operator-supplied configuration. Production remains disabled by default."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from qa.live.billing import BillingSchedule, approved_rates
from qa.live.models import BackendName, ModelPolicy
from qa.live.schema import Contract

STAGING_GUILD = "1435062989119295640"
STAGING_CATEGORY = "1558361838960382032"
PROD_GUILDS = frozenset({"745261709622771773", "1533730917854609528"})


class Target(Contract):
    guild_id: str = STAGING_GUILD
    category_id: str = STAGING_CATEGORY
    daimon_id: str = "1530628070405308456"
    qa_user_ids: list[str] = Field(default_factory=lambda: ["1533049261032341668"])
    project: str = "pymc-daimon-staging"
    enabled: bool = False
    guild_allowlist: list[str] = Field(default_factory=list)
    database_env: str = "DAIMON_QA_STAGING_DATABASE_URL"
    context: dict[str, str] = Field(default_factory=dict)
    warm_url: str = "https://staging-daimon-mcp-251774259661.us-east4.run.app/readyz"
    model_probe: list[str] = Field(default_factory=list)
    deployment_probe: list[str] = Field(default_factory=list)
    billing_probe: list[str] = Field(default_factory=list)
    qa_agent_name: str | None = None
    backend: BackendName = "anthropic"


class Pricing(Contract):
    per_turn_usd: float = Field(gt=0, allow_inf_nan=False)
    judge_input_per_million: float = Field(gt=0, allow_inf_nan=False)
    judge_output_per_million: float = Field(gt=0, allow_inf_nan=False)
    judge_input_token_limit: int = Field(default=12000, gt=0)


class Alerts(Contract):
    inbox: str = "/home/clsandoval/cs/root-opus-handoff-20260923/inbox"
    command: list[str] = Field(
        default_factory=lambda: [
            "/home/clsandoval/cs/root-opus-handoff-20260923/tsend.sh",
            "%618",
        ]
    )
    cooldown_s: int = Field(default=21600, ge=21600)
    pending_threshold: int = Field(default=3, ge=3)


class Schedule(Contract):
    staging_canary_interval_s: Literal[3600] = 3600
    prod_canary_interval_s: Literal[3600] = 3600
    catalog_runs_per_day: Literal[1] = 1
    catalog_budget_usd: float = Field(default=2, gt=0, le=10, allow_inf_nan=False)


class Config(Contract):
    driver_path: str = str(Path.home() / ".config/daimon-qa/qa.py")
    models: ModelPolicy = Field(default_factory=ModelPolicy)
    billing_rates: dict[str, BillingSchedule] = Field(default_factory=approved_rates)
    pricing: Pricing
    staging: Target = Field(default_factory=Target)
    prod: Target = Field(
        default_factory=lambda: Target(
            project="pymc-daimon",
            database_env="DAIMON_QA_PROD_DATABASE_URL",
            warm_url="",
        )
    )
    alerts: Alerts = Field(default_factory=Alerts)
    live_lock: str = "~/.local/state/daimon-qa/live-run.lock"
    admin_hooks: dict[str, list[str]] = Field(default_factory=dict)
    poll_interval_s: float = Field(default=4, gt=0)
    settle_s: float = Field(default=6, gt=0)
    fallback_watch_s: float = Field(default=180, gt=0, le=180)
    log_ingestion_delay_s: float = Field(default=45, ge=30, le=60)
    delete_retry_s: float = Field(default=2, gt=0, le=10)
    orphan_after_s: float = Field(default=3600, ge=3600)
    schedule: Schedule = Field(default_factory=Schedule)

    @model_validator(mode="after")
    def scope(self) -> Self:
        if (self.staging.guild_id, self.staging.category_id) != (STAGING_GUILD, STAGING_CATEGORY):
            raise ValueError("staging target must be the QA guild and category")
        if self.staging.project != "pymc-daimon-staging":
            raise ValueError("staging logs must use pymc-daimon-staging")
        if not set(self.prod.guild_allowlist) <= PROD_GUILDS:
            raise ValueError("production guild allow-list contains a customer guild")
        if self.prod.enabled and self.prod.backend != "anthropic":
            raise ValueError("Scenario B production canaries require a Claude Haiku QA agent")
        if self.prod.enabled and self.prod.guild_id not in self.prod.guild_allowlist:
            raise ValueError("production guild must be an explicitly allowed internal guild")
        return self

    def target(self, env: str) -> Target:
        if env == "staging":
            if not self.staging.enabled:
                raise ValueError("staging QA is disabled")
            return self.staging
        if env != "prod":
            raise ValueError("unknown environment")
        if not self.prod.enabled or self.prod.guild_id not in PROD_GUILDS:
            raise ValueError("production canary is disabled")
        return self.prod

    def validate_plan(
        self, catalog_estimate: float = 0, canary_estimate: float | None = None
    ) -> None:
        canaries = (
            24
            * 2
            * (canary_estimate if canary_estimate is not None else 2 * self.pricing.per_turn_usd)
        )
        if canaries + self.schedule.catalog_budget_usd > 10:
            raise ValueError("Scenario B hourly Haiku canaries plus catalog budget exceed $10/day")
        if catalog_estimate > self.schedule.catalog_budget_usd:
            raise ValueError("catalog estimate exceeds its daily Scenario B allocation")


def load_config(path: Path) -> Config:
    return Config.model_validate_json(path.read_text())


def write_example(path: Path) -> None:
    config = Config(
        pricing=Pricing(
            per_turn_usd=0.05,
            judge_input_per_million=1.0,
            judge_output_per_million=5.0,
        )
    )
    path.write_text(json.dumps(config.model_dump(), indent=2) + "\n")
