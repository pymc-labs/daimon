"""Operator-supplied configuration. Production remains disabled by default."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from qa.live.schema import MODEL, Contract

STAGING_GUILD = "1435062989119295640"
STAGING_CATEGORY = "1435062989119295641"
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


class Config(Contract):
    driver_path: str = str(Path.home() / ".config/daimon-qa/qa.py")
    model: Literal["claude-haiku-4-5-20251001"] = MODEL
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
    admin_hooks: dict[str, list[str]] = Field(default_factory=dict)
    poll_interval_s: float = Field(default=4, gt=0)
    settle_s: float = Field(default=6, gt=0)

    @model_validator(mode="after")
    def scope(self) -> Self:
        if (self.staging.guild_id, self.staging.category_id) != (STAGING_GUILD, STAGING_CATEGORY):
            raise ValueError("staging target must be the QA guild and category")
        if self.staging.project != "pymc-daimon-staging":
            raise ValueError("staging logs must use pymc-daimon-staging")
        if not set(self.prod.guild_allowlist) <= PROD_GUILDS:
            raise ValueError("production guild allow-list contains a customer guild")
        if self.prod.enabled and self.prod.guild_id not in self.prod.guild_allowlist:
            raise ValueError("production guild must be an explicitly allowed internal guild")
        return self

    def target(self, env: str) -> Target:
        if env == "staging":
            return self.staging
        if env != "prod":
            raise ValueError("unknown environment")
        if not self.prod.enabled or self.prod.guild_id not in PROD_GUILDS:
            raise ValueError("production canary is disabled")
        return self.prod


def load_config(path: Path) -> Config:
    return Config.model_validate_json(path.read_text())


def write_example(path: Path) -> None:
    config = Config(
        pricing=Pricing(
            per_turn_usd=0.25,
            judge_input_per_million=1.0,
            judge_output_per_million=5.0,
        )
    )
    path.write_text(json.dumps(config.model_dump(), indent=2) + "\n")
