from daimon.core.turn.notices import ended_label
from daimon.core.turn.termination import TerminationReason


def test_ended_label_says_what_happened_and_how_long_it_ran() -> None:
    assert ended_label(TerminationReason.ADMISSION_DENIED, 0) == "Not started"
    assert ended_label(TerminationReason.INTERRUPTED, 65).startswith("Stopped after ")
    assert ended_label(TerminationReason.CEILING, 65).startswith("Timed out after ")
    assert ended_label(TerminationReason.SESSION_TERMINATED, 5).startswith("Ended with an error")
