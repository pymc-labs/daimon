"""One canary set through every redaction sink: zero canaries in any output.

Sinks: the Sentry pipeline (production `init_sentry`, real SDK, in-memory
transport), the production JSON structlog chain, the CLI structlog chain,
stdlib logging through `install_log_redaction` (uvicorn error and access
records, a library logger with its own rich handler like fastmcp's), and
the CLI's error output (`run_cli` and the `daimon` entry point).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
import sentry_sdk
import structlog
import typer
from anthropic import APIStatusError
from daimon.adapters.cli import main as cli_main
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.logging import configure_bootstrap_logging
from daimon.core.errors import StoreError
from daimon.core.logging_setup import configure_log_level
from daimon.core.observability import (
    capture_exception_with_scope,
    init_sentry,
    install_log_redaction,
)
from daimon.testing.secret_shapes import FIELD_SHAPES, TEXT_SHAPES, new_canary, plain
from rich.console import Console
from rich.logging import RichHandler
from sentry_sdk.transport import Transport

TextSink = Callable[[str], str]


class _Transport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[object] = []

    def capture_envelope(self, envelope: object) -> None:
        for item in envelope.items:  # type: ignore[attr-defined]
            self.payloads.append(item.payload.json or item.payload.bytes)


@pytest.fixture
def sentry_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Transport]:
    transport = _Transport()
    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda *a, **k: real_init(*a, transport=transport, **k))
    init_sentry(
        dsn="https://public@o0.ingest.sentry.io/0",
        environment="test",
        process="mcp",
        release=None,
        traces_sample_rate=1.0,
        integrations=[],
    )
    try:
        yield transport
    finally:
        sentry_sdk.flush()
        sentry_sdk.init()


def _raise(text: str) -> None:
    raise RuntimeError(text)


def _sink_sentry(transport: _Transport, text: str) -> str:
    transport.payloads.clear()
    try:
        _raise(text)
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.capture_message(f"note: {text}")
    sentry_sdk.flush()
    assert transport.payloads, "the events were captured"
    return repr(transport.payloads)


def _sink_structlog(
    configure: Callable[[], None], capsys: pytest.CaptureFixture[str], text: str
) -> str:
    capsys.readouterr()
    configure()
    log = structlog.get_logger("daimon.test")
    try:
        _raise(text)
    except RuntimeError:
        log.exception("op.failed", note=text)
    out = capsys.readouterr()
    rendered = out.out + out.err
    assert "op.failed" in rendered
    return rendered


def _sink_stdlib(name: str, handler: logging.Handler, text: str) -> str:
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    try:
        install_log_redaction()
        try:
            _raise(text)
        except RuntimeError:
            logger.exception("Exception in application: %s", text)
    finally:
        logger.removeHandler(handler)
    return ""


def _sink_access(text: str) -> str:
    install_log_redaction()
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/" + text.replace(" ", "%20"), "1.1", 500),
        None,
    )
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    return record.getMessage()


def _sink_run_cli(text: str) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=400)

    async def _domain() -> None:
        raise StoreError(text)

    async def _upstream() -> None:
        request = httpx.Request("GET", "https://api.example")
        raise APIStatusError(text, response=httpx.Response(400, request=request), body=None)

    for coro in (_domain(), _upstream()):
        with pytest.raises(typer.Exit):
            run_cli(coro, console=console)
    return buffer.getvalue()


def _sink_cli_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> str:
    def _app() -> None:
        _raise(text)

    monkeypatch.setattr(cli_main, "app", _app)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main()
    assert exit_info.value.code == 1
    out = capsys.readouterr()
    rendered = out.out + out.err
    assert "RuntimeError" in rendered
    return rendered


def _assert_clean(rendered: str, canary: str, sink: str) -> None:
    assert canary not in rendered, f"{sink} leaked the canary"
    assert plain(canary) not in rendered, f"{sink} leaked the canary"


@pytest.mark.parametrize("shape", sorted(TEXT_SHAPES))
def test_text_shape_leaks_through_no_sink(
    shape: str,
    sentry_transport: _Transport,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsys = capfd
    canary = new_canary()
    text = TEXT_SHAPES[shape](canary)

    _assert_clean(_sink_sentry(sentry_transport, text), canary, "sentry")
    _assert_clean(
        _sink_structlog(lambda: configure_log_level("INFO"), capsys, text), canary, "structlog json"
    )
    _assert_clean(
        _sink_structlog(configure_bootstrap_logging, capsys, text), canary, "structlog cli"
    )

    stream = io.StringIO()
    plain_handler = logging.StreamHandler(stream)
    _sink_stdlib("uvicorn.error", plain_handler, text)
    _assert_clean(stream.getvalue(), canary, "stdlib uvicorn.error")

    capfd.readouterr()
    _sink_stdlib(
        "fastmcp.server", RichHandler(rich_tracebacks=True, tracebacks_show_locals=True), text
    )
    fd = capfd.readouterr()
    _assert_clean(fd.out + fd.err, canary, "fastmcp rich handler")

    _assert_clean(_sink_access(text), canary, "uvicorn access")
    _assert_clean(_sink_run_cli(text), canary, "run_cli")
    _assert_clean(_sink_cli_main(monkeypatch, capsys, text), canary, "daimon entry point")


@pytest.mark.parametrize("shape", sorted(FIELD_SHAPES))
def test_field_shape_leaks_through_no_log_chain(
    shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = new_canary()
    fields = FIELD_SHAPES[shape](canary)

    for name, configure in (
        ("json", lambda: configure_log_level("INFO")),
        ("cli", configure_bootstrap_logging),
    ):
        capsys.readouterr()
        configure()
        structlog.get_logger("daimon.test").warning(
            "credential.failed", token_count=42, session_id="ordinary-id", **fields
        )
        out = capsys.readouterr()
        rendered = out.out + out.err
        assert "credential.failed" in rendered and "ordinary-id" in rendered
        _assert_clean(rendered, canary, f"structlog {name}")


def test_ordinary_text_survives_every_sink(
    sentry_transport: _Transport,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "agent ada finished: 3 files, max_tokens=4096, exit code=1"

    assert text in _sink_sentry(sentry_transport, text)
    assert text in _sink_structlog(lambda: configure_log_level("INFO"), capsys, text)
    assert text in _sink_run_cli(text)
