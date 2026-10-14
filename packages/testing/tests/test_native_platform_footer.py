"""Frozen D11 predicates over actual posts and independently queried accounting.

Native payload normalizers and real adapters are used; rates are controlled,
not live prices. These are offline component proofs, not full catalog ingress.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import pytest
import yaml
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.state import TextBlock, TurnState, UsageTotals
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.footer_accounting import FooterAccountingReport
from daimon.testing.outcome_oracle import RunEvidence
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.slack_surface import SlackSurfaceHarness
from daimon.testing.teams_surface import TeamsSurfaceHarness
from daimon.testing.turn_accounting import RevisionRateAttribution
from mux.contracts.ids import ModelRef

from . import test_footer_accounting as controls
from .test_footer_accounting import (
    MARKUP,
    ROOT,
    Account,
    require_checks,
    terminal,
    turn_inventory,
    turn_rate,
)

account = controls.account
footer_engine = controls.footer_engine

type Surface = Literal["discord", "slack", "teams"]
type Capture = DiscordSurfaceCapture | PlatformSurfaceCapture
REPO = Path(__file__).resolve().parents[3]


class NativeResult(Protocol):
    evidence: RunEvidence
    discord_surfaces: tuple[DiscordSurfaceCapture, ...]
    platform_surfaces: tuple[PlatformSurfaceCapture, ...]
    request_count: int


async def actual_posts(
    value: Account, surface: Surface, *, answer: str = "FOOTER-OK", agent_name: str = "Daimon"
) -> Capture:
    common: dict[str, Any] = dict(
        evidence_id=f"actual:{surface}:turn1",
        session_id=value.hook.native_session.id,
        root_turn_id=ROOT,
        model_id=value.price.model,
        agent_name=agent_name,
        markup=MARKUP,
    )
    harness: DiscordSurfaceHarness | SlackSurfaceHarness | TeamsSurfaceHarness
    if surface == "discord":
        harness = DiscordSurfaceHarness(**common)
    elif surface == "slack":
        harness = SlackSurfaceHarness(channel="101", thread_ts="102", **common)
    else:
        harness = TeamsSurfaceHarness(conversation_id="102", **common)
    state = TurnState(
        content=[TextBlock(kind="text", text=answer)],
        usage_totals=UsageTotals(input_tokens=1000, output_tokens=1000)
        if value.observation.session.provider == "anthropic"
        else ProviderUsageTotals().add_observation(value.observation),
    )

    async def invoke(lifecycle: Any) -> None:
        await lifecycle.post_initial()
        await lifecycle.on_terminal_success(state)

    return await harness.run(invoke)


async def routed(value: Account, surface: Surface) -> Account:
    if surface == "discord":
        return value
    old = value.hook.binding
    native = value.hook.native_session.model_copy(update={"id": f"session:{surface}"})
    binding = old.model_copy(
        update={
            "thread": old.thread.model_copy(
                update={"channel": old.thread.channel.model_copy(update={"platform": surface})}
            ),
            "id": f"{old.id}:{surface}",
            "generation": 1,
            "native_refs": {"session": native.id},
        }
    )
    await PostgresStateStore(value.hook.sessions).put_binding(binding, expected_generation=0)
    return replace(
        value,
        hook=replace(value.hook, binding=binding, native_session=native),
        observation=controls.normalize(
            native.provider, native, ModelRef(provider=native.provider, id=value.price.model)
        ),
    )


async def joined(value: Account, capture: Capture, **changes: Any) -> FooterAccountingReport:
    options: dict[str, Any] = dict(
        capture=capture,
        terminal=terminal(session_id=value.hook.native_session.id),
        receipts=(value.observation,),
        attributions=(turn_rate(value, value.observation),),
        inventory=turn_inventory(value, value.observation),
        markup=MARKUP,
    )
    return await value.hook.evaluate_turn(**{**options, **changes})


@pytest.mark.parametrize("surface", ["discord", "slack", "teams"])
async def test_frozen_d11_required_cost_is_executed_against_actual_posts(
    account: Account, surface: Surface, tmp_path: Path
) -> None:
    value = await routed(account, surface)
    await value.record()
    before = await value.hook.snapshot()
    capture = await actual_posts(value, surface)
    report = await joined(value, capture)
    native = value.observation.session.provider != "anthropic"
    require_checks(
        report,
        ("FAIL", "POSTED_COST_MISSING") if native else ("PASS", "CONTROLLED_POSTED_COST_MATCH"),
        ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
    )
    # The frozen source explicitly requires a posted dollar amount. The adapter
    # answer/headless return and ledger are never substituted into that text.
    source = cast(
        dict[str, Any],
        yaml.safe_load(
            (
                REPO
                / "packages/testing/fixtures/target53/catalog/scenarios/QA-D11-FOOTER-COST-MATCHES-LEDGER.yaml"
            ).read_text()
        ),
    )
    pattern = source["assert"][1]["pattern"]
    if pattern != r"\d+s.*\$\d+\.\d{2,3}":
        raise RuntimeError("frozen D11 cost requirement changed")
    texts = (
        [t for m in capture.messages for t in (m.content, *m.embed_texts)]
        if isinstance(capture, DiscordSurfaceCapture)
        else [t for m in capture.messages for t in (*m.body_texts, *m.card_texts)]
    )
    if any(re.search(pattern, text) for text in texts) == native:
        raise RuntimeError("frozen posted-cost assertion disagrees with actual adapter")
    if not capture.post_capture_complete or not capture.text_capture_complete:
        raise RuntimeError("executed D11 outcome lacks complete actual capture")
    if (
        report.ledger_usd != Decimal("0.012000")
        or report.controlled_expected_usd != report.ledger_usd
    ):
        raise RuntimeError("controlled native arithmetic was not completely joined")
    if not set(e.evidence_id for e in capture.events) <= set(report.checks[0].evidence_ids):
        raise RuntimeError("accepted actual post receipts were not retained")
    if await value.hook.snapshot() != before:
        raise RuntimeError("read-only footer join changed accounting")
    save_d11_proof(tmp_path, value.observation.session.provider, capture, report, native_sdk=False)


@pytest.mark.parametrize("surface", ["slack", "teams"])
@pytest.mark.parametrize("missing", ["price", "infrastructure", "inventory", "capture", "terminal"])
async def test_incomplete_evidence_stays_typed_pending(
    account: Account, surface: Surface, missing: str
) -> None:
    value = await routed(account, surface)
    await value.record()
    capture = await actual_posts(value, surface)
    rate = turn_rate(value, value.observation)
    changes: dict[str, Any] = {}
    if missing == "price":
        changes["attributions"] = (replace(rate, price=None),)
    elif missing == "infrastructure":
        changes["attributions"] = (
            RevisionRateAttribution(
                observation_id=value.observation.id,
                revision=value.observation.revision,
                price=rate.price,
                rate_evidence_id=rate.rate_evidence_id,
            ),
        )
    elif missing == "inventory":
        changes["inventory"] = None
    elif missing == "capture":
        capture = capture.model_copy(
            update={"post_capture_complete": False, "text_capture_complete": False}
        )
    else:
        changes["terminal"] = None
    report = await joined(value, capture, **changes)
    code = {
        "price": "PRICING_INCOMPLETE",
        "infrastructure": "PRICING_INCOMPLETE",
        "inventory": "TURN_INVENTORY_UNKNOWN",
        "capture": "POST_CAPTURE_INCOMPLETE",
        "terminal": "AUTHORITATIVE_ROOT_TERMINAL_MISSING",
    }[missing]
    require_checks(report, ("PENDING", code), ("PENDING", code))


@pytest.mark.parametrize("surface", ["slack", "teams"])
async def test_body_and_notification_cost_do_not_supply_a_footer(
    account: Account, surface: Surface
) -> None:
    value = await routed(account, surface)
    await value.record()
    capture = await actual_posts(value, surface, answer="FOOTER-OK 0s\u2003\u2003$99.000 used")
    report = await joined(value, capture)
    require_checks(
        report,
        ("FAIL", "POSTED_COST_MISSING")
        if value.observation.session.provider != "anthropic"
        else ("PASS", "CONTROLLED_POSTED_COST_MATCH"),
        ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
    )


@pytest.mark.parametrize("surface", ["slack", "teams"])
async def test_foreign_platform_or_thread_cannot_join_accounting(
    account: Account, surface: Surface
) -> None:
    value = await routed(account, surface)
    await value.record()
    capture = await actual_posts(value, surface)
    if not isinstance(capture, PlatformSurfaceCapture):
        raise RuntimeError("wrong platform fixture")
    foreign = capture.model_copy(
        update={
            "messages": tuple(
                m.model_copy(update={"destination_id": "foreign"}) for m in capture.messages
            )
        }
    )
    require_checks(
        await joined(value, foreign),
        ("FAIL", "POSTED_ROOT_IDENTITY_MISMATCH"),
        ("FAIL", "POSTED_ROOT_IDENTITY_MISMATCH"),
    )


@pytest.mark.parametrize(
    ("url", "skipped"),
    [
        ("postgresql+asyncpg://localhost/daimon_test", True),
        ("postgresql+asyncpg://remote.invalid/daimon_test_nc_N5", False),
    ],
)
def test_footer_admission_precedes_any_engine_fixture(
    tmp_path: Path, url: str, skipped: bool
) -> None:
    trap = tmp_path / "forbidden-fixture-step"
    plugin = tmp_path / "n5_footer_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from daimon.testing import db\n"
        f"trap = Path({str(trap)!r})\n"
        "def forbid(*args, **kwargs):\n"
        "    trap.write_text('connection or destructive fixture reached')\n"
        "    raise RuntimeError('N5_CONNECTION_OR_CLEANUP_TRAP')\n"
        "async def forbid_async(*args, **kwargs):\n"
        "    forbid()\n"
        "def pytest_sessionstart(session):\n"
        "    original = db.build_test_engine\n"
        "    def build(*args, **kwargs):\n"
        "        engine = original(*args, **kwargs)\n"
        "        event.listen(engine.sync_engine, 'do_connect', forbid)\n"
        "        return engine\n"
        "    db.build_test_engine = build\n"
        "    db.sweep_orphan_schemas = forbid_async\n"
        "    db._create_schema_with_tables = forbid_async\n"
        "    db.truncate_all = forbid_async\n"
    )
    environment = dict(os.environ)
    environment["DAIMON_DATABASE__TEST_URL"] = url
    environment["PYTHONPATH"] = str(tmp_path)
    environment.pop("GITHUB_ACTIONS", None)
    environment.pop("CI", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            plugin.stem,
            "-p",
            "no:cacheprovider",
            "-q",
            "-rs",
            f"{Path(controls.__file__).resolve()}::"
            "test_turn_reader_resolves_complete_posted_cost_with_independent_seal[anthropic]",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    if trap.exists() or "N5_CONNECTION_OR_CLEANUP_TRAP" in output:
        raise RuntimeError("footer fixture connected or swept/truncated before admission")
    if skipped:
        if result.returncode != 0 or "1 skipped" not in output or "owned lane" not in output:
            raise RuntimeError(f"contributor database was not skipped before setup: {output}")
    elif result.returncode != 1 or "require owned local lane engines" not in output:
        raise RuntimeError(f"foreign database was not refused before setup: {output}")


def save_d11_proof(
    directory: Path,
    provider: str,
    capture: Capture,
    report: FooterAccountingReport,
    *,
    native_sdk: bool,
    request_count: int = 0,
) -> None:
    source = cast(
        dict[str, Any],
        yaml.safe_load(
            (
                REPO
                / "packages/testing/fixtures/target53/catalog/scenarios/QA-D11-FOOTER-COST-MATCHES-LEDGER.yaml"
            ).read_text()
        ),
    )
    if isinstance(capture, DiscordSurfaceCapture):
        text = "\n".join(
            part for message in capture.messages for part in (message.content, *message.embed_texts)
        )
    else:
        text = "\n".join(
            part
            for message in capture.messages
            for part in (*message.body_texts, *message.card_texts)
        )
    checks = [
        {
            "kind": requirement["kind"],
            "status": "PASS" if re.search(requirement["pattern"], text) else "FAIL",
        }
        for requirement in source["assert"][:2]
    ] + [check.model_dump(mode="json") for check in report.checks]
    if [c["status"] for c in checks] != (
        ["PASS", "PASS", "PASS", "PASS"]
        if provider == "anthropic"
        else ["PASS", "FAIL", "FAIL", "PASS"]
    ):
        raise RuntimeError("actual frozen D11 assertion results differ from the honest A/O/G table")
    (directory / f"n5-d11-{provider}-{getattr(capture, 'platform', 'discord')}.json").write_text(
        json.dumps(
            {
                "provider": provider,
                "profile": "controlled_offline_native_sdk_host"
                if native_sdk
                else "controlled_offline_adapter_component",
                "frozen_assertions": source["assert"],
                "checks": checks,
                "overall": "PASS" if provider == "anthropic" else "FAIL",
                "actual_accepted_posts": capture.model_dump(mode="json"),
                "native_revisions": report.native_revisions,
                "independently_read_ledger_usd": str(report.ledger_usd),
                "controlled_expected_usd": str(report.controlled_expected_usd),
                "request_count": request_count,
                "live_spend_certified": False,
                "source_ingress_certified": False,
                "actual_live_usd": "0",
                "held_live_usd": "0",
            },
            indent=2,
        )
    )
