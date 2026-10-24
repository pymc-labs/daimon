"""Rules the platforms enforce at send time but their schema validators do not.

Slack's `blocks.validate` is a schema check: it accepts duplicate `block_id`s and
`action_id`s, an input element with no `action_id`, an input modal with no submit
button, an over-long option label and a malformed button URL. `chat.postMessage`
rejects some of these, and a modal is only checked when a user opens it, so a
mistake ships. These rules run offline on every catalogue payload.

Each rule takes a `Built` and returns human-readable violations (empty when clean).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

from .registry import Built

Rule = Callable[[Built], list[str]]

SLACK_OPTION_TEXT_LIMIT = 75


def _walk(node: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (path, dict) for every dict nested anywhere inside `node`."""
    if isinstance(node, dict):
        mapping: dict[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
        yield path, mapping
        for key, value in mapping.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        sequence: list[Any] = node  # pyright: ignore[reportUnknownVariableType]
        for index, value in enumerate(sequence):
            yield from _walk(value, f"{path}[{index}]")


def _duplicates(values: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Group (path, value) pairs by value and keep the values seen more than once."""
    counts = Counter(value for _, value in values)
    return [(value, [p for p, v in values if v == value]) for value, n in counts.items() if n > 1]


def _slack_block_lists(built: Built) -> list[tuple[str, list[dict[str, Any]]]]:
    """The block lists that make up one Slack surface, with a label for each.

    A view and a plain block list are one scope. A message with attachments is
    one scope per attachment plus any top-level blocks.
    """
    payload = built.payload
    if built.kind == "view":
        return [("view", payload["blocks"])]
    if built.kind == "blocks":
        return [("blocks", payload)]
    lists: list[tuple[str, list[dict[str, Any]]]] = []
    if "blocks" in payload:
        lists.append(("blocks", payload["blocks"]))
    for index, attachment in enumerate(payload.get("attachments", [])):
        lists.append((f"attachments[{index}]", attachment.get("blocks", [])))
    return lists


def slack_unique_ids(built: Built) -> list[str]:
    """`block_id` is unique across a message or view; `action_id` is unique within its block.

    Slack documents `action_id` uniqueness per containing block, and `chat.postMessage`
    agrees: it rejects two buttons with one `action_id` in a single actions block but
    accepts the same `action_id` on accessories in different blocks (the agent list
    relies on that). Block and action ids are keyed by block in a modal's submitted state.
    """
    problems: list[str] = []
    for label, blocks in _slack_block_lists(built):
        block_ids = [
            (path, node["block_id"]) for path, node in _walk(blocks, label) if "block_id" in node
        ]
        for value, paths in _duplicates(block_ids):
            problems.append(f"duplicate block_id {value!r} at {paths}")
        for index, block in enumerate(blocks):
            block_path = f"{label}[{index}]"
            action_ids = [
                (path, node["action_id"])
                for path, node in _walk(block, block_path)
                if "action_id" in node
            ]
            for value, paths in _duplicates(action_ids):
                problems.append(f"duplicate action_id {value!r} within one block at {paths}")
    return problems


def slack_input_elements_have_action_id(built: Built) -> list[str]:
    """Every `input` block's element needs an `action_id`, or its value cannot be read back."""
    problems: list[str] = []
    for label, blocks in _slack_block_lists(built):
        for path, node in _walk(blocks, label):
            if node.get("type") == "input" and not node.get("element", {}).get("action_id"):
                problems.append(f"input element without action_id at {path}")
    return problems


def slack_input_modal_has_submit(built: Built) -> list[str]:
    """A modal that asks for input must have a submit button."""
    if built.kind != "view":
        return []
    view = built.payload
    has_input = any(block.get("type") == "input" for block in view["blocks"])
    if has_input and view.get("type") == "modal" and not view.get("submit"):
        return ["modal has input blocks but no submit"]
    return []


def slack_option_text_within_limit(built: Built) -> list[str]:
    """Option labels and descriptions are capped at 75 characters (the validator does not check)."""
    problems: list[str] = []
    for label, blocks in _slack_block_lists(built):
        for path, node in _walk(blocks, label):
            if "text" not in node or node.get("type") not in (None, "plain_text", "mrkdwn"):
                continue
            if not _is_option_text(path):
                continue
            if len(node["text"]) > SLACK_OPTION_TEXT_LIMIT:
                problems.append(
                    f"option text over {SLACK_OPTION_TEXT_LIMIT} chars at {path}: {len(node['text'])}"
                )
    return problems


def _is_option_text(path: str) -> bool:
    """True for the text and description of an option, or of an initial option."""
    parts = path.split(".")
    if len(parts) < 2 or parts[-1] not in ("text", "description"):
        return False
    owner = parts[-2]
    return owner.startswith(("options[", "initial_options[")) or owner == "initial_option"


def slack_button_urls_are_http(built: Built) -> list[str]:
    """A button `url`, when present, must be an absolute http(s) URL."""
    problems: list[str] = []
    for label, blocks in _slack_block_lists(built):
        for path, node in _walk(blocks, label):
            if node.get("type") == "button" and "url" in node:
                parsed = urlparse(str(node["url"]))
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    problems.append(f"button url is not http(s) at {path}: {node['url']!r}")
    return problems


def slack_message_has_no_top_level_text(built: Built) -> list[str]:
    """`blocks.validate` rejects `text` inside a message; it is a separate `chat.postMessage` argument."""
    if built.kind == "message" and "text" in built.payload:
        return ["message payload carries a top-level text"]
    return []


def discord_unique_custom_ids(built: Built) -> list[str]:
    """Discord refuses a message in which two components share a `custom_id`."""
    custom_ids = [
        (path, node["custom_id"]) for path, node in _walk(built.payload) if "custom_id" in node
    ]
    return [f"duplicate custom_id {value!r} at {paths}" for value, paths in _duplicates(custom_ids)]


SLACK_RULES: tuple[Rule, ...] = (
    slack_unique_ids,
    slack_input_elements_have_action_id,
    slack_input_modal_has_submit,
    slack_option_text_within_limit,
    slack_button_urls_are_http,
    slack_message_has_no_top_level_text,
)
DISCORD_RULES: tuple[Rule, ...] = (discord_unique_custom_ids,)


def check(built: Built) -> list[str]:
    """Run every rule for the payload's platform and return all violations."""
    rules = SLACK_RULES if built.platform == "slack" else DISCORD_RULES
    return [f"{rule.__name__}: {problem}" for rule in rules for problem in rule(built)]
