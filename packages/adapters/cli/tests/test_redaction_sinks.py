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
    def _app(**_kwargs: object) -> None:
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


def _sink_last_resort(capfd: pytest.CaptureFixture[str], text: str) -> str:
    """No handler anywhere on the path: the record reaches logging.lastResort."""
    install_log_redaction()
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    logger = logging.getLogger("aiohttp.orphan_test")
    logger.propagate = True
    try:
        capfd.readouterr()
        try:
            _raise(text)
        except RuntimeError:
            logger.error("unhandled: %s", text, exc_info=True)
        out = capfd.readouterr()
    finally:
        root.handlers[:] = saved
    rendered = out.out + out.err
    assert "unhandled" in rendered, "lastResort printed the record"
    return rendered


def _sink_asyncio_default_handler(text: str) -> str:
    """asyncio's default exception handler ("Task exception was never retrieved")."""
    import asyncio

    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    loop = asyncio.new_event_loop()
    try:
        try:
            _raise(text)
        except RuntimeError as exc:
            loop.default_exception_handler({"message": f"Task exception: {text}", "exception": exc})
    finally:
        loop.close()
        root.removeHandler(handler)
    rendered = stream.getvalue()
    assert "Task exception" in rendered
    return rendered


def _sink_handler_added_later(text: str) -> str:
    """A handler attached after install_log_redaction, on a child logger."""
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("mcp.server.lowlevel.late")
    logger.addHandler(handler)
    try:
        try:
            _raise(text)
        except RuntimeError:
            logger.exception("late handler: %s", text)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def _sink_unconfigured_cli_command(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], text: str
) -> str:
    """A CLI command that just logs: root() configures the redacting chain."""
    import structlog as _structlog
    from typer.testing import CliRunner

    _structlog.reset_defaults()

    @cli_main.app.command("redaction-probe", hidden=True)
    def _probe() -> None:
        held_secret = text  # noqa: F841 - a frame local that must not print
        try:
            _raise(text)
        except RuntimeError:
            _structlog.get_logger("daimon.cli.probe").exception("probe.failed", note=text)

    capfd.readouterr()
    result = CliRunner().invoke(cli_main.app, ["redaction-probe"])
    out = capfd.readouterr()
    rendered = result.output + out.out + out.err
    assert "probe.failed" in rendered
    return rendered


def _sink_preformatted_exc_text(text: str) -> str:
    """A record carrying only cached traceback text (no live exc_info)."""
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("daimon.test.exc_text")
    logger.addHandler(handler)
    try:
        record = logger.makeRecord(
            logger.name, logging.ERROR, __file__, 1, "cached failure", None, None
        )
        record.exc_text = f"Traceback (most recent call last):\nRuntimeError: {text}"
        logger.handle(record)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def _sink_notebook_cli(text: str) -> str:
    """The notebook CLI's own error catches (host error, cell errors)."""
    import asyncio
    import tempfile
    from pathlib import Path

    from daimon.adapters.cli.commands import notebook
    from daimon.core.config import NotebookSettings
    from pydantic import HttpUrl, SecretStr

    def _handler(status: int, body: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _req: httpx.Response(status, json=body))

    rendered = ""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "nb.py"
        source.write_text("import marimo\n", encoding="utf-8")
        for status, body, code in (
            (500, {"error": text}, 5),
            (422, {"detail": {"cell_errors": [f"RuntimeError: {text}"]}}, 4),
        ):
            buffer = io.StringIO()
            console = Console(file=buffer, force_terminal=False, width=400)
            client = httpx.AsyncClient(transport=_handler(status, body))
            with pytest.raises(typer.Exit) as exit_info:
                asyncio.run(
                    notebook.publish_blog_file(
                        NotebookSettings(
                            host_url=HttpUrl("https://nb.example"),
                            admin_secret=SecretStr("s"),
                        ),
                        console,
                        slug="probe",
                        file=str(source),
                        http_client=client,
                    )
                )
            assert exit_info.value.exit_code == code
            rendered += buffer.getvalue()
    return rendered


def _sink_usage_error(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], text: str
) -> str:
    """A Typer usage error raised inside a command, through `daimon`'s main."""

    @cli_main.app.command("usage-probe", hidden=True)
    def _probe() -> None:
        raise typer.BadParameter(text)

    monkeypatch.setattr("sys.argv", ["daimon", "usage-probe"])
    capfd.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main()
    assert exit_info.value.code == 2
    out = capfd.readouterr()
    rendered = out.out + out.err
    assert "Error" in rendered
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
    _assert_clean(_sink_last_resort(capfd, text), canary, "logging.lastResort")
    _assert_clean(_sink_asyncio_default_handler(text), canary, "asyncio default handler")
    _assert_clean(_sink_handler_added_later(text), canary, "handler added later, child logger")
    _assert_clean(
        _sink_unconfigured_cli_command(monkeypatch, capfd, text), canary, "cli command logging"
    )
    _assert_clean(_sink_preformatted_exc_text(text), canary, "cached exc_text")
    _assert_clean(_sink_notebook_cli(text), canary, "notebook cli")
    _assert_clean(_sink_usage_error(monkeypatch, capfd, text), canary, "cli usage error")
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


def test_sentry_logging_integration_keeps_the_exception_but_not_the_secret(
    sentry_transport: _Transport,
) -> None:
    """Output handlers get a redacted copy; Sentry's own handler keeps exc_info."""
    canary = new_canary()
    install_log_redaction()
    sentry_transport.payloads.clear()
    try:
        _raise(f"api_key={canary}")
    except RuntimeError:
        logging.getLogger("daimon.test.sentry").exception("op.failed")
    sentry_sdk.flush()

    rendered = repr(sentry_transport.payloads)
    assert "RuntimeError" in rendered and "exception" in rendered
    assert canary not in rendered


def test_warnings_and_unraisable_exceptions_are_redacted(
    capfd: pytest.CaptureFixture[str],
) -> None:
    import sys
    import warnings

    canary = new_canary()
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("py.warnings").addHandler(handler)
    try:
        # pytest swaps showwarning per test; re-route warnings to logging
        # inside this block, as install_log_redaction does for the process.
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            logging.captureWarnings(False)
            logging.captureWarnings(True)
            warnings.warn(f"retrying with token={canary}", RuntimeWarning, stacklevel=1)
    finally:
        logging.getLogger("py.warnings").removeHandler(handler)
    assert "retrying" in stream.getvalue()
    _assert_clean(stream.getvalue(), canary, "warnings")

    capfd.readouterr()
    try:
        _raise(f"password={canary}")
    except RuntimeError as exc:
        from types import SimpleNamespace

        sys.unraisablehook(
            SimpleNamespace(  # pyright: ignore[reportArgumentType]
                exc_type=type(exc),
                exc_value=exc,
                exc_traceback=exc.__traceback__,
                err_msg="Exception ignored in",
                object=None,
            )
        )
    out = capfd.readouterr()
    assert "RuntimeError" in out.err
    _assert_clean(out.out + out.err, canary, "unraisablehook")
