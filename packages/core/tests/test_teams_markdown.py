"""`escape_dollars`: currency stays literal under Extended Markdown, math and code untouched."""

from __future__ import annotations

import pytest
from daimon.core.teams_markdown import escape_dollars


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("It costs $5 and $10.", r"It costs \$5 and \$10."),
        ("Between $20,000 and $30,000", r"Between \$20,000 and \$30,000"),
        ("$5-$10 a month", r"\$5-\$10 a month"),
        ("Only $ 5 left", r"Only \$ 5 left"),
        ("A lone $", r"A lone \$"),
        ("Already \\$5", "Already \\$5"),
        ("Total: $$", r"Total: \$\$"),
    ],
)
def test_escape_dollars_escapes_currency(text: str, expected: str) -> None:
    """A `$` that opens or closes no math span is escaped."""
    assert escape_dollars(text) == expected, f"currency in {text!r} should render literally"


@pytest.mark.parametrize(
    "text",
    [
        "Euler: $e^{i\\pi} + 1 = 0$.",
        "Inline $x$ and $y_2$ here",
        "$$\n\\int_0^1 x\\,dx = \\tfrac12\n$$",
        "Block $$a^2 + b^2 = c^2$$ inline",
        "Escaped \\$ inside $a \\$ b$ math",
    ],
)
def test_escape_dollars_keeps_math(text: str) -> None:
    """Valid `$…$` and `$$…$$` spans are left for KaTeX."""
    assert escape_dollars(text) == text, f"math in {text!r} should be untouched"


def test_escape_dollars_mixes_currency_and_math() -> None:
    """A price before a formula: the price is escaped, the formula kept."""
    text = "At $5 per unit, revenue is $r = 5n$."
    assert escape_dollars(text) == r"At \$5 per unit, revenue is $r = 5n$.", (
        "a `$` followed later by a space-led `$` is no math opener"
    )


def test_escape_dollars_closing_before_digit_is_not_math() -> None:
    """pandoc's rule: a closing `$` followed by a digit ends no span."""
    assert escape_dollars("from $a$5") == r"from \$a\$5", "`$5` after a span must stay literal"


def test_escape_dollars_math_stops_at_a_blank_line() -> None:
    """A span never crosses paragraphs."""
    text = "Pay $x\n\nthen y$ later"
    assert escape_dollars(text) == "Pay \\$x\n\nthen y\\$ later", "no math across a blank line"


def test_escape_dollars_leaves_code_alone() -> None:
    """Inline code and fenced blocks keep their `$` exactly."""
    text = (
        "Run `echo $HOME` for $5.\n"
        "```bash\n"
        "PRICE=$5\n"
        'echo "$PRICE"\n'
        "```\n"
        "~~~\n"
        "cost = $10\n"
        "~~~\n"
        "Then $3 more."
    )
    expected = (
        "Run `echo $HOME` for \\$5.\n"
        "```bash\n"
        "PRICE=$5\n"
        'echo "$PRICE"\n'
        "```\n"
        "~~~\n"
        "cost = $10\n"
        "~~~\n"
        "Then \\$3 more."
    )
    assert escape_dollars(text) == expected, "only prose dollars should be escaped"


def test_escape_dollars_unclosed_fence_runs_to_the_end() -> None:
    """A fence never closed keeps the rest as code, as Markdown renders it."""
    text = "Price $5\n```\n$HOME and $PATH"
    assert escape_dollars(text) == "Price \\$5\n```\n$HOME and $PATH", "unclosed fence is code"


def test_escape_dollars_unmatched_backtick_is_literal() -> None:
    """A lone backtick opens no code span, so dollars after it are still prose."""
    assert escape_dollars("it's ` $5") == r"it's ` \$5", "a lone backtick is plain text"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<https://ex.com/$5> costs $5", r"<https://ex.com/$5> costs \$5"),
        ("<mailto:a$b@ex.com>", "<mailto:a$b@ex.com>"),
        ("<not a link $5>", r"<not a link \$5>"),
        ("<at>Bob</at> owes $5", r"<at>Bob</at> owes \$5"),
    ],
)
def test_escape_dollars_copies_autolinks_through(text: str, expected: str) -> None:
    """CommonMark reads no escapes in an autolink: a `\\` there would join the URL."""
    assert escape_dollars(text) == expected, f"the autolink in {text!r} should stay as is"


def test_escape_dollars_a_fence_with_an_info_string_does_not_close() -> None:
    """Only a bare fence closes one: code after "```python" inside stays code."""
    text = "```\ncode $a\n```python\n$5 still code\n```\nafter $5\n"
    expected = "```\ncode $a\n```python\n$5 still code\n```\nafter \\$5\n"
    assert escape_dollars(text) == expected, "the fence closes at the bare line only"
    assert escape_dollars("~~~\n$x\n~~~  \n$5") == "~~~\n$x\n~~~  \n\\$5", "trailing spaces close"
