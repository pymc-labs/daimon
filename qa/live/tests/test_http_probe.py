from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.http_probe import check_http
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Pending


@pytest.fixture
def endpoint() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.headers.get("Authorization") is None
            self.send_response(404 if self.path == "/missing" else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"x" * 65537 if self.path == "/large" else b"<html>Missing page</html>"
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def test_http_checks_status_mime_and_complete_body(endpoint: str) -> None:
    assert check_http(
        Assertion(
            kind="http_check",
            url=endpoint + "/missing",
            expect_status=404,
            expect_content_type="text/html",
            body_absent=r'\{"detail"',
        )
    )[0]
    assert not check_http(
        Assertion(kind="http_check", url=endpoint + "/missing", expect_status=200)
    )[0]
    assert not check_http(
        Assertion(kind="http_check", url=endpoint + "/ok", body_absent="Missing")
    )[0]
    with pytest.raises(Pending, match="bounded"):
        check_http(Assertion(kind="http_check", url=endpoint + "/large", body_absent="detail"))
    with pytest.raises(Pending, match="unauthenticated"):
        check_http(
            Assertion(kind="http_check", url="https://name:password@example.com", expect_status=200)
        )


def test_http_only_headless_never_creates_discord_turn(
    endpoint: str,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    scenario.tier = "full"
    scenario.surface = "headless"
    scenario.setup = []
    scenario.steps = []
    scenario.teardown = []
    scenario.est_turns = 0
    scenario.assertions = [Assertion(kind="http_check", url=endpoint + "/ok", expect_status=200)]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PASS"
    assert backend.events == [] and result.turns == [] and ledger._today() == 0
