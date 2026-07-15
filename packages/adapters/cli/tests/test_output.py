import json
from io import StringIO

from daimon.adapters.cli.output import emit_rows, render_json, render_table
from pydantic import BaseModel
from rich.console import Console


class _Row(BaseModel):
    name: str
    count: int


def _console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, force_terminal=False, highlight=False, width=80), buf


def test_render_json_emits_array_of_dumps() -> None:
    console, buf = _console()
    render_json(console, [_Row(name="a", count=1), _Row(name="b", count=2)])
    parsed = json.loads(buf.getvalue())
    assert parsed == [{"name": "a", "count": 1}, {"name": "b", "count": 2}]


def test_render_table_emits_column_headers_and_cells() -> None:
    console, buf = _console()
    render_table(console, [_Row(name="a", count=1)], columns=("name", "count"))
    out = buf.getvalue()
    assert "name" in out and "count" in out and "a" in out and "1" in out


def test_emit_rows_dispatches_on_as_json() -> None:
    console_t, buf_t = _console()
    emit_rows(console_t, [_Row(name="a", count=1)], columns=("name", "count"), as_json=False)
    assert "a" in buf_t.getvalue()
    console_j, buf_j = _console()
    emit_rows(console_j, [_Row(name="a", count=1)], columns=("name", "count"), as_json=True)
    assert json.loads(buf_j.getvalue()) == [{"name": "a", "count": 1}]
