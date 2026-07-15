"""Uniform table/JSON emission for list/get commands."""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel
from rich.console import Console
from rich.table import Table


def render_table(
    console: Console,
    rows: Sequence[BaseModel],
    columns: Sequence[str],
) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    for row in rows:
        dumped = row.model_dump(mode="json")
        table.add_row(*(str(dumped.get(c, "")) for c in columns))
    console.print(table)


def render_json(console: Console, rows: Sequence[BaseModel]) -> None:
    payload = [row.model_dump(mode="json") for row in rows]
    console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)


def emit_rows(
    console: Console,
    rows: Sequence[BaseModel],
    *,
    columns: Sequence[str],
    as_json: bool,
) -> None:
    if as_json:
        render_json(console, rows)
    else:
        render_table(console, rows, columns)
