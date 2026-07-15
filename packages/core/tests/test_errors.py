from __future__ import annotations

from daimon.core.errors import (
    ConfigError,
    DaimonError,
    SpecError,
    StoreError,
    TurnError,
)


def test_taxonomy_all_inherit_from_daimon_error_when_raised() -> None:
    for cls in (ConfigError, SpecError, StoreError, TurnError):
        assert issubclass(cls, DaimonError), (
            f"{cls.__name__} must inherit DaimonError so adapters catch one base"
        )


def test_turn_error_round_trips_kind_and_message_when_constructed() -> None:
    err = TurnError(kind="upstream", message="429 from MA")
    assert err.kind == "upstream", "kind must round-trip"
    assert err.message == "429 from MA", "message must round-trip"
    assert str(err) == "upstream: 429 from MA", (
        "str() should include both kind and message when both are set"
    )


def test_turn_error_defaults_message_to_empty_when_omitted() -> None:
    err = TurnError(kind="interrupted")
    assert err.kind == "interrupted"
    assert err.message == "", "default message is empty string, never None"
    assert str(err) == "interrupted", "str() falls back to the kind alone when message is empty"


def test_turn_error_preserves_cause_when_raised_from_upstream() -> None:
    original = RuntimeError("simulated anthropic.APIError")
    try:
        try:
            raise original
        except RuntimeError as err:
            raise TurnError(kind="upstream", message=str(err)) from err
    except TurnError as caught:
        assert caught.__cause__ is original, (
            "raise ... from err must set __cause__ for --trace rendering"
        )
