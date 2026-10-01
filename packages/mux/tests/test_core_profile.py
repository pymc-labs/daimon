"""The floor clears on Anthropic and OpenAI, not on Google yet."""

from __future__ import annotations

from mux.backends import BACKENDS
from mux.core_profile import missing_capabilities


def test_anthropic_clears_floor() -> None:
    assert missing_capabilities(BACKENDS["anthropic"]) == ()


def test_openai_clears_floor() -> None:
    assert missing_capabilities(BACKENDS["openai"]) == ()


def test_google_reports_its_gap() -> None:
    assert missing_capabilities(BACKENDS["google"]) == ("durable_fs",)
