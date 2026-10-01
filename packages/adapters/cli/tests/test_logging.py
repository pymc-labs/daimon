import pytest
import structlog
from daimon.adapters.cli.logging import (
    configure_admin_logging,
    configure_bootstrap_logging,
)


def test_bootstrap_logging_emits_plain_console_to_stderr() -> None:
    configure_bootstrap_logging()
    log = structlog.get_logger("daimon.adapters.cli.test")
    log.info("admin.ping", ok=True)


def test_admin_logging_is_alias_for_bootstrap() -> None:
    configure_admin_logging()
    structlog.get_logger().info("admin.emit", n=1)


def test_bootstrap_logging_tracebacks_never_print_frame_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A CLI crash must not dump locals, which can hold decrypted agent keys."""
    configure_bootstrap_logging()
    canary = "canary-" + "local-value-3b9f"

    def _fails() -> None:
        value = canary  # noqa: F841 - the local under test
        raise RuntimeError("boom")

    try:
        _fails()
    except RuntimeError:
        structlog.get_logger("daimon.adapters.cli.test").exception("admin.failed")

    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "canary-local-value-3b9f" not in captured.err


def test_bootstrap_logging_redacts_credential_text_in_exceptions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    configure_bootstrap_logging()
    canary = "canary-" + "cli-exception-7c1d"
    try:
        raise RuntimeError(json.dumps({"api_key": canary}))
    except RuntimeError:
        structlog.get_logger("daimon.adapters.cli.test").exception(
            "admin.failed", note=f"token={canary}"
        )

    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert canary not in captured.err
