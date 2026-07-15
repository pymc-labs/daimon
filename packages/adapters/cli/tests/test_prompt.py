from io import StringIO
from unittest.mock import patch

import pytest
import typer
from daimon.adapters.cli.prompt import confirm_or_abort
from rich.console import Console


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False)


def test_confirm_or_abort_skips_when_yes() -> None:
    with patch("daimon.adapters.cli.prompt.typer.confirm") as tc:
        confirm_or_abort(_console(), "delete?", yes=True)
        tc.assert_not_called()


def test_confirm_or_abort_accepts_y() -> None:
    with patch("daimon.adapters.cli.prompt.typer.confirm", return_value=True):
        confirm_or_abort(_console(), "delete?", yes=False)


def test_confirm_or_abort_raises_on_default_deny() -> None:
    with (
        patch("daimon.adapters.cli.prompt.typer.confirm", return_value=False),
        pytest.raises(typer.Exit),
    ):
        confirm_or_abort(_console(), "delete?", yes=False)
