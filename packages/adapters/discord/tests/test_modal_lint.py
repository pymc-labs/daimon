"""Tests for scripts/lint_discord_modals.py.

The script lives outside any package (`scripts/`, not `daimon.*`), so it is
loaded here via `importlib.util.spec_from_file_location` against a path
derived from the repository root rather than a normal import. This package
already runs in its own CI shard and has a conftest; `scripts/` has neither,
so the cost of a package test reaching for a top-level script is paid here
explicitly instead of inventing a new `scripts/tests/` tree.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[4] / "scripts" / "lint_discord_modals.py"


def _load_lint_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lint_discord_modals", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, (
        f"could not build an import spec for {_SCRIPT_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses' `frozen=True` resolution looks the module up in
    # sys.modules by name at class-creation time, so it must be registered
    # before exec_module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint_discord_modals = _load_lint_module()


def _findings(source: str) -> list[object]:
    """Parse ``source``, walk every Modal subclass in it, and return the
    findings — string in, findings out, no filesystem involved."""
    tree = ast.parse(source)
    int_constants = lint_discord_modals._module_int_constants(tree)
    findings: list[object] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
            lint_discord_modals._is_modal_base(b) for b in node.bases
        ):
            findings.extend(lint_discord_modals._walk_modal_class(node, "<test>", int_constants))
    return findings


def _rules(source: str) -> list[str]:
    return [f.rule for f in _findings(source)]  # type: ignore[attr-defined]


def test_label_wrapped_select_allowed_when_inside_modal() -> None:
    source = """
class Picker(discord.ui.Modal, title="Pick"):
    row = discord.ui.Label(component=discord.ui.Select(options=[]))
"""
    assert _rules(source) == [], (
        "a Label wrapping a Select is a supported modal component and must yield no findings"
    )


def test_bare_top_level_select_allowed_when_inside_modal() -> None:
    source = """
class Picker(discord.ui.Modal, title="Pick"):
    row = discord.ui.Select(options=[])
"""
    assert _rules(source) == [], (
        "discord.py 2.7.1 auto-wraps a bare top-level Select in an ActionRow just "
        "like a bare TextInput, so a bare Select must not be rejected"
    )


def test_six_mixed_children_over_cap() -> None:
    source = """
class TooMany(discord.ui.Modal, title="Too many"):
    a = discord.ui.Label(component=discord.ui.Select(options=[]))
    b = discord.ui.RadioGroup()
    c = discord.ui.CheckboxGroup()
    d = discord.ui.FileUpload()
    e = discord.ui.TextDisplay(content="hi")
    f = discord.ui.TextInput(label="F")
"""
    assert _rules(source) == ["discord/too-many-components"], (
        "six top-level children of mixed type must trip the too-many-components rule exactly once"
    )


def test_five_mixed_children_at_cap() -> None:
    source = """
class AtCap(discord.ui.Modal, title="At cap"):
    a = discord.ui.Label(component=discord.ui.Select(options=[]))
    b = discord.ui.RadioGroup()
    c = discord.ui.CheckboxGroup()
    d = discord.ui.FileUpload()
    e = discord.ui.TextDisplay(content="hi")
"""
    assert _rules(source) == [], (
        "five top-level children is Discord's exact cap and must not trip the "
        "too-many-components rule — the boundary pair guards against an off-by-one "
        "that would silently un-guard the cap"
    )


def test_five_children_including_label_wrapped_select_at_cap() -> None:
    source = """
class AtCapWithLabel(discord.ui.Modal, title="At cap"):
    a = discord.ui.Label(component=discord.ui.Select(options=[]))
    b = discord.ui.RadioGroup()
    c = discord.ui.CheckboxGroup()
    d = discord.ui.FileUpload()
    e = discord.ui.TextDisplay(content="hi")
"""
    assert _rules(source) == [], (
        "a Label wrapping a Select must cost one slot against the five-item cap, "
        "because its Select is nested inside the Label"
    )


def test_text_input_without_label_flagged() -> None:
    source = """
class NoLabel(discord.ui.Modal, title="No label"):
    field = discord.ui.TextInput()
"""
    assert _rules(source) == ["discord/missing-label"], (
        "a TextInput with no label kwarg must still be flagged — this rule is not "
        "changing and this test is a regression guard on the rewrite"
    )


def test_text_input_with_long_label_flagged() -> None:
    long_label = "x" * 46
    source = f"""
class LongLabel(discord.ui.Modal, title="Long label"):
    field = discord.ui.TextInput(label="{long_label}")
"""
    assert _rules(source) == ["discord/label-too-long"], (
        "a 46-character label exceeds Discord's 45-codepoint cap and must still be "
        "flagged — this rule is not changing and this test is a regression guard "
        "on the rewrite"
    )


def test_text_input_with_non_literal_max_length_flagged() -> None:
    source = """
class NonLiteralMaxLength(discord.ui.Modal, title="Non literal"):
    field = discord.ui.TextInput(label="F", max_length=SOME_IMPORTED_CONST)
"""
    assert _rules(source) == ["discord/non-literal-max-length"], (
        "a max_length referencing a name the lint cannot resolve (e.g. an imported "
        "constant) must still be flagged as non-literal — this rule is not "
        "changing and this test is a regression guard on the rewrite"
    )


def test_non_modal_class_produces_no_findings() -> None:
    source = """
class NotAModal(discord.ui.View):
    a = discord.ui.Label(component=discord.ui.Select(options=[]))
    b = discord.ui.RadioGroup()
    c = discord.ui.CheckboxGroup()
    d = discord.ui.FileUpload()
    e = discord.ui.TextDisplay(content="hi")
    f = discord.ui.TextInput(label="F")
"""
    assert _rules(source) == [], (
        "a class that does not subclass Modal must produce no findings whatever it contains"
    )
