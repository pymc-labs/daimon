"""Root-approved cheap models shared by judges and deployment/turn guards."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BackendName = Literal["anthropic", "openai", "gemini"]


class BackendModels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    primary: str
    dated_snapshots: bool = False
    fallback: tuple[str, ...] = ()
    fallback_status: int | None = None
    staging_override: str | None = None
    staging_aliases: tuple[str, ...] = ()

    @field_validator("fallback", "staging_aliases", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return tuple(cast(list[object], value)) if isinstance(value, list) else value

    def matches_primary(self, model: str) -> bool:
        if model == self.primary:
            return True
        if not self.dated_snapshots or not re.fullmatch(
            re.escape(self.primary) + r"-[0-9]{8}", model
        ):
            return False
        try:
            datetime.strptime(model[-8:], "%Y%m%d")
        except ValueError:
            return False
        return True

    def accepts_daimon(self, model: str, env: str) -> bool:
        return self.matches_primary(model) or (
            env == "staging"
            and self.staging_override is not None
            and model in (self.staging_override, *self.staging_aliases)
        )

    def next_model(self, current: str, http_status: int) -> str | None:
        if self.fallback_status != http_status:
            return None
        chain = (self.primary, *self.fallback)
        if current not in chain or current == chain[-1]:
            return None
        return chain[chain.index(current) + 1]


# Change policy here only. The staging override expires on the driver's explicit
# deployment NOTE; it never admits Haiku 4.5 to a production canary or judge.
APPROVED_MODELS: dict[BackendName, BackendModels] = {
    "anthropic": BackendModels(
        primary="claude-haiku-5-5",
        dated_snapshots=True,
        staging_override="claude-haiku-4-5",
        staging_aliases=("claude-haiku-4-5-20251001",),
    ),
    "openai": BackendModels(primary="gpt-6-luna"),
    "gemini": BackendModels(
        primary="gemini-3.8-flash",
        fallback=("gemini-flash-latest", "gemini-3.5-flash-lite"),
        fallback_status=503,
    ),
}


STAGING_LEGACY_MODEL = APPROVED_MODELS["anthropic"].staging_aliases[0]


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    backends: dict[BackendName, BackendModels] = Field(
        default_factory=lambda: dict(APPROVED_MODELS)
    )

    def _require_approved(self) -> None:
        if self.backends != APPROVED_MODELS:
            raise ValueError("model map must match the root-approved cheap-model policy")

    @model_validator(mode="after")
    def approved(self) -> Self:
        self._require_approved()
        return self

    def policy(self, backend: BackendName) -> BackendModels:
        self._require_approved()
        return self.backends[backend]

    def accepts(self, backend: BackendName, model: str, env: str) -> bool:
        return self.policy(backend).accepts_daimon(model, env)
