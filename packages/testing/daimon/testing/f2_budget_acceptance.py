"""Signed-only, one-session budget/tool acceptance probe: never a user message.

Creating/retrieving/archiving establishes acceptance, not empirical enforcement
or a promise of zero billed runtime. A separate probe envelope retains unknown
runtime for N8 reconciliation; it never consumes the F2 workload authorization.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from daimon.testing.a7_anthropic import source_pin
from daimon.testing.anthropic_live import (
    HOST_CONFIG_SHA256,
    LEDGER_ID,
    MODEL,
    Admission,
    LiveBinding,
    LiveContext,
    LiveRefused,
    digest_json,
    refuse_ambient,
    refuse_late_usage,
    run_live,
)
from daimon.testing.f2_native_bounds import (
    install_f2_bounds,
    native_budget,
    native_tools,
    verify_tools,
)
from mux.contracts.resources import SessionSpec
from mux.testing.conformance.budget import ContainerAllowance, ProbePlan


@dataclass(frozen=True)
class AcceptanceAdmission(Admission):
    probe_hold: Decimal = Decimal(".02")

    @property
    def hold(self) -> Decimal:
        return self.probe_hold

    @property
    def plan(self) -> ProbePlan:
        return ProbePlan.create_only(
            provider="anthropic",
            model=MODEL,
            fixture_id="F1",
            container_allowance=ContainerAllowance(
                meter="agent_session", memory_gb=None, sessions=1, seconds_per_session=60
            ),
        )


class AcceptanceTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        allowed = (
            request.method == "GET" and path.startswith(("/v1/agents/", "/v1/sessions/"))
        ) or (request.method == "POST" and (path == "/v1/sessions" or path.endswith("/archive")))
        if not allowed or "/events" in path or "/threads" in path:
            raise LiveRefused("acceptance probe forbids user messages and model work")
        return await super().handle_async_request(request)


async def acceptance(context: LiveContext) -> Mapping[str, object]:
    binding = context.admission.binding
    if binding.agent is None:
        raise LiveRefused("probe requires an explicitly authorized existing disposable F2 agent")
    install_f2_bounds(context.client)
    agent = await context.driver.agents.retrieve(binding.scope, binding.agent)
    if agent.spec.model.id != MODEL or not isinstance(agent.native, dict):
        raise LiveRefused("probe agent served model is not the exact Haiku5.5 binding")
    verify_tools(agent.native)
    session = await context.driver.sessions.create(
        binding.scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=binding.environment,
            config_revision=0,
        ),
        key="f2-budget-acceptance",
    )
    # BudgetedSessions has already checked both create ACK and independent GET,
    # including the full effective tools, before this archive.
    await context.driver.session_admin.archive(binding.scope, session.ref, key="probe-archive")
    if context.transport.user_sends != 0 or len(context.transport.session_ids) != 1:
        raise LiveRefused("acceptance probe exceeded its zero-message one-session envelope")
    return {
        "status": "pass",
        "native_budget_acknowledged": native_budget(),
        "effective_toolset_verified": True,
        "user_messages": 0,
        "model_tokens_requested": 0,
        "enforcement_tested": False,
        "billed_runtime_verified": False,
    }


def admit_probe(args: argparse.Namespace) -> AcceptanceAdmission:
    refuse_ambient()
    refuse_late_usage(args.spend_ledger)
    source = source_pin()
    go = json.loads(args.go_receipt.read_text())
    approval = json.loads(args.authorization.read_text())
    if (
        digest_json(go) != args.go_sha256
        or digest_json(approval) != args.authorization_sha256
        or args.authorization_sha256
        != "8628b05e1b6d637b152c138a0977dc806e40833e9fc6e1d54794881a9ae063a3"
    ):
        raise LiveRefused("probe requires independently lead-pinned GO and financial approval")
    expected = {
        "live_go": True,
        "purpose": "F2-native-budget-acceptance",
        "provider": "anthropic",
        "model": MODEL,
        "native_budget": native_budget(),
        "native_tools": native_tools(),
        "maximum_user_messages": 0,
        "maximum_session_creates": 1,
        "wall_clock_deadline_seconds_total": 60,
        "config_sha256": HOST_CONFIG_SHA256,
        "ledger_id": LEDGER_ID,
        "merged_source_head": source["head"],
        "binding_sha256": hashlib.sha256(args.live_binding.read_bytes()).hexdigest(),
        "approval_digest": args.authorization_sha256,
    }
    if source["clean"] is not True or any(go.get(k) != v for k, v in expected.items()):
        raise LiveRefused("probe GO scope/source/bindings are stale or unsupported")
    ancestor = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            str(source["head"]),
            "origin/integration/neutral-core",
        ],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        check=False,
    )
    expires = datetime.fromisoformat(str(approval.get("expires_at")).replace("Z", "+00:00"))
    hold = Decimal(str(approval.get("maximum_usd")))
    if (
        ancestor.returncode != 0
        or expires.tzinfo is None
        or datetime.now(UTC) >= expires
        or hold != Decimal(".02")
        or approval.get("purpose") != expected["purpose"]
        or approval.get("provider") != "anthropic"
        or approval.get("model") != MODEL
        or approval.get("config_sha256") != HOST_CONFIG_SHA256
        or approval.get("ledger_id") != LEDGER_ID
        or approval.get("credential_file") != str(Path.home() / ".config/daimon-nc/anthropic.env")
        or Path(str(approval.get("ledger_path"))).resolve() != args.spend_ledger.resolve()
        or hashlib.sha256(args.budget_config.read_bytes()).hexdigest() != HOST_CONFIG_SHA256
    ):
        raise LiveRefused("probe financial approval is absent, expired or outside the signed route")
    for field in ("install_receipt", "recipe"):
        if hashlib.sha256(Path(str(go.get(field + "_path"))).read_bytes()).hexdigest() != go.get(
            field + "_sha256"
        ):
            raise LiveRefused("probe installation or recipe evidence changed")
    binding = LiveBinding.model_validate_json(args.live_binding.read_text())
    binding.verify("F2-Anthropic", f2_session_binding=False)
    if binding.agent is None or args.output.exists():
        raise LiveRefused("probe needs an owned existing F2 agent and fresh output")
    return AcceptanceAdmission(
        "F2-Anthropic",
        approval,
        args.authorization_sha256,
        args.budget_config,
        args.spend_ledger,
        binding,
        args.output,
        60,
        str(source["head"]),
        hold,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "authorization",
        "go-receipt",
        "live-binding",
        "budget-config",
        "spend-ledger",
        "output",
    ):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--go-sha256", required=True)
    args = parser.parse_args()
    admission = admit_probe(args)  # No credential or request before admission/reservation.
    asyncio.run(
        run_live(
            admission,
            acceptance,
            transport_factory=lambda: AcceptanceTransport(trust_env=False, retries=0),
        )
    )


if __name__ == "__main__":
    main()
