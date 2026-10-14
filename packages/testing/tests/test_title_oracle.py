"""Real title captures and false-credit probes; no native completion claims."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from daimon.testing.discord_ingress import DiscordIngressReceipt
from daimon.testing.provider_fixtures import load_provider_fixtures
from daimon.testing.title_oracle import SCENARIOS, TitleVerdict, evaluate_title
from mux.contracts.ids import Provider
from pydantic import ValidationError

from packages.adapters.discord.tests.owned_db_preflight import (
    offline_db_preflight as offline_db_preflight,
)
from packages.adapters.discord.tests.test_gateway_ingress import OwnedIngress, payload, start
from packages.adapters.discord.tests.test_gateway_ingress import owned as owned

DATA = Path(__file__).parents[1] / "fixtures/target53"
CATALOG = DATA / "catalog"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=CATALOG)
TRIGGER = "1525400123000000001"


def judge(name: str, backend: Provider, receipt: DiscordIngressReceipt | None) -> TitleVerdict:
    return evaluate_title(
        PACK,
        catalog_root=CATALOG,
        scenario_id=name,
        backend=backend,
        trigger_message_id=TRIGGER,
        receipt=receipt,
    )


def replace_name(raw: dict[str, Any], name: str) -> None:
    creation = raw["thread_creations"][0]
    creation["name"] = name
    for field in ("request_json", "response_json"):
        row = json.loads(creation[field])
        row["name"] = name
        creation[field] = json.dumps(row)


def mutate(raw: dict[str, Any], kind: str) -> None:
    """Mutation controls operate on actual receipts, then JSON-roundtrip them."""
    gateway = raw["gateway_messages"][0]
    creation = raw["thread_creations"][0]
    if kind in ("gateway_id", "gateway_channel", "gateway_guild", "gateway_text", "author"):
        row = json.loads(gateway["payload_json"])
        if kind == "author":
            row["author"]["id"] = "999"
        else:
            key = {
                "gateway_id": "id",
                "gateway_channel": "channel_id",
                "gateway_guild": "guild_id",
                "gateway_text": "content",
            }[kind]
            row[key] = "foreign"
        gateway["payload_json"] = json.dumps(row)
    elif kind == "gateway_metadata_channel":
        gateway["channel_id"] = "foreign"
    elif kind == "before_trigger":
        creation["observed_s"] = gateway["observed_s"] - 1.0
    elif kind in ("creation_id", "creation_message", "creation_channel"):
        creation[
            {
                "creation_id": "thread_id",
                "creation_message": "message_id",
                "creation_channel": "channel_id",
            }[kind]
        ] = "foreign"
    elif kind in (
        "response_id",
        "response_parent",
        "response_guild",
        "response_name",
        "request_name",
    ):
        field = "request_json" if kind == "request_name" else "response_json"
        row = json.loads(creation[field])
        key = {
            "response_id": "id",
            "response_parent": "parent_id",
            "response_guild": "guild_id",
            "response_name": "name",
            "request_name": "name",
        }[kind]
        row[key] = "foreign"
        creation[field] = json.dumps(row)
    elif kind in ("naming_text", "naming_model", "naming_broken_json"):
        row = json.loads(raw["naming_calls"][0]["request_json"])
        if kind == "naming_text":
            row["messages"][0]["content"] = "<message>\nunrelated\n</message>"
        else:
            row["model"] = "foreign"
        raw["naming_calls"][0]["request_json"] = (
            "{" if kind == "naming_broken_json" else json.dumps(row)
        )
    elif kind in ("meter_session", "meter_user", "meter_model"):
        raw["naming_meters"][0][
            {
                "meter_session": "managed_session_id",
                "meter_user": "platform_user_id",
                "meter_model": "model",
            }[kind]
        ] = "foreign"
    elif kind.startswith("missing_"):
        raw[
            {
                "missing_gateway": "gateway_messages",
                "missing_creation": "thread_creations",
                "missing_dispatch": "dispatched_message_ids",
                "missing_request": "naming_calls",
                "missing_meter": "naming_meters",
            }[kind]
        ] = []
    elif kind.startswith("extra_"):
        key = {
            "extra_gateway": "gateway_messages",
            "extra_creation": "thread_creations",
            "extra_request": "naming_calls",
            "extra_meter": "naming_meters",
            "extra_dispatch": "dispatched_message_ids",
        }[kind]
        raw[key].append(raw[key][0])
    elif kind == "errors":
        raw["errors"] = ["on_message"]
    elif kind == "bare_placeholder":
        replace_name(raw, "qa-thread")
        raw["naming_calls"] = []
        raw["naming_meters"] = []
    else:
        raise ValueError(kind)


MUTATIONS = (
    "gateway_id",
    "gateway_channel",
    "gateway_guild",
    "gateway_text",
    "author",
    "gateway_metadata_channel",
    "before_trigger",
    "creation_id",
    "creation_message",
    "creation_channel",
    "response_id",
    "response_parent",
    "response_guild",
    "response_name",
    "request_name",
    "naming_text",
    "naming_model",
    "naming_broken_json",
    "meter_session",
    "meter_user",
    "meter_model",
    "missing_gateway",
    "missing_creation",
    "missing_dispatch",
    "missing_request",
    "missing_meter",
    "extra_gateway",
    "extra_creation",
    "extra_request",
    "extra_meter",
    "extra_dispatch",
    "errors",
    "bare_placeholder",
)


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("name", SCENARIOS)
async def test_frozen_title_real_gateway_and_roundtrip_probes(
    owned: OwnedIngress,
    backend: Provider,
    name: str,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    scenario = next(row for row in PACK.scenarios if row.scenario_id == name)
    trigger = next(i for i in scenario.invocations if i["operation"] == "host_turn")
    text = trigger["text"]
    assert isinstance(text, str)
    harness = await start(owned, backend)
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
        try:
            if name == SCENARIOS[0]:
                context = next(i for i in scenario.invocations if i["operation"] == "context")
                assert isinstance(context["text"], str)
                harness.deliver(payload(int(TRIGGER) - 1, context["text"], mentioned=False))
            harness.deliver(payload(int(TRIGGER), text))
            await asyncio.wait_for(harness.http.first_post.wait(), timeout=30)
            original = await harness.receipt()
            receipt = DiscordIngressReceipt.model_validate_json(original.model_dump_json())
            result = judge(name, backend, receipt)
            expected = "PENDING" if name == SCENARIOS[0] else "PASS"
            assert result.status == expected
            assert result.whole_cell_status == "PENDING"
            authored = scenario.scenario["assert"]
            assert isinstance(authored, list)
            assert result.assertion == next(
                a for a in authored if isinstance(a, dict) and a.get("kind") == "thread_name"
            )
            assert not any(r.path.startswith("/v1/sessions") for r in owned.native.requests)
            record_testsuite_property(
                f"title:{name}:{backend}",
                json.dumps(
                    {
                        "verdict": result.model_dump(mode="json"),
                        "receipt": receipt.model_dump(mode="json"),
                    }
                ),
            )
            assert judge(name, backend, None).status == "PENDING"
            if name == SCENARIOS[0]:
                assert not receipt.naming_calls
                assert result.actual_thread_name == "Chat with daimon"  # the bot, never the agent
                return
            for kind in MUTATIONS:
                raw = json.loads(receipt.model_dump_json())
                mutate(raw, kind)
                changed = DiscordIngressReceipt.model_validate_json(json.dumps(raw))
                assert judge(name, backend, changed).status == "PENDING", kind
            other: Provider = "gemini" if backend != "gemini" else "openai"
            assert judge(name, other, receipt).status == "PENDING"
            forbidden = "Google accounts sign in" if name == SCENARIOS[1] else "Hello"
            for bad_name, reason in ((forbidden, "FORBIDDEN_TITLE"), ("x" * 61, "TITLE_TOO_LONG")):
                raw = json.loads(receipt.model_dump_json())
                replace_name(raw, bad_name)
                failed = judge(
                    name, backend, DiscordIngressReceipt.model_validate_json(json.dumps(raw))
                )
                assert (failed.status, failed.reason) == ("FAIL", reason)
            raw = json.loads(result.model_dump_json())
            raw["whole_cell_status"] = "PASS"
            with pytest.raises(ValidationError):
                TitleVerdict.model_validate_json(json.dumps(raw))
        finally:
            await harness.close()


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
def test_missing_title_capture_stays_pending(name: str, backend: Provider) -> None:
    result = judge(name, backend, None)
    assert result.status == result.whole_cell_status == "PENDING"


def test_mutated_embedded_assertion_refuses_before_capture() -> None:
    raw = json.loads(PACK.model_dump_json())
    row = next(r for r in raw["scenarios"] if r["scenario_id"] == SCENARIOS[2])
    title = next(a for a in row["scenario"]["assert"] if a["kind"] == "thread_name")
    title["pattern_absent"] = "cannot match"
    from daimon.testing.provider_fixtures import FixturePack

    changed = FixturePack.model_validate_json(json.dumps(raw))
    with pytest.raises(ValueError, match="embedded scenario differs"):
        evaluate_title(
            changed,
            catalog_root=CATALOG,
            scenario_id=SCENARIOS[2],
            backend="anthropic",
            trigger_message_id=TRIGGER,
            receipt=None,
        )
