"""The `[attachment]` lines every chat adapter hands the agent."""

from __future__ import annotations

from daimon.core.media.attachments import AttachedFile, attachment_line, attachment_lines


def test_attachment_line_when_inlined_says_it_is_an_image() -> None:
    line = attachment_line(AttachedFile("chart.png", inlined=True))
    assert line == (
        "[attachment] `chart.png`, shared with this message, is attached as an image."
    ), "an inlined image is named, so its block reads as shared rather than as prompt"


def test_attachment_line_when_linked_gives_the_url_size_and_hint() -> None:
    line = attachment_line(
        AttachedFile(
            "q3.csv",
            url="https://files.example.com/q3",
            size=2048,
            expires="about 24 hours",
            not_inlined="too large",
            hint="Then upload it.",
        )
    )
    assert line.startswith("[attachment] `q3.csv` (2048 bytes), shared by the user"), line
    assert "not inlined as an image (too large)" in line, "the reason it is a link"
    assert "(expires in about 24 hours)" in line, "how long the link works"
    assert line.endswith("https://files.example.com/q3 Then upload it."), line


def test_attachment_line_when_unreadable_names_the_file_and_reason() -> None:
    assert attachment_line(AttachedFile("a.pdf")) == (
        "[attachment] `a.pdf` was shared but can't be opened here."
    ), "no reason: the file is only named"
    assert attachment_line(AttachedFile("a.pdf", unreadable="no access.")) == (
        "[attachment] `a.pdf` was shared but can't be opened: no access."
    ), "a reason that ends in a period is not doubled"


def test_attachment_lines_joins_one_line_per_file_and_is_empty_for_none() -> None:
    assert attachment_lines([]) == "", "nothing shared adds nothing to the message"
    both = attachment_lines([AttachedFile("a", inlined=True), AttachedFile("b")])
    assert both.count("\n") == 1, "one line per file"
