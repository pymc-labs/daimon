"""Tests for the shared Discord embed palette and its stdlib-purity invariant."""

from __future__ import annotations

import pathlib

import daimon.adapters.discord.theme as theme


def test_theme_exports_seven_named_color_constants_with_correct_hex() -> None:
    assert theme.COLOR_BLURPLE == 0x5865F2, "COLOR_BLURPLE must be Discord blurple"
    assert theme.COLOR_RED == 0xED4245, "COLOR_RED must be Discord red"
    assert theme.COLOR_GREEN == 0x57F287, "COLOR_GREEN must be Discord green"
    assert theme.COLOR_GREYPLE == 0x99AAB5, "COLOR_GREYPLE must be Discord greyple"
    assert theme.COLOR_IN_PROGRESS == 0x3498DB, "COLOR_IN_PROGRESS must be the running turn color"
    assert theme.COLOR_PAUSED == 0xFEE75C, "COLOR_PAUSED must be the routine paused color"
    assert theme.COLOR_AMBER == 0xFEE75C, "COLOR_AMBER must be the posted-control accent color"


def test_theme_module_imports_no_discord_or_adapter_module() -> None:
    src = pathlib.Path(theme.__file__).read_text()
    assert "import discord" not in src, (
        "theme.py must stay stdlib-only — embed.py purity depends on it"
    )
    assert "daimon.adapters" not in src, (
        "theme.py must not import any adapter module — it is imported by pure embed.py"
    )
