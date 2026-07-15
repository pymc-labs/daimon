from daimon.adapters.cli.flags import JSON_OPTION, TRACE_OPTION, YES_OPTION


def test_flags_carry_consistent_help_text() -> None:
    assert "JSON" in JSON_OPTION.help
    assert "confirmation" in YES_OPTION.help
    assert "chat" in TRACE_OPTION.help
