"""Tests for the shared Components V2 layout helpers."""

from __future__ import annotations

import pathlib

import daimon.adapters.discord.layout as layout
import discord


class TestHeader:
    def test_header_without_subtext_renders_h2_title(self) -> None:
        td = layout.header("Agent setup")
        assert td.content == "## Agent setup", "header() without subtext must produce '## {title}'"

    def test_header_with_subtext_appends_dim_line(self) -> None:
        td = layout.header("Privacy", subtext="for you")
        assert td.content == "## Privacy\n-# for you", (
            "header() with subtext must append a '-# {subtext}' line separated by newline"
        )

    def test_header_returns_text_display_instance(self) -> None:
        td = layout.header("Test")
        assert isinstance(td, discord.ui.TextDisplay), (
            "header() must return a discord.ui.TextDisplay"
        )

    def test_header_subtext_none_produces_no_dim_line(self) -> None:
        td = layout.header("No subtext", subtext=None)
        assert "\n" not in td.content, (
            "header() with subtext=None must not include a newline in the content"
        )


class TestHairline:
    def test_hairline_returns_visible_separator(self) -> None:
        sep = layout.hairline()
        assert sep.visible is True, (
            "hairline() must return a visible Separator (discord.py default)"
        )

    def test_hairline_returns_separator_instance(self) -> None:
        sep = layout.hairline()
        assert isinstance(sep, discord.ui.Separator), (
            "hairline() must return a discord.ui.Separator"
        )


class TestAirGap:
    def test_air_gap_is_invisible(self) -> None:
        sep = layout.air_gap()
        assert sep.visible is False, (
            "air_gap() must return a Separator with visible=False (wire field: divider=false)"
        )

    def test_air_gap_has_large_spacing(self) -> None:
        sep = layout.air_gap()
        assert sep.spacing == discord.SeparatorSpacing.large, (
            "air_gap() must use SeparatorSpacing.large for the invisible spacer"
        )

    def test_air_gap_returns_separator_instance(self) -> None:
        sep = layout.air_gap()
        assert isinstance(sep, discord.ui.Separator), "air_gap() must return a discord.ui.Separator"


class TestStaticView:
    def test_static_view_returns_layout_view(self) -> None:
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        view = layout.static_view(container)
        assert isinstance(view, discord.ui.LayoutView), (
            "static_view() must return a discord.ui.LayoutView"
        )

    def test_static_view_contains_the_passed_container(self) -> None:
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        view = layout.static_view(container)
        children = list(view.walk_children())
        assert container in children, (
            "static_view() must include the passed container in walk_children()"
        )

    def test_static_view_has_no_interactive_children(self) -> None:
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        view = layout.static_view(container)
        for child in view.walk_children():
            assert not isinstance(child, discord.ui.Button), (
                "static_view() must not contain any Button components"
            )
            assert not isinstance(child, discord.ui.Select), (  # type: ignore[misc]
                "static_view() must not contain any Select components"
            )


class TestLayoutModulePurity:
    def test_layout_module_imports_no_first_party_adapter_code(self) -> None:
        src = pathlib.Path(layout.__file__).read_text()  # type: ignore[arg-type]
        assert "daimon.adapters" not in src, (
            "layout.py must not import any daimon.adapters module — it is a pure leaf"
        )
