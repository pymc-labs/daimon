"""Title-only frozen predicates over the real, single-trigger C7 ingress capture.

The receipt is an offline observation boundary, not a native turn certificate.
No request, no title verdict: even a real bare-mention fallback stays PENDING.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from daimon.core.thread_naming import THREAD_NAMING_MODEL, strip_mentions
from daimon.testing.discord_ingress import DiscordIngressReceipt
from daimon.testing.discord_surface import SurfaceModel
from daimon.testing.provider_fixtures import FixturePack
from daimon.testing.provider_replay import Object
from mux.contracts.ids import Provider
from pydantic import TypeAdapter

SCENARIOS = (
    "QA-I6-BARE-MENTION-THREAD-TITLE",
    "QA-NEW8-LINK-PAGE-THREAD-TITLE",
    "QA-NEW9-NO-INTERNAL-PLUMBING",
)


class TitleVerdict(SurfaceModel):
    scenario_id: str
    backend: Provider
    boundary: Literal["discord_ingress_title"] = "discord_ingress_title"
    status: Literal["PASS", "FAIL", "PENDING"]
    reason: Literal[
        "SOURCE_PREDICATE_SATISFIED",
        "FORBIDDEN_TITLE",
        "TITLE_TOO_LONG",
        "CAPTURE_UNBOUND",
        "NAMING_REQUEST_UNBOUND",
    ]
    # The original YAML assertion is copied intact, including regex escapes.
    assertion: Object
    actual_thread_name: str | None = None
    trigger_message_id: str
    whole_cell_status: Literal["PENDING"] = "PENDING"
    whole_cell_reason: Literal["REMAINING_FROZEN_ASSERTIONS_UNBOUND"] = (
        "REMAINING_FROZEN_ASSERTIONS_UNBOUND"
    )


def evaluate_title(
    pack: FixturePack,
    *,
    catalog_root: Path,
    scenario_id: str,
    backend: Provider,
    trigger_message_id: str,
    receipt: DiscordIngressReceipt | None,
) -> TitleVerdict:
    """Judge only a source-bound title facet; never accept caller-declared titles.

    Supports a fresh single-trigger capture. Extra naming calls/meters/threads,
    foreign identities and malformed wire snapshots refuse facet credit. Re-run
    after JSON deserialization; no in-memory identity token authorizes PASS.
    """
    pack.target.verify(catalog_root)
    if scenario_id not in SCENARIOS:
        raise ValueError("not a title oracle scenario")
    scenario = next(row for row in pack.scenarios if row.scenario_id == scenario_id)
    # Revalidate the selected frozen source at every assertion, without reading
    # the other 52 scenarios for each mutation probe or title-only observation.
    source_path = (catalog_root / scenario.source.path).resolve()
    if (
        not source_path.is_relative_to(catalog_root.resolve())
        or scenario.source.path != scenario.projection.path
    ):
        raise ValueError("title source pin escapes or differs from its projection")
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() not in (
        scenario.source.sha256,
        scenario.projection.sha256,
    ):
        raise ValueError("title source pin changed")
    document = TypeAdapter[Object](Object).validate_python(yaml.safe_load(source_bytes))
    document.pop("sources", None)
    if document != scenario.scenario:
        raise ValueError("embedded scenario differs from pinned catalog bytes")
    assertions = scenario.scenario.get("assert")
    if not isinstance(assertions, list):
        raise ValueError("frozen assertions are missing")
    titles = [a for a in assertions if isinstance(a, dict) and a.get("kind") == "thread_name"]
    if len(titles) != 1:
        raise ValueError("expected one frozen thread_name assertion")
    assertion = titles[0]
    if set(assertion) != {"kind", "turn", "pattern_absent", "max_len"}:
        raise ValueError("unsupported authored title predicate")
    pattern, maximum = assertion["pattern_absent"], assertion["max_len"]
    if not isinstance(pattern, str) or type(maximum) is not int or assertion["turn"] != 1:
        raise ValueError("unsupported authored title predicate")
    steps = scenario.scenario.get("steps")
    if not isinstance(steps, list):
        raise ValueError("frozen source steps are missing")
    # Count actual authored host mentions; context messages are not host turns.
    triggers = [i for i in steps if isinstance(i, dict) and i.get("do") == "mention"]
    if len(triggers) != 1:
        raise ValueError("unsupported source trigger window")
    source_text = triggers[0].get("text")
    if not isinstance(source_text, str):
        raise ValueError("original trigger text is absent")

    def verdict(
        status: Literal["PASS", "FAIL", "PENDING"],
        reason: Literal[
            "SOURCE_PREDICATE_SATISFIED",
            "FORBIDDEN_TITLE",
            "TITLE_TOO_LONG",
            "CAPTURE_UNBOUND",
            "NAMING_REQUEST_UNBOUND",
        ],
        name: str | None = None,
    ) -> TitleVerdict:
        return TitleVerdict(
            scenario_id=scenario_id,
            backend=backend,
            status=status,
            reason=reason,
            assertion=assertion,
            actual_thread_name=name,
            trigger_message_id=trigger_message_id,
        )

    if (
        receipt is None
        or receipt.errors
        or receipt.backend_revision.backend != backend
        or receipt.backend_revision.channel.platform != "discord"
        or receipt.backend_revision.channel.channel_id != receipt.parent_channel_id
    ):
        return verdict("PENDING", "CAPTURE_UNBOUND")
    messages = [m for m in receipt.gateway_messages if m.message_id == trigger_message_id]
    if (
        len(messages) != 1
        or receipt.dispatched_message_ids.count(trigger_message_id) != 1
        or len(receipt.thread_creations) != 1
    ):
        return verdict("PENDING", "CAPTURE_UNBOUND")
    message, creation = messages[0], receipt.thread_creations[0]
    try:
        raw = json.loads(message.payload_json)
        request = json.loads(creation.request_json)
        response = json.loads(creation.response_json)
        if (
            message.channel_id != receipt.parent_channel_id
            or str(raw["id"]) != trigger_message_id
            or str(raw["guild_id"]) != receipt.guild_id
            or str(raw["channel_id"]) != receipt.parent_channel_id
            or strip_mentions(raw["content"]) != source_text
            or raw["attachments"]
            or raw["author"].get("bot", False)
            or creation.message_id != trigger_message_id
            or creation.thread_id != trigger_message_id
            or creation.channel_id != receipt.parent_channel_id
            or creation.observed_s < message.observed_s
            or request["name"] != creation.name
            or response["name"] != creation.name
            or str(response["id"]) != trigger_message_id
            or str(response["parent_id"]) != receipt.parent_channel_id
            or str(response["guild_id"]) != receipt.guild_id
        ):
            return verdict("PENDING", "CAPTURE_UNBOUND")
        if len(receipt.naming_calls) != 1 or len(receipt.naming_meters) != 1:
            return verdict("PENDING", "NAMING_REQUEST_UNBOUND", creation.name)
        meter = receipt.naming_meters[0]
        naming = json.loads(receipt.naming_calls[0].request_json)
        if (
            not source_text
            or naming["model"] != THREAD_NAMING_MODEL
            or naming["messages"]
            != [{"role": "user", "content": f"<message>\n{source_text}\n</message>"}]
            or meter.managed_session_id != f"thread-naming:{trigger_message_id}"
            or meter.platform_user_id != str(raw["author"]["id"])
            or meter.model != THREAD_NAMING_MODEL
        ):
            return verdict("PENDING", "NAMING_REQUEST_UNBOUND", creation.name)
    except (KeyError, TypeError, ValueError, AttributeError):
        return verdict("PENDING", "CAPTURE_UNBOUND")
    if len(creation.name) > maximum:
        return verdict("FAIL", "TITLE_TOO_LONG", creation.name)
    if re.search(pattern, creation.name):
        return verdict("FAIL", "FORBIDDEN_TITLE", creation.name)
    return verdict("PASS", "SOURCE_PREDICATE_SATISFIED", creation.name)
