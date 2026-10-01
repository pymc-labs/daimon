"""Reader-2 (model) `ToolError` copy for the continuity tool surface.

Split out of `daimon.core.continuity.messages` to keep that module under the
~200-line target: these three strings are `ToolError` text read by the model
deciding what to say next, not final person-facing copy, but they live in the
`continuity` package because they cover the same events (handoff refusals,
the unsaved-work question posed to the tool caller).

Pure module — no I/O, no clock, no randomness.
"""

from __future__ import annotations

from daimon.core.continuity.messages import render_unsaved_work_question

__all__ = [
    "render_tool_refusal_setup_thread",
    "render_tool_refusal_unreachable",
    "render_tool_unsaved_work_question",
]


def render_tool_refusal_unreachable(name: str, channel: str) -> str:
    """`ToolError` text: `name` cannot be handed a task because it answers nowhere."""
    return "\n".join(
        [
            f"'{name}' does not answer anywhere in this workspace, so it cannot be handed a task.",
            f"Tell the caller an admin can say: make {name} answer in {channel}. Then the "
            "handoff will work.",
            "Nothing was changed. Do not retry.",
        ]
    )


def render_tool_refusal_setup_thread(target_name: str) -> str:
    """`ToolError` text: a setup conversation cannot hand off a task."""
    return "\n".join(
        [
            "Setup conversations always answer as Daimon, so a task cannot be handed over here.",
            f"Tell the caller to ask {target_name} in a channel where it answers, or to "
            "start a thread there.",
            "Nothing was changed. Do not retry.",
        ]
    )


def render_tool_unsaved_work_question(repo: str) -> str:
    """`ToolError` text: tell the model to ask the D. question, verbatim, on one line."""
    one_line_question = " ".join(render_unsaved_work_question(repo).split("\n"))
    return "\n".join(
        [
            f"Switching would change the checkout and there are uncommitted changes in {repo}.",
            "Ask the caller this question and nothing else, then call hand_off_task again "
            "with unsaved_work:",
            f'"{one_line_question}"',
            "Nothing was changed.",
        ]
    )
