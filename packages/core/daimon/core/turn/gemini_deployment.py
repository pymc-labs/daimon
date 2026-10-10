"""Explicit Gemini deployment selection; no startup credential discovery."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
)
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, SQLiteGeminiStorage
from daimon.core.turn.prepare import ProviderPreparationRequest
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
from mux.drivers.gemini.core import compile_agent, compile_environment
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.errors import ScopeViolation
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class GeminiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["gemini.inline_reuse"]
    tenant_id: Nonblank
    platform: Nonblank
    channel_id: Nonblank
    account_id: Nonblank
    config_digest: Nonblank
    project: Nonblank
    api_key_env: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    records_path: Path
    holds_path: Path
    hold_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    cap_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    agent_name: Nonblank
    system: str | None = None
    # REQUIRED: [] means no tools. Never use the provider's implicit paid tools.
    tools: tuple[ToolSpec, ...]
    environment: EnvironmentSpec


def load_gemini_deployment(request: ProviderPreparationRequest) -> GeminiPlan:
    revision = request.admission.backend_revision
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if revision is None or revision.profile != PROFILE or not path:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        with Path(path).open("rb") as manifest:
            raw = manifest.read(1_048_577)
        if len(raw) > 1_048_576:
            raise AdmissionDenied(reason="backend_unsupported")
        rows = TypeAdapter(list[dict[str, JsonValue]]).validate_json(raw)
        selected = [
            row
            for row in rows
            if row.get("profile") == PROFILE
            and row.get("tenant_id") == request.scope.tenant_id
            and row.get("account_id") == request.scope.account_id
            and row.get("platform") == revision.channel.platform
            and row.get("channel_id") == revision.channel.channel_id
            and row.get("config_digest") == revision.digest
        ]
        if len(selected) != 1:
            raise AdmissionDenied(reason="backend_unsupported")
        plan = GeminiPlan.model_validate(selected[0])
        if (
            not plan.records_path.is_absolute()
            or not plan.holds_path.is_absolute()
            or plan.records_path.resolve() == plan.holds_path.resolve()
            or plan.hold_usd > plan.cap_usd
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        return plan
    except (OSError, ValidationError):
        raise AdmissionDenied(reason="backend_unsupported") from None


def build_gemini_runtime(request: ProviderPreparationRequest) -> TurnRuntime:
    revision, scope = request.admission.backend_revision, request.scope
    if revision is None or revision.model != FLASH_PRIMARY or request.deps.state_store is None:
        raise AdmissionDenied(reason="backend_unsupported")
    plan = load_gemini_deployment(request)
    agent = AgentSpec(
        name=plan.agent_name,
        model=ModelRef(provider="gemini", id=FLASH_PRIMARY),
        system=plan.system,
        tools=plan.tools,
    )
    # Validate native policy before reading the selected credential or opening
    # persistence. Unsupported MCP, confirmations and workspace sources refuse.
    compile_agent(agent)
    compile_environment(plan.environment)
    namespace = json.dumps([scope.tenant_id, scope.account_id, plan.project], separators=(",", ":"))

    def transport(config: ConfigRevision, authorized: Scope) -> GeminiTransportRuntime:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "Gemini deployment differs from admitted turn")
        secret = os.environ.get(plan.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        from mux.drivers.gemini.deployment import configured_transport

        return GeminiTransportRuntime(configured_transport(api_key=secret))

    return TurnRuntime(
        transport_factory=transport,
        journal=GeminiJournal(
            SQLiteGeminiStorage(plan.records_path, namespace=namespace),
            request.deps.state_store,
            GeminiDeployment(
                revision.digest, revision.local, plan.project, agent, plan.environment
            ),
        ),
        usage_store=GeminiUsageRuntime(
            SQLiteFlashAccounting(
                plan.holds_path, namespace=namespace, hold_usd=plan.hold_usd, cap_usd=plan.cap_usd
            ),
            {},
        ),
    )
