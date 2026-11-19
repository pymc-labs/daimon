"""The OpenAI fake serves every request the real API accepted in the saved live recordings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.testing.sim.openai import OpenAIAPI

CORPUS = Path(__file__).resolve().parents[2] / "packages/mux/tests/fixtures/openai/recordings"
#: What a recorded body's field names were filled with; the recordings keep names only.
_VALUES: dict[str, Any] = {
    "name": "recorded",
    "model": "gpt-6-luna",
    "metadata": {"daimon": "recorded"},
    "environment": {"type": "none"},
}


@pytest.mark.parametrize("fixture", ["C10", "C15", "C16"])
async def test_the_fake_answers_a_recorded_live_run_request_for_request(fixture: str) -> None:
    fake = OpenAIAPI()
    ids = {"resource-1": "", "resource-2": ""}
    deleted = False
    for batch in json.loads((CORPUS / f"{fixture}.json").read_text())["batches"]:
        request = batch["request"]
        path = request["path"]
        for recorded, real in ids.items():
            path = path.replace(recorded, real)
        body = {name: _VALUES.get(name) for name in request["body_fields"]}
        if "agent_id" in body:
            body["agent_id"] = ids["resource-1"]
        response = await fake.handle(
            httpx.Request(request["method"], f"https://{fake.host}{path}", json=body or None)
        )
        # The recorded cleanup retried a session the API had not yet let go; once the
        # fake has deleted it, it answers 404 as the API does for a gone session.
        gone = deleted and ids["resource-2"] in path
        assert response.status_code < 300 or (gone and response.status_code == 404), (
            request["method"],
            path,
            response.text,
        )
        deleted = deleted or (request["method"] == "DELETE" and "/sessions/" in path)
        if request["method"] == "POST" and request["path"] == "/v1/agents":
            ids["resource-1"] = response.json()["id"]
        if request["method"] == "POST" and request["path"] == "/v1/agents/sessions":
            ids["resource-2"] = response.json()["id"]
    assert not fake.violations, fake.violations
