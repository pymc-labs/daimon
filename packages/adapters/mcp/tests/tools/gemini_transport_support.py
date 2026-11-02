"""Offline Gemini catalog and interaction transport for ordinary agent tests."""

import json
from datetime import UTC, datetime, timedelta

import httpx
from mux.testing.drivers.gemini.fake import FakeTransport


class GeminiCatalog:
    """The Gemini agent catalog: an agent is created, then read back."""

    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []

    def handle(self, call: httpx.Request) -> httpx.Response:
        self.requests.append(call)
        if call.method == "POST" and call.url.path == "/v1beta/agents":
            agent = json.loads(call.content)
            self.agents[agent["id"]] = agent
            return httpx.Response(200, json=agent)
        name = call.url.path.removeprefix("/v1beta/agents/")
        if call.method == "GET" and name in self.agents:
            return httpx.Response(200, json=self.agents[name])
        return httpx.Response(404, json={"error": {"message": "not found"}})


class GeminiTransport(FakeTransport):
    """Gemini interactions: every create completes at once with one answer."""

    async def create(self, request):
        self.requests.append(json.loads(json.dumps(dict(request))))
        # Stamped now: the driver expires a workspace seven days after its last activity.
        created = datetime.now(UTC).replace(microsecond=0)
        response = {
            "id": f"gemini-{len(self.requests)}",
            "status": "completed",
            "created": created.isoformat().replace("+00:00", "Z"),
            "updated": (created + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            "tools": request.get("tools", []),
            "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "gemini says X"}]}
            ],
            # Every stage, as Gemini meters it in production (staging 7aba3054: cached
            # and thought counts reported), so the turn settles; a meter missing one
            # stays pending, and the run's cost would rightly read as "so far".
            "usage": {
                "total_input_tokens": 10,
                "total_cached_tokens": 0,
                "total_output_tokens": 2,
                "total_thought_tokens": 0,
            },
        }
        self.saved[response["id"]] = response
        return json.loads(json.dumps(response))
