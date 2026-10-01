"""End-to-end walk of publishing a report and reading it, against a real
deployment.

Opt-in only (``-m contract``), never a CI gate. Requires four environment
variables naming a real running stack:

- ``DAIMON_TEST_REPORT_MCP_URL`` — the seam's MCP endpoint (the daimon-mcp
  server that exposes the ``publish_report``/``delete_report`` tools), e.g.
  ``https://<host>/mcp``.
- ``DAIMON_TEST_REPORT_MCP_BEARER`` — a platform-user MCP bearer token,
  scoped to one tenant, authorized to call those two tools in that tenant.
- ``DAIMON_TEST_REPORT_HOST_URL`` — the report host's own base URL (the
  reader-facing app the tool's ``upload_url`` and recipient links point
  into), e.g. ``https://<host>``.
- ``DAIMON_TEST_REPORT_TENANT_ID`` — the tenant the bearer above is scoped
  to. Used only to build a readable, unique slug for this run; never sent
  to the tool (the tool resolves the caller's tenant from the bearer).

Run it explicitly: ``uv run pytest -m contract tests/integration/test_report_flow.py``.
With any of the four unset, it skips (never errors) naming what's missing.

Walks: publish through the tool, upload a small real archive, read the
viewer state, ask a grounded question, ask a follow-up in the same thread,
ask something the bundle cannot answer, confirm the second recipient sees
none of the first's threads, then delete the report and confirm both links
stop working. Cleanup (``delete_report``) runs on the failure path as well
as the success path via a ``try``/``finally``, and the slug carries a random
suffix so two concurrent runs never collide.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import tarfile
import time
from dataclasses import dataclass
from typing import cast

import httpx
import pytest
from fastmcp import Client

pytestmark = pytest.mark.contract

_ENV_MCP_URL = "DAIMON_TEST_REPORT_MCP_URL"
_ENV_MCP_BEARER = "DAIMON_TEST_REPORT_MCP_BEARER"
_ENV_REPORT_HOST_URL = "DAIMON_TEST_REPORT_HOST_URL"
_ENV_TENANT_ID = "DAIMON_TEST_REPORT_TENANT_ID"
_REQUIRED_ENV_VARS = (_ENV_MCP_URL, _ENV_MCP_BEARER, _ENV_REPORT_HOST_URL, _ENV_TENANT_ID)

# The one number this walk's grounded question resolves against, planted in
# the tiny bundle's own table — not sourced from any real report.
_KNOWN_TOTAL_VISITS = 28155

_POLL_INTERVAL_SECONDS = 3.0
_TURN_TIMEOUT_SECONDS = 600.0

_REFUSAL_MARKERS = (
    "not in",
    "isn't in",
    "don't have",
    "does not cover",
    "doesn't cover",
    "no information",
    "not covered",
    "can't answer",
    "cannot answer",
    "not able to find",
    "not something",
    "outside",
)


@dataclass(frozen=True)
class _FlowEnv:
    mcp_url: str
    mcp_bearer: str
    report_host_url: str
    tenant_id: str


def _flow_env() -> _FlowEnv:
    """Read the four required env vars, or skip cleanly naming what's missing."""
    missing = [name for name in _REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        pytest.skip(f"missing env vars for the report flow test: {', '.join(missing)}")
    return _FlowEnv(
        mcp_url=os.environ[_ENV_MCP_URL],
        mcp_bearer=os.environ[_ENV_MCP_BEARER],
        report_host_url=os.environ[_ENV_REPORT_HOST_URL].rstrip("/"),
        tenant_id=os.environ[_ENV_TENANT_ID],
    )


def _build_bundle_archive() -> bytes:
    """One gzip archive: a minimal PDF at the root, a readme, a manifest, and
    one table carrying a single known number the grounded question resolves
    against."""
    manifest = {
        "report": "report-flow contract test bundle (synthetic)",
        "tables": {"headline": "tables/headline.json"},
    }
    headline = {"total_visits": _KNOWN_TOTAL_VISITS}
    readme = (
        "# report-flow contract test bundle\n\n"
        "Answer only from tables/headline.json. It holds one number: "
        "total_visits. Quote the file and the key when you state it.\n"
    )
    members: list[tuple[str, bytes]] = [
        ("report.pdf", b"%PDF-1.4 minimal contract-test report\n%%EOF"),
        ("README.md", readme.encode()),
        ("manifest.json", json.dumps(manifest).encode()),
        ("tables/headline.json", json.dumps(headline).encode()),
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _get_state(
    http: httpx.AsyncClient, report_host_url: str, slug: str, token: str
) -> dict[str, object]:
    resp = await http.get(f"{report_host_url}/api/{slug}/state", params={"k": token})
    resp.raise_for_status()
    return _as_mapping(resp.json())


async def _ask(
    http: httpx.AsyncClient,
    report_host_url: str,
    slug: str,
    token: str,
    message: str,
    thread_id: int | None,
) -> int:
    resp = await http.post(
        f"{report_host_url}/api/{slug}/ask",
        params={"k": token},
        json={"message": message, "thread": thread_id},
    )
    resp.raise_for_status()
    return _as_int(_as_mapping(resp.json())["thread"])


async def _poll_thread_until_settled(
    http: httpx.AsyncClient,
    report_host_url: str,
    slug: str,
    token: str,
    thread_id: int,
    after: int,
) -> list[dict[str, object]]:
    """Poll the thread until it stops running, returning every message seen."""
    deadline = time.monotonic() + _TURN_TIMEOUT_SECONDS
    seen: list[dict[str, object]] = []
    last_id = after
    while True:
        resp = await http.get(
            f"{report_host_url}/api/{slug}/threads/{thread_id}",
            params={"k": token, "after": last_id},
        )
        resp.raise_for_status()
        body = _as_mapping(resp.json())
        messages = _as_list(body["messages"])
        for raw_message in messages:
            message = _as_mapping(raw_message)
            seen.append(message)
            last_id = _as_int(message["id"])
        if body["status"] != "running":
            return seen
        assert time.monotonic() < deadline, (
            f"thread {thread_id} did not settle within {_TURN_TIMEOUT_SECONDS}s"
        )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _last_assistant_text(messages: list[dict[str, object]]) -> str:
    assistant_messages = [m for m in messages if m["role"] != "user"]
    assert assistant_messages, "expected at least one non-user message after asking"
    return _as_str(assistant_messages[-1]["text"])


def _as_mapping(value: object) -> dict[str, object]:
    """Narrow an ``object`` known (by the wire contract) to be a JSON object.

    A plain ``isinstance(value, dict)`` leaves pyright with ``dict[Unknown,
    Unknown]`` because the source is a decoded JSON value (``Any``) — every
    JSON object this test reads is host-defined and dynamically shaped, so a
    single narrowing cast here, verified by the isinstance check on the line
    above it, is what lets every caller work with a precisely typed
    ``dict[str, object]`` instead of re-deriving this cast at every call site.
    """
    assert isinstance(value, dict), f"expected a JSON object, got {value!r}"
    return cast(dict[str, object], value)


def _as_list(value: object) -> list[object]:
    """Narrow an ``object`` known to be a JSON array. See ``_as_mapping``."""
    assert isinstance(value, list), f"expected a JSON array, got {value!r}"
    return cast(list[object], value)


def _as_str(value: object) -> str:
    assert isinstance(value, str), f"expected a string, got {value!r}"
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, int | float | str), f"expected a number, got {value!r}"
    return float(value)


def _as_int(value: object) -> int:
    assert isinstance(value, int | str), f"expected an integer, got {value!r}"
    return int(value)


async def test_report_flow_publish_upload_ask_isolate_delete() -> None:
    env = _flow_env()
    slug = f"flow-test-{secrets.token_hex(4)}"

    async with (
        Client(env.mcp_url, auth=env.mcp_bearer) as mcp_client,
        httpx.AsyncClient(timeout=30.0) as http,
    ):
        # Step 1: publish through the tool.
        publish_result = await mcp_client.call_tool(
            "publish_report",
            {
                "slug": slug,
                "title": "Report flow contract test",
                "recipients": [
                    {"name": "Ada", "label": "ada@example.com"},
                    {"name": "Ben", "label": "ben@example.com"},
                ],
                "cap_usd": "1.00",
            },
        )
        publish_data = _as_mapping(publish_result.data)

        try:
            # Step 2: the response carries an upload URL and one per-recipient
            # link, and the two links differ.
            upload_url = _as_str(publish_data["upload_url"])
            links = _as_mapping(publish_data["links"])
            assert upload_url, "publish must return an upload URL"
            assert set(links) == {"Ada", "Ben"}, "publish must return one link per recipient"
            assert links["Ada"] != links["Ben"], "recipient links must not be interchangeable"
            ada_link = _as_str(links["Ada"])
            ben_link = _as_str(links["Ben"])
            ada_token = _as_str(httpx.URL(ada_link).params["k"])
            ben_token = _as_str(httpx.URL(ben_link).params["k"])

            # Step 3: build a small archive and PUT it to the upload URL.
            archive_bytes = _build_bundle_archive()
            upload_resp = await http.put(upload_url, content=archive_bytes)
            upload_resp.raise_for_status()

            # Step 4: the upload response carries the per-recipient links.
            upload_body = _as_mapping(upload_resp.json())
            upload_links = _as_mapping(upload_body["links"])
            assert set(upload_links) >= {"Ada", "Ben"}, (
                "the publish upload response must carry a link for every recipient"
            )

            # Step 5: read the first recipient's state — report present, PDF
            # served, budget shows the cap and no spend yet.
            state = await _get_state(http, env.report_host_url, slug, ada_token)
            current_pdf = _as_str(state["current_pdf"])
            assert current_pdf, "state must name a current PDF after upload"
            pdf_resp = await http.get(
                f"{env.report_host_url}/files/{slug}/{current_pdf}",
                params={"k": ada_token},
            )
            pdf_resp.raise_for_status()
            assert pdf_resp.content.startswith(b"%PDF"), "the served file must be the uploaded PDF"
            budget = _as_mapping(state["budget"])
            assert _as_float(budget["spent_usd"]) == 0.0, "no spend before any question is asked"
            assert _as_float(budget["cap_usd"]) == 1.0, "the cap must be the one published"

            # Step 6: ask a grounded question; poll until settled; the answer
            # names the known number and the file it came from.
            thread_id = await _ask(
                http,
                env.report_host_url,
                slug,
                ada_token,
                "What is total_visits, and which file does it come from?",
                None,
            )
            first_messages = await _poll_thread_until_settled(
                http, env.report_host_url, slug, ada_token, thread_id, after=0
            )
            first_answer = _last_assistant_text(first_messages)
            assert str(_KNOWN_TOTAL_VISITS) in first_answer, (
                f"grounded answer must cite {_KNOWN_TOTAL_VISITS}, got: {first_answer!r}"
            )
            assert "headline" in first_answer.lower(), (
                f"grounded answer must name the file it came from, got: {first_answer!r}"
            )
            last_seen_id = _as_int(first_messages[-1]["id"])

            # Step 7: a follow-up in the same thread does not settle
            # instantly with nothing, and its answer belongs to the second
            # question (the prototype's bug: a stale terminal line reread
            # from the previous turn).
            await _ask(
                http,
                env.report_host_url,
                slug,
                ada_token,
                "Say that number again, spelled out in words.",
                thread_id,
            )
            follow_up_messages = await _poll_thread_until_settled(
                http, env.report_host_url, slug, ada_token, thread_id, after=last_seen_id
            )
            assert follow_up_messages, "a follow-up must produce at least one new message"
            follow_up_answer = _last_assistant_text(follow_up_messages)
            assert follow_up_answer != first_answer, (
                "the follow-up's answer must not be the first question's stale answer"
            )
            last_seen_id = _as_int(follow_up_messages[-1]["id"])

            # Step 8: a question the bundle cannot answer gets a refusal, not
            # an invented figure.
            await _ask(
                http,
                env.report_host_url,
                slug,
                ada_token,
                "What was the customer satisfaction score for the sales team?",
                thread_id,
            )
            refusal_messages = await _poll_thread_until_settled(
                http, env.report_host_url, slug, ada_token, thread_id, after=last_seen_id
            )
            refusal_answer = _last_assistant_text(refusal_messages).lower()
            assert any(marker in refusal_answer for marker in _REFUSAL_MARKERS), (
                f"out-of-bundle question must be refused, not answered with an invented "
                f"figure, got: {refusal_answer!r}"
            )

            # Step 9: spend is now real, positive, and under the cap.
            state_after = await _get_state(http, env.report_host_url, slug, ada_token)
            budget_after = _as_mapping(state_after["budget"])
            spent = _as_float(budget_after["spent_usd"])
            cap = _as_float(budget_after["cap_usd"])
            assert 0.0 < spent < cap, f"spend must be positive and under cap, got {spent} of {cap}"

            # Step 10: the second recipient sees the report but none of the
            # first recipient's threads.
            ben_state = await _get_state(http, env.report_host_url, slug, ben_token)
            assert _as_str(ben_state["current_pdf"]) == current_pdf, (
                "both recipients must see the same published report"
            )
            assert ben_state["threads"] == [], (
                "a recipient must never see another recipient's threads"
            )
        finally:
            # Step 11: delete through the tool; both links stop working.
            # Runs on the failure path as well as the success path.
            await mcp_client.call_tool("delete_report", {"slug": slug})

        ada_state_resp = await http.get(
            f"{env.report_host_url}/api/{slug}/state", params={"k": ada_token}
        )
        assert ada_state_resp.status_code == httpx.codes.FORBIDDEN, (
            "the first recipient's link must stop working after delete"
        )
        ben_state_resp = await http.get(
            f"{env.report_host_url}/api/{slug}/state", params={"k": ben_token}
        )
        assert ben_state_resp.status_code == httpx.codes.FORBIDDEN, (
            "the second recipient's link must stop working after delete"
        )
