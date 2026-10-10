"""Keep each gcloud SSH payload in one shell argument.

Shell's ``'\\''`` concatenation is valid: it inserts an apostrophe in the
same argument. An unescaped apostrophe, even in a remote comment, can end the
argument early and make gcloud reject the remaining words.

Convention: a comment on a payload's closing-quote line (``' # done``) must
not contain a quote. The guard reads such a quote as opening a new argument
and fails, which errs toward a loud false positive, never a missed split.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import cast

import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_SSH = re.compile(r"\bgcloud[ \t]+compute[ \t]+ssh\b")
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def _run_blocks(node: Node) -> list[ScalarNode]:
    if isinstance(node, SequenceNode):
        return [block for child in node.value for block in _run_blocks(child)]
    if isinstance(node, MappingNode):
        blocks: list[ScalarNode] = []
        for key, value in node.value:
            if isinstance(key, ScalarNode) and key.value == "run":
                assert isinstance(value, ScalarNode)
                blocks.append(value)
            else:
                blocks.extend(_run_blocks(value))
        return blocks
    return []


def _shell_command(text: str) -> str:
    """Stop at a command separator outside shell quotes."""
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        elif not quote and char in "\n;|&":
            break
        index += 1
    return text[:index]


def _unquoted_ssh_matches(text: str) -> list[re.Match[str]]:
    """Find SSH invocations outside quoted arguments and shell comments."""
    matches: list[re.Match[str]] = []
    quote = ""
    comment = False
    command_start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
                command_start = index + 1
        elif char == "\\" and quote != "'":
            index += 2
            continue
        elif char == quote:
            quote = ""
        elif not quote:
            if char in "'\"":
                quote = char
            elif char == "#" and (index == 0 or text[index - 1] in " \t\n;|&"):
                comment = True
            elif char in "\n;|&(){}":
                command_start = index + 1
            else:
                match = _SSH.match(text, index)
                prefix = text[command_start:index].strip()
                if match and prefix in {"", "if", "then", "do", "else", "elif", "!"}:
                    matches.append(match)
                    index = match.end()
                    continue
        index += 1
    return matches


def _check_workflow(name: str, source: str) -> int:
    document = cast("Node | None", yaml.compose(source))  # pyright: ignore[reportUnknownMemberType]
    assert document is not None, f"{name}: empty workflow"
    scripts = 0
    for block in _run_blocks(document):
        for match in _unquoted_ssh_matches(block.value):
            line = (
                block.start_mark.line
                + (2 if block.style in {"|", ">"} else 1)
                + block.value.count("\n", 0, match.start())
            )
            command = _shell_command(block.value[match.start() :])
            command_lines = command.splitlines()
            display = command_lines[0].strip()
            if len(command_lines) > 1:
                display += f" ... {command_lines[-1].strip()}"
            try:
                # GitHub replaces these expressions before Bash parses the step.
                words = shlex.split(
                    _EXPRESSION.sub("VALUE", command.replace("\\\n", "")),
                    comments=True,
                )
            except ValueError as exc:
                raise AssertionError(f"{name}:{line}: {display}: {exc}") from exc
            expected = [
                "gcloud",
                "compute",
                "ssh",
                "VALUE",
                "--zone",
                "$ZONE",
                "--tunnel-through-iap",
            ]
            assert (
                len(words) >= 7
                and words[:3] == expected[:3]
                and words[3].startswith("VALUE")
                and words[4:7] == expected[4:7]
            ), f"{name}:{line}: unexpected SSH arguments: {display}"
            assert len(words) >= 8, f"{name}:{line}: missing --command: {display}"
            inline = words[7].startswith("--command=")
            assert words[7] == "--command" or inline, (
                f"{name}:{line}: unexpected --command position: {display}"
            )
            assert len(words) == (8 if inline else 9), (
                f"{name}:{line}: SSH payload must be exactly one argument: {display}"
            )
            script = words[7].removeprefix("--command=") if inline else words[8]
            assert script, f"{name}:{line}: empty SSH payload: {display}"
            if script != "true":
                scripts += 1
                # A multiline payload closes at the command's indentation.
                # An indented body line starting with a quote is not its close.
                opener = re.search(r"--command(?:=|[ \t]+)'", command)
                if opener and "\n" in command[opener.end() :]:
                    closing = command.splitlines()[-1]
                    opening = next(line for line in command.splitlines() if "--command" in line)
                    opening_indent = len(opening) - len(opening.lstrip())
                    closing_indent = len(closing) - len(closing.lstrip())
                    assert re.fullmatch(r"'\s*(?:#.*)?", closing.strip()) and (
                        closing_indent <= opening_indent
                    ), f"{name}:{line}: SSH payload closed inside its body: {closing.strip()}"
    return scripts


def test_deploy_workflow_remote_scripts() -> None:
    scripts = 0
    for workflow in sorted((*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml"))):
        scripts += _check_workflow(workflow.name, workflow.read_text())
    assert scripts == 4, f"expected exactly 4 remote scripts, found {scripts}"


@pytest.mark.parametrize(
    ("filename", "command", "error"),
    [
        ("fixture.yml", "--command '\n  # asset's comment\n  echo ok\n'", "one argument"),
        ("fixture.yml", "--command '\n  echo ok\n  'oops\n'", "closed inside its body"),
        ("fixture.yaml", "--command='\n  echo ok\n  'oops\n'", "closed inside its body"),
        ("fixture.yml", "--command='echo ok'", None),
        ("fixture.yml", "--command 'echo ok'", None),
        ("fixture.yaml", "--command 'echo ok'", None),
        ("fixture.yml", "--command 'echo it'\\''s ok'", None),
        ("fixture.yml", "--command 'echo \"gcloud compute ssh is available\"'", None),
        ("fixture.yml", "--command '\n  echo ok\n' # done", None),
    ],
)
def test_ssh_quote_fixtures(filename: str, command: str, error: str | None) -> None:
    run = (
        f'gcloud compute ssh ${{{{ vars.VM }}}} --zone "$ZONE" \\\n  --tunnel-through-iap {command}'
    )
    source = "jobs:\n  test:\n    steps:\n      - run: |\n" + "".join(
        f"          {line}\n" for line in run.splitlines()
    )
    if error:
        with pytest.raises(AssertionError, match=error):
            _check_workflow(filename, source)
    else:
        assert _check_workflow(filename, source) == 1
