"""Approved copy that must keep its line spacing."""

from daimon.adapters.slack import feedback, memory
from daimon.adapters.slack.help import build_help_blocks


def test_feedback_and_memory_messages_keep_approved_spacing() -> None:
    assert feedback._POLICY_UNREADABLE == (  # pyright: ignore[reportPrivateUsage]
        "Your feedback wasn't saved.\n\nAsk an admin to check this workspace's access settings."
    )
    assert feedback._SHARED_HINT == (  # pyright: ignore[reportPrivateUsage]
        "Support gets your feedback and a link to the answer, not the answer itself."
    )
    assert memory._EMPTY == "No memory to show here."  # pyright: ignore[reportPrivateUsage]


def test_help_lists_registered_commands_on_consecutive_lines() -> None:
    blocks = build_help_blocks(display_name="Daimon")
    commands = blocks[2]["text"]["text"]
    assert len(commands.splitlines()) == 9
    assert all(line.startswith("/") for line in commands.splitlines())
    assert "/github connect" not in commands
    assert "/github       Connect repos and choose which agents use them" in commands
