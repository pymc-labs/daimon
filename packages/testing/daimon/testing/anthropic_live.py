"""One-attempt Anthropic probe admission. No effect until explicit live selection.

The financial hold and streamed stop are controls, never provider token caps.
Native usage lacking billed runtime remains incomplete for N8 reconciliation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal, Never, cast

import httpx
from anthropic import AsyncAnthropic, AsyncStream
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic import (
    AnthropicManagedAgents,
    ResourceAuthorization,
    Vaults,
    observation_from_event,
)
from mux.errors import ScopeViolation
from mux.testing.conformance.budget import (
    ActualSpend,
    BudgetConfig,
    BudgetGuard,
    ContainerAllowance,
    ModelPrice,
    ProbePlan,
    Reservation,
    SessionPrice,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)
from mux.testing.conformance.recording import Audit, RecordingError
from mux.testing.conformance.usage_settlement import (
    AnthropicSessionSpend,
    actual_from_anthropic_sessions,
)
from mux.testing.isolated_sdk import TLS_PROXY_ENV_VARS, build_async_anthropic
from pydantic import BaseModel, ConfigDict, JsonValue

MODEL = "claude-haiku-5-5"
HOST_CONFIG_SHA256 = "36c06fdfe606ba07f42f6f5866cbc68f13b25387fc9aceb62f256ffbabc247e9"
LEDGER_ID = "091ee642b92141f88a8f3026edaa1a5a"
Package = Literal["A7", "F2-Anthropic"]


class LiveRefused(ValueError):
    """Constant, credential-free diagnostics only."""


class LiveStopped(RuntimeError):
    """A stream stop control fired; no replay or new session is allowed."""


class LiveBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Scope
    account_scope_id: str
    environment: ResourceRef
    agent: ResourceRef | None = None
    database_url: str | None = None
    mcp_url: str | None = None
    mcp_bearer_file: Path | None = None
    mcp_signing_secret_file: Path | None = None

    def verify(self, package: Package, *, f2_session_binding: bool = True) -> None:
        if self.scope.is_platform or self.scope.is_legacy_host_authorized:
            raise LiveRefused("live probes require an authorized tenant/account scope")
        for ref, kind in ((self.environment, "environment"), (self.agent, "agent")):
            if ref is None:
                continue
            if (
                ref.provider != "anthropic"
                or ref.kind != kind
                or ref.account_scope_id != self.account_scope_id
                or ref.tenant_id != self.scope.tenant_id
                or ref.account_id != self.scope.account_id
            ):
                raise LiveRefused("live resource binding is outside the admitted scope")
        if package == "A7" and (self.agent is None or self.database_url is None):
            raise LiveRefused("A7 requires an owned DB and authorized disposable agent")
        if package == "F2-Anthropic":
            if not self.mcp_url:
                raise LiveRefused("F2 requires a provider-reachable owned MCP binding")
            if f2_session_binding:
                if not self.database_url or not self.mcp_signing_secret_file:
                    raise LiveRefused("F2 requires DB/signing authority for after-ACK MCP binding")
            elif not self.mcp_bearer_file:
                raise LiveRefused("F2 acceptance requires its owned bootstrap MCP bearer")
            url = httpx.URL(self.mcp_url)
            if (
                url.scheme != "https"
                or not url.host
                or url.host.endswith(".invalid")
                or url.host in {"localhost", "127.0.0.1", "::1"}
                or url.userinfo
                or url.query
            ):
                raise LiveRefused("F2 requires an explicit public HTTPS MCP destination")


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def explicit_secret(path: Path, *, env_file: bool = False) -> str:
    """Read only after admission, without sourcing or consulting environment values."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16384
        ):
            raise LiveRefused("credential file must be owned, regular and mode 0600")
        data = os.read(descriptor, 16385).decode()
    finally:
        os.close(descriptor)
    if env_file:
        lines = [
            line.strip()
            for line in data.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(lines) != 1 or not lines[0].startswith("ANTHROPIC_API_KEY="):
            raise LiveRefused(
                "credential file must contain only an explicit Anthropic key assignment"
            )
        data = lines[0].split("=", 1)[1].strip().strip("\"'")
    value = data.strip()
    if not value or any(c.isspace() for c in value):
        raise LiveRefused("explicit credential is missing or malformed")
    return value


def refuse_ambient() -> None:
    if any(
        name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        or name.upper() in TLS_PROXY_ENV_VARS
        for name in os.environ
    ):
        raise LiveRefused("live entry refuses ambient provider, TLS and proxy configuration")


@dataclass(frozen=True)
class Admission:
    package: Package
    approval: Mapping[str, object]
    approval_digest: str
    config_path: Path
    ledger_path: Path
    binding: LiveBinding
    output: Path
    deadline_s: int
    source_head: str

    @property
    def hold(self) -> Decimal:
        return Decimal(".35" if self.package == "A7" else ".65")

    @property
    def plan(self) -> ProbePlan:
        return ProbePlan(
            provider="anthropic",
            model=MODEL,
            fixture_id="A7" if self.package == "A7" else "F1",
            limits=TokenLimits(
                input_tokens=200000 if self.package == "A7" else 500000,
                output_tokens=40000 if self.package == "A7" else 32000,
            ),
            container_allowance=ContainerAllowance(
                meter="agent_session",
                memory_gb=None,
                sessions=2 if self.package == "A7" else 1,
                seconds_per_session=self.deadline_s,
            ),
        )


def late_usage_prefix(ledger: Path) -> str:
    return ".n4-late-usage-" + hashlib.sha256(str(ledger.resolve()).encode()).hexdigest() + "-"


def refuse_late_usage(ledger: Path) -> None:
    # Never trust a caller-edited boolean to clear a money-safety latch. N8 moves
    # reconciled evidence out of this namespace after the signed reconciliation.
    if any(ledger.parent.glob(late_usage_prefix(ledger) + "*.json")):
        raise LiveRefused("unreconciled late usage blocks this ledger for A7 and F2")


def admit(
    *,
    package: Package,
    approval_path: Path,
    go_path: Path,
    go_digest: str,
    config_path: Path,
    ledger_path: Path,
    binding_path: Path,
    output: Path,
    source_head: str,
    source_clean: bool,
    now: datetime | None = None,
) -> Admission:
    """A separately lead-pinned GO binds exact approval/install/source/recipe bytes."""
    refuse_ambient()
    refuse_late_usage(ledger_path)
    go = json.loads(go_path.read_text())
    document = json.loads(approval_path.read_text())
    approval = document.get("approval", document)
    signed = {
        "A7": "5dc7a801d7566e63e91fd9e30f2c667db613166ab27eedd1d629af616950b9a1",
        "F2-Anthropic": "6103a70419f4d1260c97eb0f3be60a3e238701cd9de7ae5ccd802f894b1b6a18",
    }
    if digest_json(go) != go_digest or go.get("live_go") is not True:
        raise LiveRefused("separate lead GO receipt is absent or changed")
    if digest_json(approval) != signed[package]:
        raise LiveRefused("financial envelope is not the exact lead-signed replacement")
    if go.get("approval_digest") != digest_json(approval):
        raise LiveRefused("financial authorization changed after signature")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_head, "origin/integration/neutral-core"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        raise LiveRefused("live entry has not merged into integration")
    current = now or datetime.now(UTC)
    expires = datetime.fromisoformat(str(approval["expires_at"]).replace("Z", "+00:00"))
    if expires.tzinfo is None or current >= expires:
        raise LiveRefused("financial authorization expired")
    expected = {
        "package": package,
        "provider": "anthropic",
        "model": MODEL,
        "config_sha256": HOST_CONFIG_SHA256,
        "ledger_id": LEDGER_ID,
        "maximum_usd": ".35" if package == "A7" else ".65",
        "sessions": 2 if package == "A7" else 1,
        "native_budget_sent": package == "F2-Anthropic",
        "native_budget_must_be_acknowledged": package == "F2-Anthropic",
    }
    if package == "F2-Anthropic":
        from daimon.testing.f2_native_bounds import native_budget, native_tools

        # Both independently pinned financial and GO documents bind these
        # exact native controls. Guard-only authorizations remain refused.
        if go.get("native_budget") != native_budget() or go.get("native_tools") != native_tools():
            raise LiveRefused("F2 GO lacks the exact native budget and tool allowlist")
    if any(approval.get(key) != value for key, value in expected.items()):
        raise LiveRefused("financial authorization does not match the admitted probe")
    if (
        not source_clean
        or go.get("merged_source_head") != source_head
        or go.get("package") != package
        or go.get("model") != MODEL
        or go.get("binding_sha256") != hashlib.sha256(binding_path.read_bytes()).hexdigest()
        or go.get("config_sha256") != HOST_CONFIG_SHA256
        or go.get("ledger_id") != LEDGER_ID
        or Path(str(approval.get("ledger_path"))).resolve() != ledger_path.resolve()
    ):
        raise LiveRefused("merged source, binding or installed ledger pin is missing or stale")
    install = Path(str(go.get("install_receipt_path")))
    recipe = Path(str(go.get("recipe_path")))
    if (
        hashlib.sha256(install.read_bytes()).hexdigest() != go.get("install_receipt_sha256")
        or hashlib.sha256(recipe.read_bytes()).hexdigest() != go.get("recipe_sha256")
        or hashlib.sha256(config_path.read_bytes()).hexdigest() != HOST_CONFIG_SHA256
    ):
        raise LiveRefused("installed configuration or reviewed recipe receipt changed")
    deadline = 300 if package == "A7" else 600
    if (
        approval.get("wall_clock_deadline_seconds_total") != deadline
        or approval.get("streamed_usage_monitor", {}).get("threshold_fraction_of_hold") != "0.8"
    ):
        raise LiveRefused("reviewed deadline and streamed-cost controls are missing")
    rates = json.loads(config_path.read_text())["providers"]["anthropic"]["models"][MODEL]
    if digest_json(rates) != "0403ed42c37c8df954e20756c5cc1cf9f5446a2fea90aa721ef2ae87431911b8":
        raise LiveRefused("dated Haiku5.5 pricing changed after signature")
    binding = LiveBinding.model_validate_json(binding_path.read_text())
    binding.verify(package)
    if output.exists():
        raise LiveRefused("preserve previous evidence; output must be fresh")
    return Admission(
        package,
        approval,
        digest_json(approval),
        config_path,
        ledger_path,
        binding,
        output,
        deadline,
        source_head,
    )


@dataclass(frozen=True)
class OwnedAuthorization(ResourceAuthorization):
    created: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])

    def check(self, scope: Scope, kind: str, resource_id: str | None = None) -> None:
        if scope != self.scope:
            raise ScopeViolation(kind, "live scope differs from admission")
        if resource_id is not None and (kind, resource_id) in self.created:
            return
        super().check(scope, kind, resource_id)


class LiveRequestMetadata(BaseModel):
    """Closed routes and separately audited identities; no opaque URL blobs."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["GET", "POST", "DELETE"]
    route: Literal[
        "/v1/agents",
        "/v1/sessions",
        "/v1/skills",
        "/v1/vaults",
        "/v1/agents/{resource_id}",
        "/v1/sessions/{resource_id}",
        "/v1/skills/{resource_id}",
        "/v1/vaults/{resource_id}",
        "/v1/agents/{resource_id}/archive",
        "/v1/sessions/{resource_id}/archive",
        "/v1/vaults/{resource_id}/archive",
        "/v1/sessions/{resource_id}/events",
        "/v1/sessions/{resource_id}/events/stream",
        "/v1/sessions/{resource_id}/threads/{thread_id}/stream",
        "/v1/vaults/{resource_id}/credentials",
        "/v1/skills/{resource_id}/versions",
        "/v1/skills/{resource_id}/versions/{version}/content",
    ]
    resource_id: str | None = None
    version: str | None = None
    thread_id: str | None = None


def request_metadata(
    requests: list[tuple[str, str]], resources: frozenset[tuple[str, str]]
) -> list[dict[str, JsonValue]]:
    """Project only known routes and already authorized/accepted parent IDs."""
    result: list[dict[str, JsonValue]] = []
    kinds = {"agents": "agent", "sessions": "session", "skills": "skill", "vaults": "vault"}
    for method, path in requests:
        parts = path.split("/")
        if (
            method not in {"GET", "POST", "DELETE"}
            or len(parts) < 3
            or parts[:2] != ["", "v1"]
            or parts[2] not in kinds
            or any(not part or any(c in part for c in "?#%\\\r\n") for part in parts[1:])
        ):
            raise RecordingError("unknown live request route; export refused")
        collection = parts[2]
        resource_id = version = thread_id = None
        route = f"/v1/{collection}"
        if len(parts) == 3:
            if method not in {"GET", "POST"}:
                raise RecordingError("unknown live request route; export refused")
        else:
            resource_id = parts[3]
            if (kinds[collection], resource_id) not in resources:
                raise RecordingError("unaccepted live request identity; export refused")
            route += "/{resource_id}"
            tail = tuple(parts[4:])
            allowed = (
                not tail
                and (method == "GET" or collection == "agents" and method == "POST")
                or not tail
                and collection == "skills"
                and method == "DELETE"
                or tail == ("archive",)
                and collection in {"agents", "sessions", "vaults"}
                and method == "POST"
                or tail == ("events",)
                and collection == "sessions"
                and method in {"GET", "POST"}
                or tail == ("events", "stream")
                and collection == "sessions"
                and method == "GET"
                or tail == ("credentials",)
                and collection == "vaults"
                and method in {"GET", "POST"}
                or tail == ("versions",)
                and collection == "skills"
                and method in {"GET", "POST"}
            )
            if (
                len(tail) == 3
                and tail[0] == "threads"
                and tail[2] == "stream"
                and collection == "sessions"
                and method == "GET"
            ):
                thread_id = tail[1]
                route += "/threads/{thread_id}/stream"
                allowed = True
            elif (
                len(tail) == 3
                and tail[0] == "versions"
                and tail[2] == "content"
                and collection == "skills"
                and method == "GET"
            ):
                version = tail[1]
                route += "/versions/{version}/content"
                allowed = True
            else:
                route += "".join("/" + segment for segment in tail)
            if not allowed:
                raise RecordingError("unknown live request route; export refused")
        projected = LiveRequestMetadata.model_validate(
            {
                "method": method,
                "route": route,
                "resource_id": resource_id,
                "version": version,
                "thread_id": thread_id,
            }
        )
        record = projected.model_dump(mode="json")
        if thread_id is None:
            record.pop("thread_id")
        result.append(record)
    return result


def monitor_token_estimate(price: ModelPrice, tokens: TokenUsage, at: datetime) -> Decimal | None:
    """Unknown write TTL uses the highest pinned rate only for the stop estimate."""
    exact = price.actual(tokens, at)
    if exact is not None:
        return exact
    if (
        at.tzinfo is None
        or price.effective_from is None
        or price.source is None
        or at.astimezone(UTC).date() < price.effective_from
        or price.effective_until is not None
        and at.astimezone(UTC).date() >= price.effective_until
        or price.actual_input_limit is not None
        and tokens.minimum_input_tokens > price.actual_input_limit
        or any(
            value is None
            for value in (
                tokens.input_tokens,
                tokens.output_tokens,
                tokens.input_cached_tokens,
                tokens.input_cache_write_tokens,
            )
        )
        or not tokens.input_cache_write_tokens
        or tokens.input_cache_write_5m_tokens is not None
        and tokens.input_cache_write_1h_tokens is not None
    ):
        return None
    rate = (
        price.short_prompt
        if price.short_prompt is not None
        and cast(int, tokens.input_tokens) <= price.short_prompt.through_input_tokens
        else price
    )
    with localcontext() as context:
        context.prec = 80
        return (
            (
                cast(int, tokens.input_tokens)
                - cast(int, tokens.input_cached_tokens)
                - tokens.input_cache_write_tokens
            )
            * rate.input
            + cast(int, tokens.input_cached_tokens) * rate.cached_input
            + tokens.input_cache_write_tokens * price.cache_write_input
            + cast(int, tokens.output_tokens) * rate.output
        ) / Decimal(1000000)


class LateUsageEvidenceAudit(Audit):
    """Bounded per-run audit of the complete set of durable late records."""

    def __init__(
        self, secrets: tuple[str, ...], opaque_hashes: frozenset[str] = frozenset()
    ) -> None:
        super().__init__(secrets)
        self.opaque_hashes = opaque_hashes

    def canonical_text(
        self, value: str, depth: int = 0, *, digests: frozenset[str] = frozenset()
    ) -> str:
        # These exact host-owned pins are opaque identities, never encoded text.
        # Raw/literal scanning still includes them and every other durable field.
        if value in (HOST_CONFIG_SHA256, LEDGER_ID) or value in self.opaque_hashes:
            return value
        return super().canonical_text(value, depth, digests=digests)


def audit_late_usage_records(
    records: list[JsonValue], secrets: tuple[str, ...], opaque_hashes: frozenset[str] = frozenset()
) -> None:
    """Audit full concatenation/field streams, not only the newest native meter."""
    LateUsageEvidenceAudit(secrets, opaque_hashes).audit(records)


@dataclass
class DurableNativeIds:
    """Private run-local mapping; raw native identities never leave this object.

    Hash even shape-valid IDs: alphanumeric credential fragments can otherwise
    masquerade as two distinct valid native IDs. Shape and duplicate flags remain
    available to reconciliation without preserving the provider's text.
    """

    seen: set[str] = field(default_factory=set[str])
    hashes: set[str] = field(default_factory=set[str])

    def project(self, id_: str) -> dict[str, JsonValue]:
        digest = hashlib.sha256(id_.encode()).hexdigest()
        result: dict[str, JsonValue] = {
            "sha256": digest,
            "shape_rejected": re.fullmatch(r"(?:sevt|sesn)_[A-Za-z0-9]{20,40}", id_) is None,
            "duplicate_rejected": id_ in self.seen,
        }
        self.seen.add(id_)
        self.hashes.add(digest)
        return result


def durable_receipt(
    facts: dict[str, object], identities: DurableNativeIds, secrets: tuple[str, ...]
) -> dict[str, JsonValue]:
    """Display projection only; never changes the guard's canonical settlement.

    The runner owns receipt keys and status vocabulary. Provider IDs, result
    text/evidence, and native-meter text are replaced with closed hash records.
    """
    raw: dict[str, JsonValue] = json.loads(json.dumps(facts))
    # Preserve full-value credential refusals before closing the free-text slots.
    # IDs are separately projected, so orphan cleanup can still retire a native
    # resource whose returned identifier is malformed or credential-bearing.
    Audit(secrets).audit(raw.get("result"))
    Audit(secrets).audit(raw.get("observed_request_meters"))
    flags: list[JsonValue] = []
    result_fields = {
        "status",
        "reason",
        "legs",
        "path",
        "termination",
        "session_id",
        "final_text",
        "elapsed_ms",
        "journal_records",
        "quiet_host_latency_certificate",
        "full_33_recipe_certificate",
        "evidence",
        "user_messages",
        "model_tokens_requested",
        "enforcement_tested",
    }
    meter_fields = {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
        "speed",
    }

    def visit(value: JsonValue, path: tuple[str, ...] = ()) -> JsonValue:
        if isinstance(value, dict):
            closed: dict[str, JsonValue] = {}
            for index, (key, item) in enumerate(value.items()):
                fields = (
                    result_fields
                    if path and path[0] == "result"
                    else (
                        meter_fields
                        if "native_meter" in path or "observed_request_meters" in path
                        else None
                    )
                )
                if fields is not None and key not in fields:
                    safe_key = f"rejected_field_{index}"
                    projected_key = identities.project(key)
                    closed[safe_key] = {
                        "key_sha256": projected_key["sha256"],
                        "text_rejected": True,
                        "value": visit(item, (*path, safe_key)),
                    }
                else:
                    closed[key] = visit(item, (*path, key))
            return closed
        if isinstance(value, list):
            return [visit(item, (*path, str(index))) for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        key = path[-1] if path else ""
        native_id = key in {"id", "session_id", "resource_id", "thread_id", "native_request_id"}
        native_id |= bool(
            path
            and path[0]
            in {
                "created_resources",
                "orphan_resources",
                "archived_sessions",
                "conflicting_request_evidence",
                "quarantined_native_request_ids",
                "observed_token_threshold_crossings",
            }
            and key.isdecimal()
            and not (path[0] in {"created_resources", "orphan_resources"} and key == "0")
        )
        native_id |= bool(path and path[0] == "requests" and key == "version")
        if native_id:
            projected = identities.project(value)
            flags.append({"path": ".".join(path), **projected})
            return projected["sha256"]
        host_enum = (
            key == "status"
            and value in {"pass", "fail", "failed", "pending", "PENDING", "cancelled"}
            or key == "path"
            and value in {"legacy", "mux"}
            or key == "termination"
            and value in {"completed", "cancelled", "errored", "interrupted"}
        )
        free_text = bool(path and path[0] == "result" and not host_enum)
        free_text |= "native_meter" in path or "observed_request_meters" in path
        if free_text and value != "standard":
            projected = identities.project(value)
            return {"sha256": projected["sha256"], "text_rejected": True}
        return value

    projected = visit(raw)
    assert isinstance(projected, dict)
    projected["native_id_projection"] = flags
    LateUsageEvidenceAudit(secrets, frozenset(identities.hashes)).audit(projected)
    return projected


@dataclass
class UsageMonitor:
    price: ModelPrice
    session_price: SessionPrice
    threshold: Decimal
    started: float = field(default_factory=time.monotonic)
    observations: dict[tuple[str, str], dict[str, JsonValue]] = field(
        default_factory=dict[tuple[str, str], dict[str, JsonValue]]
    )
    observed_thresholds: list[str] = field(default_factory=list[str])
    stop_reason: str | None = None
    cost: Decimal | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    observed_times: dict[tuple[str, str], datetime] = field(
        default_factory=dict[tuple[str, str], datetime]
    )
    quarantined_ids: set[str] = field(default_factory=set[str])
    verified_models: dict[str, str] = field(default_factory=dict[str, str])
    threads: dict[str, str] = field(default_factory=dict[str, str])
    settled: bool = False
    on_late_usage: Callable[[Mapping[str, JsonValue], str], None] | None = None

    def threshold_receipts(self) -> list[dict[str, JsonValue]]:
        facts: list[dict[str, JsonValue]] = []
        for identity, raw in self.observations.items():
            session, id_ = identity
            try:
                obs = observation_from_event(
                    raw,
                    ResourceRef(
                        provider="anthropic", account_scope_id="monitor", kind="session", id=session
                    ),
                    observed_at=self.observed_times[identity],
                    model_id=MODEL,
                )
                input_, output_ = obs.input_tokens, obs.output_tokens
            except (ValueError, KeyError, TypeError):
                input_ = output_ = None
            facts.append(
                {
                    "id": id_,
                    "session_id": session,
                    "served_model_verified": self.verified_models.get(session) == MODEL,
                    "observed_at": self.observed_times[identity].isoformat(),
                    "inclusive_input_tokens": input_,
                    "output_tokens": output_,
                    "input_threshold_status": "not_observed"
                    if input_ is None
                    else "exceeded"
                    if input_ > 100000
                    else "observed_within_threshold",
                    "output_threshold_status": "not_observed"
                    if output_ is None
                    else "exceeded"
                    if output_ > 20000
                    else "observed_within_threshold",
                }
            )
        return facts

    def halt(self, reason: str) -> Never:
        self.stop_reason = reason
        self.stop.set()
        raise LiveStopped(reason)

    def observe(self, raw: Mapping[str, JsonValue], session_id: str = "monitor") -> None:
        if self.settled:
            if raw.get("type") == "span.model_request_end" and self.on_late_usage is not None:
                self.on_late_usage(raw, session_id)
            self.halt("post-settlement native usage quarantined for reconciliation")
        with localcontext() as context:
            context.prec = 80
            try:
                self._observe(raw, session_id)
            except (ValueError, KeyError, TypeError):
                self.halt("native usage record is malformed")

    def _observe(self, raw: Mapping[str, JsonValue], session_id: str) -> None:
        thread = raw.get("thread_id")
        if isinstance(thread, str) and thread:
            previous = self.threads.setdefault(session_id, thread)
            if previous != thread:
                self.halt("unexpected second native thread")
        collision = False
        if raw.get("type") == "span.model_request_end":
            id_ = raw.get("id")
            if not isinstance(id_, str):
                self.halt("unidentified usage")
            identity = (session_id, id_)
            if identity in self.observations:
                self.quarantined_ids.add(id_)
                self.halt("duplicate usage quarantined")
            collision = any(request == id_ for _, request in self.observations)
            if collision:
                self.quarantined_ids.add(id_)
            self.observations[identity] = dict(raw)
            self.observed_times[identity] = datetime.now(UTC)
        costs: dict[str, Decimal] = {}
        for (_, request_id), event in self.observations.items():
            ref = ResourceRef(
                provider="anthropic", account_scope_id="monitor", kind="session", id="monitor"
            )
            obs = observation_from_event(event, ref, observed_at=datetime.now(UTC), model_id=MODEL)
            cache = obs.native_meter.get("cache_creation")
            if cache is not None and not isinstance(cache, dict):
                raise ValueError("invalid cache TTL split")
            duration = cache if isinstance(cache, dict) else {}
            tokens = TokenUsage(
                input_tokens=obs.input_tokens,
                output_tokens=obs.output_tokens,
                input_cached_tokens=obs.input_cached_tokens,
                input_cache_write_tokens=obs.input_cache_write_tokens,
                input_cache_write_5m_tokens=cast(
                    int | None, duration.get("ephemeral_5m_input_tokens")
                ),
                input_cache_write_1h_tokens=cast(
                    int | None, duration.get("ephemeral_1h_input_tokens")
                ),
            )
            if ((obs.input_tokens or 0) > 100000 or (obs.output_tokens or 0) > 20000) and str(
                event["id"]
            ) not in self.observed_thresholds:
                self.observed_thresholds.append(str(event["id"]))
            amount = monitor_token_estimate(self.price, tokens, datetime.now(UTC))
            if amount is None:
                self.halt("usage cost is unknown")
            costs[request_id] = max(costs.get(request_id, Decimal(0)), amount)
        total = sum(costs.values(), Decimal(0))
        # Wall time is a conservative monitoring estimate, never billed-runtime evidence.
        total += self.session_price.cost(Decimal(str(time.monotonic() - self.started)))
        self.cost = total
        if total >= self.threshold:
            self.halt("streamed cost reached the stop threshold")
        if collision:
            self.halt("cross-session native usage identity quarantined")


class MonitoredStream(httpx.AsyncByteStream):
    """Monitor the SDK's decoded SSE frames before forwarding them to its consumer."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        monitor: UsageMonitor,
        session_id: str,
        *,
        headers: httpx.Headers | None = None,
    ) -> None:
        self.stream, self.monitor, self.session_id = stream, monitor, session_id
        self.headers = headers
        self.pending_bytes = 0
        self.closed = False

    async def bounded_source(self, decoded: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in decoded:
            self.pending_bytes += len(chunk)
            if self.pending_bytes > 2_000_000:
                self.monitor.halt("native SSE frame exceeded the evidence limit")
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.closed:
            raise LiveStopped("native stream sealed before iteration")
        # Public SDK edge uses its own incremental decoder and HTTP content decoding.
        upstream = httpx.Response(200, headers=self.headers, stream=self.stream)
        source = httpx.Response(
            200, stream=_BoundedBytes(self.bounded_source(upstream.aiter_bytes()))
        )
        received = False
        async for frame in AsyncStream.raw_events(source):
            self.pending_bytes = 0
            if frame.data and frame.event != "ping":
                try:
                    event = json.loads(frame.data)
                except ValueError:
                    self.monitor.halt("native SSE payload is malformed")
                if isinstance(event, dict):
                    event = cast(dict[str, JsonValue], event)
                    if "type" not in event:
                        event["type"] = frame.event  # Same type injection as the SDK stream.
                    if not isinstance(event.get("type"), str):
                        self.monitor.halt("native SSE frame has no typed event")
                    received = True
                    self.monitor.observe(event, self.session_id)
                else:
                    self.monitor.halt("native SSE payload is not a typed event")
            if self.closed:
                raise LiveStopped("native stream sealed while response was in flight")
            # Forward one decoded frame, never a chunk containing later unchecked events.
            yield ("\n".join(frame.raw) + "\n\n").encode()
        if not received:
            self.monitor.halt("native stream contained no decodable events")

    async def aclose(self) -> None:
        self.closed = True
        await self.stream.aclose()


class ResponseFence(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, transport: LiveTransport) -> None:
        self.stream, self.transport = stream, transport
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.closed or self.transport.closed:
            raise LiveStopped("native response sealed before iteration")
        async for chunk in self.stream:
            if self.closed or self.transport.closed:
                raise LiveStopped("native response sealed while body was in flight")
            yield chunk

    async def aclose(self) -> None:
        self.closed = True
        await self.stream.aclose()


class _BoundedBytes(httpx.AsyncByteStream):
    def __init__(self, iterator: AsyncIterator[bytes]) -> None:
        self.iterator = iterator

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.iterator:
            yield chunk


class LiveTransport(httpx.AsyncBaseTransport):
    """Observe accepted create IDs and enforce one native session/no replay POSTs."""

    def __init__(
        self,
        upstream: httpx.AsyncBaseTransport,
        authorization: OwnedAuthorization,
        monitor: UsageMonitor,
        *,
        max_user_sends: int,
    ) -> None:
        self.upstream, self.authorization, self.monitor = upstream, authorization, monitor
        self.max_user_sends = max_user_sends
        self.native_budget_sent = False
        self.session_attempts = self.user_sends = 0
        self.session_id: str | None = None
        self.session_ids: list[str] = []
        self.archived: set[str] = set()
        self.skill_bodies: list[tuple[str, bytes]] = []
        self.agent_body: dict[str, JsonValue] | None = None
        self.requests: list[tuple[str, str]] = []
        self.stopping = False
        self.closed = False
        self.orphans: set[tuple[str, str]] = set()
        self.pending_creates = 0
        self.on_orphan: Callable[[str, str], Awaitable[None]] | None = None
        self.responses: set[httpx.Response] = set()
        self.pending_responses: set[asyncio.Task[object]] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.closed:
            raise LiveStopped("live transport sealed after cleanup")
        path = request.url.path
        stopped = self.stopping or self.monitor.stop.is_set()
        if (
            stopped
            and request.method == "POST"
            and not (path.endswith("/events") or path.endswith("/archive"))
        ):
            raise LiveStopped("paid mutation refused after a stop control")
        if path.startswith("/v1/sessions/") and path.split("/")[3] not in self.session_ids:
            raise LiveStopped("native request escaped the accepted session inventory")
        self.requests.append((request.method, path))
        if request.method == "POST" and path == "/v1/sessions":
            body = json.loads(await request.aread())
            if "budget" in body:
                self.native_budget_sent = True
            if self.stopping or self.monitor.stop_reason is not None:
                raise LiveStopped("new session refused after a stop control")
            if self.session_ids and self.session_ids[-1] not in self.archived:
                raise LiveStopped("prior session cleanup has not been acknowledged")
            self.session_attempts += 1
            if self.session_attempts > self.max_user_sends:
                raise LiveStopped("fresh or retry session refused")
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(await request.aread())
            if stopped and any(
                item.get("type") != "user.interrupt" for item in body.get("events", [])
            ):
                raise LiveStopped("non-cleanup event refused after a stop control")
            if any(item.get("type") == "user.message" for item in body.get("events", [])):
                self.user_sends += 1
                if (
                    self.stopping
                    or self.monitor.stop_reason is not None
                    or self.user_sends > self.max_user_sends
                ):
                    raise LiveStopped("retry or extra user turn refused")
        if request.method == "POST" and path == "/v1/skills":
            self.skill_bodies.append((request.headers["content-type"], await request.aread()))
        if request.method == "POST" and path == "/v1/agents":
            self.agent_body = json.loads(await request.aread())
        creating = request.method == "POST" and path in {
            "/v1/sessions",
            "/v1/agents",
            "/v1/skills",
            "/v1/vaults",
        }
        if creating:
            self.pending_creates += 1
        pending = asyncio.current_task()
        if pending is not None:
            self.pending_responses.add(pending)
        try:
            response = await self.upstream.handle_async_request(request)
        finally:
            if pending is not None:
                self.pending_responses.discard(pending)
        if self.closed and not creating:
            await response.aclose()
            raise LiveStopped("late native response refused after settlement fence")
        if not self.closed:
            self.responses.add(response)  # Includes an in-flight native create body.
        if creating and not response.is_success:
            self.pending_creates -= 1
        if response.is_success and path.endswith("/archive"):
            self.archived.add(path.split("/")[-2])
        if path.endswith("/stream") or (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            == "text/event-stream"
        ):
            if not path.startswith("/v1/sessions/") or path.split("/")[3] not in self.session_ids:
                await response.aclose()
                self.monitor.halt("native SSE response has no accepted session identity")
            wrapped = httpx.Response(
                response.status_code,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() not in {"content-encoding", "content-length"}
                },
                stream=MonitoredStream(
                    cast(httpx.AsyncByteStream, response.stream),
                    self.monitor,
                    path.split("/")[3],
                    headers=response.headers,
                ),
                extensions=response.extensions,
            )
            self.responses.discard(response)
            self.responses.add(wrapped)
            return wrapped
        if (
            response.is_success
            and request.method == "POST"
            and path in {"/v1/sessions", "/v1/agents", "/v1/skills", "/v1/vaults"}
        ):
            raw = cast(dict[str, JsonValue], json.loads(await response.aread()))
            id_ = raw.get("id")
            if not isinstance(id_, str) or not id_:
                raise LiveStopped("native create acknowledgement has no resource identity")
            kind = {
                "/v1/sessions": "session",
                "/v1/agents": "agent",
                "/v1/skills": "skill",
                "/v1/vaults": "vault",
            }[path]
            self.pending_creates -= 1
            if self.closed or self.stopping or self.monitor.stop.is_set():
                orphan = (kind, id_)
                if orphan not in self.orphans:
                    self.orphans.add(orphan)
                    if self.on_orphan is not None:
                        await self.on_orphan(kind, id_)
                raise LiveStopped("late create acknowledgement quarantined for reconciliation")
            self.authorization.created.add((kind, id_))
            if kind == "session":
                self.session_id = id_
                self.session_ids.append(id_)
                agent = raw.get("agent")
                model = agent.get("model") if isinstance(agent, dict) else None
                if isinstance(model, dict):
                    model = model.get("id")
                if model == MODEL:
                    self.monitor.verified_models[id_] = MODEL
        if self.closed:
            await response.aclose()
            raise LiveStopped("late native response refused after body fence")
        if response.is_stream_consumed:
            return response  # Preserve the ACK's cached body; native streams are one-shot.
        wrapped = httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=ResponseFence(cast(httpx.AsyncByteStream, response.stream), self),
            extensions=response.extensions,
        )
        self.responses.discard(response)
        self.responses.add(wrapped)
        return wrapped

    def seal(self) -> None:
        self.closed = True
        for pending in self.pending_responses:
            pending.cancel()
        for response in self.responses:
            stream = response.stream
            if isinstance(stream, (MonitoredStream, ResponseFence)):
                stream.closed = True

    async def aclose(self) -> None:
        self.seal()
        try:
            await asyncio.gather(*(response.aclose() for response in self.responses))
        finally:
            await self.upstream.aclose()


@dataclass
class LiveContext:
    admission: Admission
    client: AsyncAnthropic
    driver: AnthropicManagedAgents
    transport: LiveTransport
    reservation: Reservation
    evidence: list[AnthropicSessionSpend] = field(default_factory=list[AnthropicSessionSpend])
    completed_legs: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    retire: Callable[[], Awaitable[None]] | None = None
    redaction_secrets: tuple[str, ...] = field(default=(), repr=False)

    def retain_completed_leg(self, leg: dict[str, object]) -> None:
        """Save audited final output before a later proof or cleanup can fail."""
        path = leg.get("path")
        if self.admission.package != "A7" or path not in ("legacy", "mux"):
            raise LiveRefused("completed leg is outside the admitted A7 pair")
        Audit(self.redaction_secrets).audit(json.loads(json.dumps([*self.completed_legs, leg])))
        descriptor = os.open(
            self.admission.output / f"a7-{path}-leg.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w") as output:
            json.dump(leg, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(self.admission.output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.completed_legs.append(leg)

    def ref(self, id_: str, kind: str) -> ResourceRef:
        b = self.admission.binding
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="anthropic",
            account_scope_id=b.account_scope_id,
            tenant_id=b.scope.tenant_id,
            account_id=b.scope.account_id,
        )

    async def stop_session(self) -> None:
        self.transport.stopping = True
        for id_ in self.transport.session_ids:
            if id_ in self.transport.archived:
                continue
            ref = self.ref(id_, "session")
            try:
                async with asyncio.timeout(3):
                    await self.driver.events.cancel(
                        self.admission.binding.scope, ref, turn_id="live-root", key="live-stop"
                    )
            finally:
                async with asyncio.timeout(3):
                    await self.driver.session_admin.archive(
                        self.admission.binding.scope, ref, key="live-archive"
                    )


def usage_tokens(obs: UsageObservation) -> TokenUsage:
    cache = obs.native_meter.get("cache_creation")
    duration = cache if isinstance(cache, dict) else {}
    return TokenUsage.model_validate(
        {
            "input_tokens": obs.input_tokens,
            "output_tokens": obs.output_tokens,
            "input_cached_tokens": obs.input_cached_tokens,
            "input_cache_write_tokens": obs.input_cache_write_tokens,
            "input_cache_write_5m_tokens": duration.get("ephemeral_5m_input_tokens"),
            "input_cache_write_1h_tokens": duration.get("ephemeral_1h_input_tokens"),
        }
    )


def verified_usage_variants(context: LiveContext) -> dict[tuple[str, str], list[UsageObservation]]:
    variants: dict[tuple[str, str], list[UsageObservation]] = {}
    monitor = context.transport.monitor

    def retain(obs: UsageObservation) -> None:
        session = obs.session
        if (
            session.id not in context.transport.session_ids
            or session != context.ref(session.id, "session")
            or monitor.verified_models.get(session.id) != MODEL
            or obs.model is None
            or obs.model.provider != "anthropic"
            or obs.model.id != MODEL
            or obs.observed_at.tzinfo is None
            or obs.observed_at < context.reservation.receipt.timestamp
            or obs.grain != "model_request"
            or obs.basis != "increment"
            or obs.covers
        ):
            return  # Foreign/stale/unverified facts never erase independent verified proof.
        try:
            usage_tokens(obs)
        except (ValueError, TypeError):
            return  # Malformed categories never erase valid independent proof.
        variants.setdefault((session.id, obs.id), []).append(obs)

    for evidence in context.evidence:
        for obs in evidence.observations:
            if obs.session == evidence.session:
                retain(obs)
    for identity, raw in monitor.observations.items():
        session_id, _ = identity
        try:
            retain(
                observation_from_event(
                    raw,
                    context.ref(session_id, "session"),
                    observed_at=monitor.observed_times[identity],
                    model_id=MODEL,
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    owners: dict[str, set[str]] = {}
    for session, request in variants:
        owners.setdefault(request, set()).add(session)
    monitor.quarantined_ids.update(
        request for request, sessions in owners.items() if len(sessions) > 1
    )
    return variants


def selected_usage(
    context: LiveContext, key: Callable[[UsageObservation], Decimal | int]
) -> tuple[AnthropicSessionSpend, ...]:
    # Quarantined collisions remain separate evidence. A lower-bound projection
    # chooses ONE original witness per native ID; it never asserts disjoint billing.
    by_request: dict[str, list[UsageObservation]] = {}
    for (_, request), variants in verified_usage_variants(context).items():
        by_request.setdefault(request, []).extend(variants)
    by_session: dict[str, list[UsageObservation]] = {}
    for variants in by_request.values():
        witness = max(variants, key=key)
        by_session.setdefault(witness.session.id, []).append(witness)
    return tuple(
        AnthropicSessionSpend(
            context.ref(id_, "session"), tuple(observations), runtime=None, usage_complete=False
        )
        for id_, observations in by_session.items()
    )


def monetary_bound(context: LiveContext, obs: UsageObservation) -> Decimal:
    singleton = actual_from_anthropic_sessions(
        context.reservation,
        (AnthropicSessionSpend(obs.session, (obs,), None),),
        pricing_basis="standard-global",
    )
    with localcontext() as precision:
        precision.prec = 80
        # Exact pure N8 bound, explicitly owner-granted for this proof selector.
        return BudgetGuard._union_floor(  # pyright: ignore[reportPrivateUsage]
            context.reservation.receipt, singleton, None
        )


def merged_usage(context: LiveContext) -> tuple[AnthropicSessionSpend, ...]:
    """Maximum dated verified floor per request; conflicts never imply complete actual."""
    return selected_usage(context, lambda obs: monetary_bound(context, obs))


def limit_overrun_proof(context: LiveContext) -> ActualSpend | None:
    # A cheaper conflicting snapshot must not erase either financial token-limit latch.
    for input_ in (True, False):
        evidence = selected_usage(
            context,
            (lambda obs: usage_tokens(obs).minimum_input_tokens)
            if input_
            else (lambda obs: obs.output_tokens or 0),
        )
        count = sum(
            usage_tokens(obs).minimum_input_tokens if input_ else obs.output_tokens or 0
            for item in evidence
            for obs in item.observations
        )
        maximum = (
            context.admission.plan.limits.input_tokens
            if input_
            else context.admission.plan.limits.output_tokens
        )
        if count > maximum:
            return actual_from_anthropic_sessions(
                context.reservation, evidence, pricing_basis="standard-global"
            )
    return None


async def run_live(
    admission: Admission,
    callback: Callable[[LiveContext], Awaitable[Mapping[str, object]]],
    *,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] = lambda: httpx.AsyncHTTPTransport(
        trust_env=False
    ),
    secret_loader: Callable[[Path], str] | None = None,
) -> Mapping[str, object]:
    """Exactly one admission/settlement, cleanup bounded inside the whole-run deadline."""
    refuse_ambient()
    refuse_late_usage(admission.ledger_path)
    marker = admission.ledger_path.parent / (".n4-live-attempt-" + admission.approval_digest)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        raise LiveRefused("the signed envelope already has an attempt") from None
    guard = BudgetGuard(admission.config_path, admission.ledger_path)
    reservation = guard.reserve_with_hold(
        admission.plan,
        minimum_hold_usd=admission.hold,
        maximum_hold_usd=admission.hold,
        expected_config_sha256=HOST_CONFIG_SHA256,
        expected_ledger_id=LEDGER_ID,
    )
    started = time.monotonic()
    context: LiveContext | None = None
    status: Literal["completed", "failed", "cancelled"] = "failed"
    result: Mapping[str, object] = {"status": "failed"}
    receipt: SpendReceipt | None = None
    task: asyncio.Future[Mapping[str, object]] | None = None
    stop_task: asyncio.Task[bool] | None = None
    durable_ids = DurableNativeIds()
    try:
        admission.output.mkdir(parents=True, exist_ok=False)
        path = Path(str(admission.approval["credential_file"]))
        key = (
            secret_loader(path)
            if secret_loader is not None
            else explicit_secret(path, env_file=True)
        )
        config = BudgetConfig.load(admission.config_path)
        provider = config.providers["anthropic"]
        price = provider.models[MODEL]
        runtime_price = next(p for p in provider.session_prices if p.meter == "agent_session")
        authorization = OwnedAuthorization(
            admission.binding.scope,
            frozenset(
                (ref.kind, ref.id)
                for ref in (admission.binding.environment, admission.binding.agent)
                if ref is not None
            ),
        )
        monitor = UsageMonitor(
            price, runtime_price, admission.hold * Decimal(".8"), started=started
        )
        transport = LiveTransport(
            transport_factory(),
            authorization,
            monitor,
            max_user_sends=2 if admission.package == "A7" else 1,
        )
        client = build_async_anthropic(
            transport=transport, api_key=key, max_retries=0, timeout=httpx.Timeout(10, read=30)
        )
        native = AnthropicManagedAgents(
            client, account_scope_id=admission.binding.account_scope_id, authorization=authorization
        )
        context = LiveContext(
            admission, client, native, transport, reservation, redaction_secrets=(key,)
        )

        async def orphan_cleanup(kind: str, id_: str) -> None:
            facts: dict[str, object] = {
                "kind": kind,
                "id": id_,
                "acknowledged_at": datetime.now(UTC).isoformat(),
                "reconciliation_required": True,
                "hold_retained": True,
                "late_cleanup_outside_original_deadline": True,
                "interrupt_ack": False,
                "retirement_ack": False,
            }
            orphan_path = admission.output / (
                "orphan-" + hashlib.sha256((kind + ":" + id_).encode()).hexdigest() + ".json"
            )

            def persist() -> None:
                safe_facts = durable_receipt(facts, durable_ids, context.redaction_secrets)
                descriptor = os.open(
                    orphan_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
                )
                try:
                    os.write(descriptor, (json.dumps(safe_facts, indent=2) + "\n").encode())
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            persist()  # Durable evidence precedes any further native IO.
            cleanup_client: AsyncAnthropic | None = None
            try:
                async with asyncio.timeout(6):
                    cleanup_client = build_async_anthropic(
                        transport=transport_factory(),
                        api_key=key,
                        max_retries=0,
                        timeout=httpx.Timeout(3),
                    )
                    owned = OwnedAuthorization(admission.binding.scope, frozenset({(kind, id_)}))
                    cleanup = AnthropicManagedAgents(
                        cleanup_client,
                        account_scope_id=admission.binding.account_scope_id,
                        authorization=owned,
                    )
                    ref = context.ref(id_, kind)
                    if kind == "session":
                        try:
                            async with asyncio.timeout(3):
                                await cleanup.events.cancel(
                                    admission.binding.scope,
                                    ref,
                                    turn_id="live-root",
                                    key="late-orphan-interrupt",
                                )
                                facts["interrupt_ack"] = True
                        finally:
                            async with asyncio.timeout(3):
                                await cleanup.session_admin.archive(
                                    admission.binding.scope, ref, key="late-orphan-archive"
                                )
                                facts["retirement_ack"] = True
                    elif kind == "agent":
                        await cleanup.agents.archive(
                            admission.binding.scope, ref, key="late-orphan-agent"
                        )
                        facts["retirement_ack"] = True
                    elif kind == "skill":
                        await cleanup.skills.delete(
                            admission.binding.scope, id_, key="late-orphan-skill"
                        )
                        facts["retirement_ack"] = True
                    elif kind == "vault":
                        vaults = cleanup.extension(Vaults, namespace="anthropic.vaults", version=1)
                        await vaults.archive(admission.binding.scope, ref, key="late-orphan-vault")
                        facts["retirement_ack"] = True
            except Exception:
                facts["cleanup_failed"] = True
            finally:
                if cleanup_client is not None:
                    try:
                        async with asyncio.timeout(1):
                            await cleanup_client.close()
                    except Exception:
                        facts["cleanup_failed"] = True
                persist()

        transport.on_orphan = orphan_cleanup

        late_durable_records: list[JsonValue] = []
        late_durable_bytes = 0
        late_enrichment_refused = False

        def late_usage(raw: Mapping[str, JsonValue], session_id: str) -> None:
            nonlocal late_durable_bytes, late_enrichment_refused
            observed_at = datetime.now(UTC)
            identity = hashlib.sha256(
                (session_id + ":" + str(raw.get("id")) + ":" + observed_at.isoformat()).encode()
            ).hexdigest()
            path = admission.ledger_path.parent / (
                late_usage_prefix(admission.ledger_path) + identity + ".json"
            )
            facts: dict[str, object] = {
                "admission_blocked": True,
                "reconciliation_required": True,
                "hold_retained": True,
                "retained_hold_usd": str(
                    receipt.held_usd if receipt is not None else admission.hold
                ),
                "ledger_id": LEDGER_ID,
                "run_id": reservation.receipt.run_id,
                "config_sha256": HOST_CONFIG_SHA256,
                "model": MODEL,
                "observed_at": observed_at.isoformat(),
                "actual_usd": None,
                "usage_complete": False,
                "runtime": None,
            }
            initial: JsonValue = json.loads(json.dumps(facts))
            initial_encoded = (json.dumps(facts, indent=2) + "\n").encode()
            tracked = (
                not late_enrichment_refused
                and len(late_durable_records) < 256
                and late_durable_bytes + len(initial_encoded) <= 1024 * 1024
            )
            if not tracked:
                late_enrichment_refused = True
            else:
                try:
                    audit_late_usage_records(
                        [*late_durable_records, initial],
                        context.redaction_secrets,
                        frozenset(durable_ids.hashes),
                    )
                except RecordingError:
                    late_enrichment_refused = True
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                # Latch first: malformed or secret-bearing extra proof cannot remove it.
                # Audit the combined durable set before even this write; a hit
                # closes enrichment, never suppresses the trusted safety latch.
                os.write(descriptor, initial_encoded)
                os.fsync(descriptor)
                if tracked:
                    late_durable_records.append(initial)
                    late_durable_bytes += len(initial_encoded)
                # Keep the durable latch, but never enrich it with a registered
                # API key or MCP credential echoed anywhere in a native meter.
                Audit(context.redaction_secrets).audit(dict(raw))
                native_id = raw.get("id")
                if not isinstance(native_id, str):
                    raise RecordingError("late usage lacks a native identity")
                id_projection = durable_ids.project(native_id)
                facts["native_id_projection"] = id_projection
                if id_projection["shape_rejected"] or id_projection["duplicate_rejected"]:
                    # A rejected identity leaves only its hash and trusted latch.
                    # Never turn malformed/free-text IDs into a durable proof.
                    safe: JsonValue = json.loads(json.dumps(facts))
                    audit_late_usage_records(
                        [*(late_durable_records[:-1] if tracked else late_durable_records), safe],
                        context.redaction_secrets,
                        frozenset(durable_ids.hashes),
                    )
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    os.ftruncate(descriptor, 0)
                    rejected_encoded = (json.dumps(facts, indent=2) + "\n").encode()
                    os.write(descriptor, rejected_encoded)
                    os.fsync(descriptor)
                    if tracked:
                        late_durable_records[-1] = safe
                        late_durable_bytes += len(rejected_encoded) - len(initial_encoded)
                    raise RecordingError("late native identity rejected; enrichment closed")
                # Unknown provider metadata may contain only HALF of a known
                # credential per frame. Refuse it before any proof write rather
                # than persisting halves and trying to retract them later.
                meter = raw.get("model_usage")
                if not isinstance(meter, dict):
                    raise RecordingError("late usage meter is not a closed numeric proof")
                counts = {
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                }
                if set(meter) - counts - {"cache_creation", "speed"}:
                    raise RecordingError("late usage meter contains unreviewed metadata")
                for name in counts:
                    value = meter.get(name)
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                    ):
                        raise RecordingError("late usage meter has a nonnumeric counter")
                if meter.get("speed") not in (None, "standard"):
                    raise RecordingError("late usage inference basis is unverified")
                cache = meter.get("cache_creation")
                if cache is not None:
                    if not isinstance(cache, dict) or set(cache) - {
                        "ephemeral_5m_input_tokens",
                        "ephemeral_1h_input_tokens",
                    }:
                        raise RecordingError("late cache meter contains unreviewed metadata")
                    for value in cache.values():
                        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                            raise RecordingError("late cache meter has a nonnumeric counter")
                verified = (
                    session_id in transport.session_ids
                    and monitor.verified_models.get(session_id) == MODEL
                )
                facts["served_model_verified"] = verified
                if verified:
                    observation = observation_from_event(
                        raw,
                        context.ref(session_id, "session"),
                        observed_at=observed_at,
                        model_id=MODEL,
                    )
                    usage_tokens(observation)
                    proof: dict[str, JsonValue] = observation.model_dump(mode="json")
                    proof["id"] = id_projection["sha256"]
                    session_projection = durable_ids.project(session_id)
                    session_proof = proof["session"]
                    if not isinstance(session_proof, dict):
                        raise RecordingError("late usage lacks a session identity")
                    session_proof["id"] = session_projection["sha256"]
                    facts["session_id_projection"] = session_projection
                    facts["proof"] = proof
                    facts["verified_monetary_hold_floor_usd"] = str(
                        monetary_bound(context, observation)
                    )
                    # Audit provider proof, not our trusted SHA pins (the tape
                    # scanner treats long opaque hex as ambiguous encoded text).
                    Audit(context.redaction_secrets).audit(observation.model_dump(mode="json"))
                    candidate: JsonValue = json.loads(json.dumps(facts))
                    encoded = (json.dumps(facts, indent=2) + "\n").encode()
                    if (
                        late_enrichment_refused
                        or late_durable_bytes - len(initial_encoded) + len(encoded) > 1024 * 1024
                    ):
                        late_enrichment_refused = True
                        raise RecordingError("late usage audit scope is closed")
                    try:
                        # Audit every durable field across every accepted late
                        # frame before enrichment, including proof.id/session.
                        audit_late_usage_records(
                            [*late_durable_records[:-1], candidate],
                            context.redaction_secrets,
                            frozenset(durable_ids.hashes),
                        )
                    except RecordingError:
                        late_enrichment_refused = True
                        raise
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    os.ftruncate(descriptor, 0)
                    os.write(descriptor, encoded)
                    os.fsync(descriptor)
                    late_durable_records[-1] = candidate
                    late_durable_bytes += len(encoded) - len(initial_encoded)
            except RecordingError:
                # A per-frame refusal must close the same scope as a joined hit.
                late_enrichment_refused = True
                raise
            finally:
                os.close(descriptor)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

        monitor.on_late_usage = late_usage
        try:
            # Reserve 15 seconds of the SAME deadline for interrupt/archive/close.
            task = asyncio.ensure_future(callback(context))
            remaining = max(
                0,
                admission.deadline_s
                - min(15, admission.deadline_s / 2)
                - (time.monotonic() - started),
            )
            stop_task = asyncio.create_task(monitor.stop.wait())
            done, _ = await asyncio.wait(
                (task, stop_task), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor.stop.is_set() or task not in done:
                context.transport.stopping = True
                task.cancel()
                await asyncio.wait(
                    (task,),
                    timeout=min(1, max(0, admission.deadline_s - (time.monotonic() - started)) / 4),
                )
                if monitor.stop_reason is None:
                    monitor.stop_reason = "whole-run deadline reached; cleanup margin reserved"
                    monitor.stop.set()
                raise LiveStopped(monitor.stop_reason)
            result = task.result()
            status = "completed" if result.get("status") == "pass" else "failed"
        except asyncio.CancelledError:
            context.transport.stopping = True
            if task is not None:
                task.cancel()
            status = "cancelled"
            raise
        except Exception as error:
            result = {
                "status": "failed",
                "reason": "native run stopped or failed; no retry",
                "failure_type": type(error).__name__,
            }
    finally:

        async def finalize() -> None:
            nonlocal status, result, receipt
            try:
                remaining = max(0, admission.deadline_s - (time.monotonic() - started))
                async with asyncio.timeout(remaining):
                    if stop_task is not None:
                        stop_task.cancel()
                        await asyncio.gather(stop_task, return_exceptions=True)
                    if task is not None:

                        def consume_done(future: asyncio.Future[Mapping[str, object]]) -> None:
                            if not future.cancelled():
                                future.exception()

                        task.add_done_callback(consume_done)
                    if context is not None:
                        try:
                            await context.stop_session()
                        finally:
                            try:
                                if context.retire is not None:
                                    await context.retire()
                            finally:
                                await context.client.close()
            except Exception:
                status = "failed"
                result = {
                    "status": "failed",
                    "reason": "cleanup acknowledgement unavailable; hold retained",
                }
            finally:
                actual = None
                overrun_witness = None
                if context is not None:
                    context.transport.seal()
                    try:
                        actual = actual_from_anthropic_sessions(
                            reservation, merged_usage(context), pricing_basis="standard-global"
                        )
                        overrun_witness = limit_overrun_proof(context)
                    except Exception:
                        actual = None
                if overrun_witness is not None:
                    receipt = guard.settle_with_overrun(
                        reservation,
                        status=status,
                        limits=admission.plan.limits,
                        actual=actual,
                        overrun_evidence=overrun_witness,
                    )
                else:
                    receipt = guard.settle(
                        reservation, status=status, limits=admission.plan.limits, actual=actual
                    )
                if context is not None:
                    context.transport.monitor.settled = True
                    if context.completed_legs:
                        result = {**result, "legs": context.completed_legs}
                if admission.output.is_dir():
                    facts: dict[str, object] = {
                        "source_head": admission.source_head,
                        "package": admission.package,
                        "model": MODEL,
                        "native_control_mode": (
                            "native-budget-and-guard"
                            if context is not None and context.transport.native_budget_sent
                            else "guard-only"
                        ),
                        "native_budget_sent": context is not None
                        and context.transport.native_budget_sent,
                        "provider_token_caps_enforced": False,
                        "observed_per_request_thresholds": [100000, 20000],
                        "wall_clock_deadline_seconds": admission.deadline_s,
                        "stop_threshold_usd": str(admission.hold * Decimal(".8")),
                        "result": result,
                        "spend": receipt.model_dump(mode="json"),
                    }
                    if context is not None:
                        facts["created_resources"] = sorted(context.transport.authorization.created)
                        facts["pending_create_acknowledgements"] = context.transport.pending_creates
                        facts["pending_native_responses"] = len(context.transport.pending_responses)
                        facts["open_native_responses"] = sum(
                            not response.is_closed for response in context.transport.responses
                        )
                        facts["orphan_resources"] = sorted(context.transport.orphans)
                        facts["reconciliation_required"] = bool(
                            context.transport.pending_creates
                            or context.transport.orphans
                            or context.transport.pending_responses
                        )
                        facts["conflicting_request_evidence"] = [
                            id_
                            for (_, id_), variants in verified_usage_variants(context).items()
                            if len(
                                {
                                    json.dumps(usage_tokens(obs).model_dump(), sort_keys=True)
                                    for obs in variants
                                }
                            )
                            > 1
                        ]
                        facts["quarantined_native_request_ids"] = sorted(
                            context.transport.monitor.quarantined_ids
                        )
                        facts["quarantined_request_provenance"] = [
                            {"session_id": session, "native_request_id": request}
                            for session, request in verified_usage_variants(context)
                            if request in context.transport.monitor.quarantined_ids
                        ]
                        facts["reconciliation_required"] = bool(
                            facts["reconciliation_required"]
                            or context.transport.monitor.quarantined_ids
                        )
                        facts["archived_sessions"] = sorted(context.transport.archived)
                        facts["observed_token_threshold_crossings"] = (
                            context.transport.monitor.observed_thresholds
                        )
                        facts["stream_stop_reason"] = context.transport.monitor.stop_reason
                        facts["stream_cost_estimate_usd"] = (
                            str(context.transport.monitor.cost)
                            if context.transport.monitor.cost is not None
                            else None
                        )
                        facts["monitor_cost_complete"] = False
                        facts["monitor_cost_basis"] = (
                            "known token lower bound plus wall-time estimate"
                        )
                        facts["observed_request_threshold_status"] = (
                            context.transport.monitor.threshold_receipts()
                        )
                        facts["runtime_measurement"] = (
                            "unknown; wall-clock monitoring is not billed-runtime proof"
                        )
                        facts["observed_request_meters"] = [
                            raw.get("model_usage")
                            for raw in context.transport.monitor.observations.values()
                        ]
                    try:
                        if context is not None:
                            facts["requests"] = request_metadata(
                                context.transport.requests,
                                context.transport.authorization.resources
                                | frozenset(context.transport.authorization.created)
                                | frozenset(context.transport.orphans),
                            )
                        facts = dict(
                            durable_receipt(
                                facts, durable_ids, context.redaction_secrets if context else ()
                            )
                        )
                    except RecordingError:
                        facts = {
                            "package": admission.package,
                            "model": MODEL,
                            "status": "failed",
                            "reason": "evidence export refused by credential audit",
                            "guard_settlement_persisted": True,
                            "native_budget_sent": context is not None
                            and context.transport.native_budget_sent,
                        }
                    (admission.output / "receipt.json").write_text(
                        json.dumps(facts, indent=2) + "\n"
                    )

        finalizer = asyncio.create_task(finalize())
        recancelled = False
        while not finalizer.done():
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                recancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        finalizer.result()
        if recancelled:
            raise asyncio.CancelledError

    if receipt is None:
        raise LiveRefused("settlement receipt unavailable")
    return {"result": result, "spend": receipt.model_dump(mode="json")}


def add_live_arguments(parser: object) -> None:
    import argparse

    if not isinstance(parser, argparse.ArgumentParser):
        raise TypeError("expected an argument parser")
    parser.add_argument("--live-binding", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--go-receipt", type=Path)
    parser.add_argument("--go-sha256")
    parser.add_argument("--budget-config", type=Path)
    parser.add_argument("--spend-ledger", type=Path)


def live_command(package: Package, args: object) -> None:
    import argparse

    from daimon.testing.a7_anthropic import source_pin
    from daimon.testing.anthropic_live_cases import a7_pair, f2_default

    if not isinstance(args, argparse.Namespace):
        raise TypeError("expected command arguments")
    try:
        paths = (
            args.live_binding,
            args.authorization,
            args.go_receipt,
            args.go_sha256,
            args.budget_config,
            args.spend_ledger,
        )
        if any(item is None for item in paths):
            raise LiveRefused("signed installation, bindings and separate lead GO are required")
        source = source_pin()
        admission = admit(
            package=package,
            approval_path=args.authorization,
            go_path=args.go_receipt,
            go_digest=args.go_sha256,
            config_path=args.budget_config,
            ledger_path=args.spend_ledger,
            binding_path=args.live_binding,
            output=args.output,
            source_head=str(source["head"]),
            source_clean=source["clean"] is True,
        )
    except Exception:
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "receipt.json").write_text(
            json.dumps(
                {
                    "status": "PENDING",
                    "reason": "live admission prerequisites missing or stale",
                    "package": package,
                    "model": MODEL,
                    "native_control_mode": "guard-only",
                    "native_budget_sent": False,
                    "external_provider_calls": 0,
                    "credential_reads": 0,
                    "reserved_usd": "0",
                },
                indent=2,
            )
            + "\n"
        )
        return
    asyncio.run(run_live(admission, a7_pair if package == "A7" else f2_default))
