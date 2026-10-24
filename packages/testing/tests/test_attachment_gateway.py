"""C7 composition controls at the dual-signed a456c7f dependency.

Real gateway -> actual bot/native user input, followed by a separately driven
native mount control. Neither default fake output nor manual mount is cell PASS.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import sentry_sdk
from daimon.testing.attachment_ingestion import attachment_cells, read_attachment
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.output_ownership import FilePin
from mux.testing.drivers.anthropic.ma import FakeMAState, make_fake_memory_store_handler

from packages.adapters.discord.tests.owned_db_preflight import (
    offline_db_preflight as offline_db_preflight,
)
from packages.adapters.discord.tests.test_gateway_ingress import (
    OwnedIngress,
    payload,
    start,
)
from packages.adapters.discord.tests.test_gateway_ingress import (
    owned as owned,
)
from packages.testing.tests.test_attachment_ingestion import (
    CATALOG,
    SCENARIOS,
    attachment_payload,
    mount,
    ref,
    scenario,
)


@pytest.mark.parametrize("name", SCENARIOS)
async def test_frozen_pdf_real_gateway_native_handoff_and_mount_control(
    owned: OwnedIngress,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    row = scenario(name)
    agent = ma_agent(tenant_id=owned.tenant_id, name="qa-agent")
    state = FakeSessionsState(
        ma=FakeMAState(
            agents={agent.id: agent.model_dump(mode="json")},
            agent_versions={agent.id: {agent.version: agent.model_dump(mode="json")}},
        )
    )
    handler = make_fake_sessions_handler(state)
    memory = make_fake_memory_store_handler()
    router = owned.native.router
    assert router is not None
    for method in ("GET", "POST", "PATCH", "DELETE"):
        router.add(method, r"/v1/(sessions|files).*", lambda request, match: handler(request))
        router.add(method, r"/v1/memory_stores.*", lambda request, match: memory(request))

    harness = await start(owned, "anthropic")
    file_payload = attachment_payload(row, channel_id=101)
    invocations = [i for i in row.invocations if i.get("operation") == "host_turn"]
    original = payload(1525400123000000001, str(invocations[0]["text"]))
    original["attachments"] = file_payload["attachments"]
    source = FilePin(path=row.assets[0].path, sha256=row.assets[0].sha256)
    body = source.read(CATALOG)
    url = original["attachments"][0]["url"]
    cdn_reads: list[str] = []

    async def cdn_read(actual_url: str) -> bytes:
        if actual_url != url:
            raise ValueError("foreign attachment download")
        cdn_reads.append(actual_url)
        return body

    monkeypatch.setattr(harness.http, "get_from_cdn", cdn_read, raising=False)
    try:
        with sentry_sdk.isolation_scope() as sentry:
            sentry.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(original)
            await asyncio.wait_for(harness.http.first_post.wait(), 15)
            harness.http.release_first_post.set()
            await asyncio.wait_for(harness.wait(), 30)
        receipt = await harness.receipt()
        assert not receipt.errors and receipt.queue_in_flight == receipt.callbacks_pending == 0
        assert len(state.sessions) == len(state.sent_batches) == 1
        actual_session = next(iter(state.sessions))
        handoff = json.dumps(state.sent_batches)
        assert url in handoff and "[attachment]" in handoff
        assert original["id"] in handoff and source.read(CATALOG) == body
        assert not cdn_reads  # actual PDF ingestion is URL handoff, no eager fetch
        message = next(m for m in harness.bot.cached_messages if str(m.id) == original["id"])
        capture = await read_attachment(
            message,
            attachment_id=105,
            original_payload_json=json.dumps(original),
            source=source,
            catalog_root=CATALOG,
        )
        assert cdn_reads == [url]
        proof = await mount(
            owned.native,
            state,
            capture,
            existing_session=ref("session", actual_session, harness.scope),
            scope=harness.scope,
        )
        assert proof.workspace_bytes.content == body and not proof.scenario_cell_credit
        if len(invocations) == 2:
            followup = payload(
                1525400123000000002, str(invocations[1]["text"]), channel_id=int(original["id"])
            )
            with sentry_sdk.isolation_scope() as sentry:
                sentry.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
                harness.deliver(followup)
                await asyncio.wait_for(harness.wait(), 30)
            assert len(state.sessions) == 1 and len(state.sent_batches) == 2
            assert state.sent_batches[1][0] == state.sent_batches[0][0] == actual_session
            assert state.sandbox[actual_session][proof.mount_path] == body
            assert all(Path(path).name != "qa_canary.txt" for path in state.sandbox[actual_session])
        assert not owned.native.violations
        assert all(cell.status == "PENDING" for cell in attachment_cells(row))
        record_testsuite_property(
            name,
            json.dumps(
                {
                    "classification": "offline_facets_only",
                    "ingress": (await harness.receipt()).model_dump(mode="json"),
                    "mount": proof.model_dump(mode="json"),
                    "cdn_reads": cdn_reads,
                    "native_sent_batches": state.sent_batches,
                    "native_sessions": tuple(state.sessions),
                    "cells": [cell.model_dump(mode="json") for cell in attachment_cells(row)],
                }
            ),
        )
    finally:
        await harness.close()
