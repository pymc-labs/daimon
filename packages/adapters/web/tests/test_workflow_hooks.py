"""A webhook trigger as the Triggers tab and a sender use it: rotate, deliver, preview,
replay and Listen, through `/api/workflows/*` and the public `/hooks/w/*`."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core.workflows import triggers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import PUBLIC_URL, WebClientFactory
from .test_workflows_routes import Rig

SPEC: dict[str, Any] = {
    "schema": "daimon.workflow/v1",
    "name": "Form",
    "start": "done",
    "inputs": {"type": "object", "properties": {"n": {"type": "integer"}}},
    "triggers": [
        {
            "id": "form",
            "kind": "webhook",
            "verify": "standard-webhooks",
            "inputs": "${{ payload.data }}",
        }
    ],
    "nodes": {"done": {"kind": "finish"}},
}


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client)


def signed(secret: str, payload: dict[str, Any], msg_id: str) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    stamp = str(int(datetime.now(UTC).timestamp()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    headers = {
        "content-type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": stamp,
        "webhook-signature": f"v1,{base64.b64encode(mac).decode()}",
    }
    return {"content": body, "headers": headers}


async def test_a_signed_webhook_starts_a_run_and_the_tab_previews_replays_and_listens(
    rig: Rig,
) -> None:
    keys = {"keys": [Fernet.generate_key().decode()]}
    async with rig.open(features={"workflow_webhooks": "on"}, crypto=keys) as r:
        made = await r.make()
        base = f"/api/workflows/{made['id']}"
        saved = await r.client.put(f"{base}/draft", json={"base_revision": 1, "spec": SPEC})
        assert saved.status_code == 200, saved.text
        published = await r.client.post(f"{base}/draft/publish", json={})
        assert published.status_code == 200, published.text
        hook = f"{base}/triggers/form"

        info = (await r.client.get(f"{hook}/webhook")).json()
        assert info["live_url"].startswith(f"{PUBLIC_URL}/hooks/w/"), info
        assert info["test_url"] == f"{info['live_url']}/test" and info["secret_hint"] is None
        made_secret = await r.client.post(f"{hook}/rotate", json={"overlap": "none"})
        assert made_secret.status_code == 200, made_secret.text
        secret = made_secret.json()["secret"]
        info = (await r.client.get(f"{hook}/webhook")).json()
        assert info["secret_hint"] == f"whsec_...{secret[-4:]}", "only the hint is shown again"
        assert secret not in json.dumps(info), "the secret is never shown again"

        path = info["live_url"].removeprefix(PUBLIC_URL)
        sent = await r.client.post(path, **signed(secret, {"data": {"n": 3}}, "msg_1"))
        assert sent.status_code == 202, sent.text
        assert sent.headers["cache-control"] == "no-store", "never cached"
        forged = signed("whsec_" + base64.b64encode(b"x" * 32).decode(), {"data": {}}, "msg_2")
        assert (await r.client.post(path, **forged)).status_code == 401, "a forged one fails"
        too_big = await r.client.post(path, content=b"x" * (2 << 20))
        assert too_big.status_code == 413, "a body over the cap is refused unread"
        assert await triggers.start_from_events(r.factory, r.settings, now=datetime.now(UTC)) == 1

        rows = (await r.client.get(f"{hook}/deliveries")).json()
        assert [(d["outcome"], d["status"]) for d in rows] == [("rejected", 401), ("accepted", 202)]
        accepted = rows[1]
        assert accepted["run"]["number"] == 1 and accepted["payload"] == {"data": {"n": 3}}, rows

        preview = await r.client.post(
            f"{hook}/preview", json={"inputs": "${{ payload.data.n }}", "delivery": accepted["id"]}
        )
        assert preview.json() == {"ok": True, "value": 3}, preview.text
        bad = await r.client.post(
            f"{hook}/preview", json={"inputs": "${{ payload.nope.n }}", "delivery": accepted["id"]}
        )
        assert bad.json()["ok"] is False, bad.text

        replayed = await r.client.post(f"{hook}/deliveries/{accepted['id']}/replay", json={})
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()["run"]["number"] == 2, "a replay is a new run"

        listening = (await r.client.post(f"{hook}/listen", json={})).json()
        assert listening["listening_until"] is not None, listening
        test_path = listening["test_url"].removeprefix(PUBLIC_URL)
        captured = await r.client.post(test_path, **signed(secret, {"data": {"n": 1}}, "msg_3"))
        assert captured.json()["delivery"].startswith("dlv_test_"), captured.text
        stopped = (await r.client.get(f"{hook}/webhook")).json()
        assert stopped["listening_until"] is None, "one capture closes the test URL"
