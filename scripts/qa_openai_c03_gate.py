"""Offline admission for one separately lead-pinned C03 OptionA attempt."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

CONFIG_SHA256 = "28abe4be3fcedb3a980ee1fb1f3b9f32cff94e9339b7454abaae2f080b9c65f1"
LEDGER_ID = "091ee642b92141f88a8f3026edaa1a5a"
SPRINT = Path("/home/clsandoval/cs/daimon-neutral-core-20261009")
LANE = SPRINT / "lanes/N11-openai"
LEAD_NOTES = SPRINT / "lanes/N9-qa/LEAD-NOTES.md"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_go(path: Path):
    """One strict parse and canonical digest; whitespace does not change a pin."""

    def unique(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("duplicate GO field")
            value[name] = item
        return value

    def nonfinite(value):
        raise ValueError("nonfinite GO value")

    go = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(go, dict):
        raise ValueError("GO must be an object")
    canonical = json.dumps(go, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return go, hashlib.sha256(canonical).hexdigest()


def attempt_marker(go):
    identity = go.get("attempt_identity")
    if (
        not isinstance(identity, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", identity) is None
    ):
        raise ValueError("invalid C03 attempt identity")
    return LANE / "c03-attempts" / (identity + ".attempt")


def require_unused_attempt(go):
    """Read current markers/receipts and join legacy source-only markers to GO."""
    identity = go["attempt_identity"]
    if attempt_marker(go).exists():
        raise ValueError("C03 attempt identity already consumed")
    legacy_outputs = set()
    for pattern in ("*.attempt", "*receipt*.json"):
        for path in LANE.rglob(pattern):
            raw = path.read_text().strip()
            if raw == identity:
                raise ValueError("C03 attempt identity already consumed")
            if path.suffix == ".attempt" and re.fullmatch(r"[a-f0-9]{32}|[a-f0-9]{40}", raw):
                if path.name == "C03.attempt":
                    legacy_outputs.add(path.parent.resolve())
                continue
            try:
                evidence = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError("C03 consumption evidence unreadable") from None
            if not isinstance(evidence, dict):
                raise ValueError("C03 consumption evidence malformed")
            if evidence.get("attempt_identity") == identity or any(
                isinstance(row, dict) and row.get("attempt_identity") == identity
                for row in evidence.get("results", ())
            ):
                raise ValueError("C03 attempt identity already consumed")
            if path.name == "C03.receipt.json":
                legacy_outputs.add(path.parent.resolve())
    # Prior recipes wrote only a source SHA into C03.attempt, and their receipts
    # omitted the attempt identity. Their retained GO supplies the missing join.
    for path in LANE.rglob("*.json"):
        if "go" not in path.name.lower():
            continue
        evidence, _ = read_go(path)
        output = evidence.get("output")
        if (
            evidence.get("attempt_identity") == identity
            and isinstance(output, str)
            and Path(output).resolve() in legacy_outputs
        ):
            raise ValueError("C03 attempt identity already consumed")


def envelope_from_go(go):
    if not isinstance(go, dict):
        raise ValueError("C03 requires GO-derived financial pins")
    attempt_marker(go)
    path, expected = go.get("envelope_path"), go.get("envelope_sha256")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or not isinstance(expected, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected) is None
    ):
        raise ValueError("C03 requires GO-derived financial pins")
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("C03 financial envelope differs from lead signature")
    approval = json.loads(raw)
    if approval.get("attempt_identity") != go["attempt_identity"]:
        raise ValueError("C03 envelope attempt differs from GO")
    return Path(path), approval


def validate(args):
    """Admit exact canonical lead authority before DB/guard/key access."""
    if getattr(args, "runner_pin", None) is None:
        raise ValueError("C03 requires its reviewed runner pin and separate lead GO pin")
    path, expected = getattr(args, "go_file", None), getattr(args, "go_sha256", None)
    if (
        path is None
        or not isinstance(expected, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected) is None
    ):
        raise ValueError("C03 requires the exact separate lead GO pin")
    go, canonical = read_go(path)
    if canonical != expected:
        raise ValueError("C03 requires the exact separate lead GO pin")
    if not any(
        re.match(r"^LEAD PIN\b", line)
        and re.search(r"(?<![a-f0-9])" + canonical + r"(?![a-f0-9])", line)
        for line in LEAD_NOTES.read_text().splitlines()
    ):
        raise ValueError("C03 canonical GO has no LEAD PIN")
    attempt_marker(go)
    require_unused_attempt(go)
    if (
        go.get("fixture_id") != "C03"
        or go.get("model") != "gpt-6-luna"
        or go.get("profile") != "openai.conversation_only"
        or go.get("integration_sha") != args.integration_sha
        or go.get("output") != str(args.output.resolve())
        or go.get("ledger_id") != LEDGER_ID
        or go.get("config_sha256") != CONFIG_SHA256
        or digest(args.budget_config) != CONFIG_SHA256
        or go.get("runner_pin_sha256") != digest(args.runner_pin)
    ):
        raise ValueError("C03 GO scope, source, budget or runner pin differs")
    _, approval = envelope_from_go(go)
    require_option_a_config(args.budget_config, go)
    now = datetime.now(UTC)
    require_envelope_unexpired(approval, now=now)
    expiry = datetime.fromisoformat(go["expires_at"])
    if (
        expiry.tzinfo is None
        or now >= expiry
        or expiry > datetime.fromisoformat(approval["expires_at"])
    ):
        raise ValueError("C03 GO expired or exceeds envelope expiry")
    binding_path = Path(go["binding_path"])
    raw_binding = binding_path.read_bytes()
    if hashlib.sha256(raw_binding).hexdigest() != go.get("binding_sha256"):
        raise ValueError("C03 scoped binding differs from lead pin")
    if binding_path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("C03 binding must have mode 0600")
    binding = json.loads(raw_binding)
    from uuid import UUID

    UUID(binding["tenant_id"])
    UUID(binding["account_id"])
    if (
        binding.get("provider") != "openai"
        or binding.get("profile") != "openai.conversation_only"
        or binding.get("principal_id") != "n11-cert-caller"
        or binding.get("authorization_id") != "lead-go"
        or binding.get("generation") != 1
        or binding.get("config_revision") != 1
        or binding.get("credential_file") != approval["credential_file"]
        or binding.get("attempt_identity") != go["attempt_identity"]
        or not isinstance(binding.get("invocation"), str)
        or not binding["invocation"].startswith("n11:cert:")
    ):
        raise ValueError("C03 binding scope differs from admitted attempt")
    args.c03_binding = binding
    args.c03_go = go
    return binding


def verify_harness(checkout: Path, lane: Path):
    """Freeze the lane-only recipe in the reviewed repository head."""
    manifest = checkout / "packages/mux/tests/drivers/openai/c03_runner_manifest.json"
    pinned = json.loads(manifest.read_text())
    required = {
        "live_pending.py",
        "live_smoke.py",
        "live_c03.sh",
        "prepare_live_c03.py",
        "offline_live_pending_test.py",
        "offline_live_c03_test.py",
    }
    if set(pinned["files"]) != required:
        raise ValueError("C03 reviewed recipe omits a changed dependency")
    for name, expected in pinned["files"].items():
        if digest(lane / name) != expected:
            raise ValueError("C03 lane recipe differs from reviewed commit")


def option_a_plan(go):
    """The signed three-request envelope replaces C03's old container/cutoff plan."""
    from decimal import Decimal

    from mux.testing.conformance.budget import ProbePlan, TokenLimits

    _, envelope = envelope_from_go(go)
    require_envelope_unexpired(envelope)
    plan = ProbePlan.model_validate(envelope["probe_plan"])
    if (
        envelope.get("provider") != "openai"
        or envelope.get("model") != "gpt-6-luna"
        or envelope.get("profile") != "openai.conversation_only"
        or envelope.get("attempts_per_case") != 1
        or envelope.get("sessions_authorized") != 1
        or envelope.get("sdk_max_retries") != 0
        or envelope.get("case_deadline_seconds") != 300
        or envelope.get("cleanup_deadline_seconds_total") != 60
        or envelope.get("credential_file") != "/home/clsandoval/.config/daimon-nc/openai.env"
        or envelope.get("minimum_hold_usd") != "1.0755"
        or envelope.get("maximum_hold_usd") != "1.0755"
        or envelope.get("maximum_model_requests") != 3
        or envelope.get("initial_create_turns") != 1
        or envelope.get("distinct_C03_user_input_POSTs_authorized") != 2
        or envelope.get("container_allowance") is not None
        or envelope.get("native_session_configuration_required")
        != {
            "environment": {"type": "none"},
            "agent": {
                "model": "gpt-6-luna",
                "tools": [],
                "service_tier": "default",
                "multi_agent": {"enabled": False},
            },
        }
        or plan.fixture_id != "C03"
        or plan.provider != "openai"
        or plan.model != "gpt-6-luna"
        or plan.container_allowance is not None
        or plan.limits != TokenLimits(input_tokens=3150000, output_tokens=384000)
        or envelope["guard_policy"]["expected_config_sha256"] != CONFIG_SHA256
        or envelope["guard_policy"]["expected_ledger_id"] != LEDGER_ID
        or Decimal(envelope["guard_policy"]["minimum_hold_usd"]) != Decimal("1.0755")
        or Decimal(envelope["guard_policy"]["maximum_hold_usd"]) != Decimal("1.0755")
    ):
        raise ValueError("C03 OptionA signed execution or hold terms differ")
    return plan


def require_option_a_config(config_path: Path, go):
    """No credential/ledger access: bind config bytes and exact N=3 reservation."""
    from decimal import Decimal

    from mux.testing.conformance.budget import BudgetConfig

    if digest(config_path) != CONFIG_SHA256:
        raise ValueError("C03 OptionA config differs from lead signature")
    plan = option_a_plan(go)
    config = BudgetConfig.load(config_path)
    price = config.providers["openai"].models["gpt-6-luna"]
    if (
        config.providers["openai"].cap_usd != Decimal("50")
        or price.actual_input_limit is not None
        or price.reserve(plan.limits) != Decimal("1.0755")
    ):
        raise ValueError("C03 OptionA config differs from three-request hold bound")
    return plan


def require_envelope_unexpired(envelope, *, now=None):
    expiry = datetime.fromisoformat(envelope["expires_at"])
    if expiry.tzinfo is None or (now if now is not None else datetime.now(UTC)) >= expiry:
        raise ValueError("C03 financial envelope expired or lacks an aware expiry")
