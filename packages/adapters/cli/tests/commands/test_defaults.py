import json
from io import StringIO

from daimon.adapters.cli.commands.defaults import _format_report_json, _format_report_table
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from rich.console import Console


def test_format_report_json_emits_kind_bucket_lists() -> None:
    report = ApplyReport()
    report.add(
        ResourceOutcome(kind="agent", name="coder", action=Action.CREATED, anthropic_id="ag_1")
    )
    report.add(ResourceOutcome(kind="environment", name="default", action=Action.SKIPPED))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False)
    _format_report_json(console, report)
    parsed = json.loads(buf.getvalue())
    assert parsed["agents"][0]["name"] == "coder"
    assert parsed["environments"][0]["action"] == "skipped"


def test_format_report_table_shows_summary_footer() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="agent", name="x", action=Action.CREATED))
    report.add(ResourceOutcome(kind="agent", name="y", action=Action.FAILED, error="oops"))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)
    _format_report_table(console, report)
    out = buf.getvalue()
    assert "created" in out and "failed" in out and "x" in out and "y" in out


def test_format_report_json_includes_system_config_bucket() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="system_config", name="system", action=Action.UPDATED))
    buf = StringIO()
    console = Console(file=buf, highlight=False, force_terminal=False, width=200)
    _format_report_json(console, report)
    payload = json.loads(buf.getvalue())
    assert payload["system_config"] == [
        {
            "kind": "system_config",
            "name": "system",
            "action": "updated",
            "anthropic_id": None,
            "error": None,
        }
    ]


def test_format_report_table_renders_system_config_rows() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="system_config", name="system", action=Action.UPDATED))
    buf = StringIO()
    console = Console(file=buf, highlight=False, force_terminal=False, width=200)
    _format_report_table(console, report)
    out = buf.getvalue()
    assert "system_config" in out
    assert "system" in out
    assert "updated" in out
