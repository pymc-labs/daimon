"""Literal `.env` parsing and serialization. Pure: no I/O, no clock, no state.

This module is the one place that decides what an uploaded `.env` file means.
The grammar is a deliberately small subset of the informal dotenv format, and
the docstring below *is* the specification — there is no other authority.

Grammar
-------
- The file is split on ``"\\n"``; one trailing ``"\\r"`` per line is stripped, so
  CRLF files parse identically to LF files.
- Blank and whitespace-only lines are ignored. A line whose first non-space
  character is ``#`` is a comment and is ignored.
- An entry line may start with the literal word ``export`` followed by one or
  more spaces or tabs; the prefix is dropped.
- The name runs up to the first ``=`` and must fullmatch ``ENV_NAME_PATTERN``.
  Whitespace on either side of the ``=`` is ignored.
- Values take one of three forms:

  - ``'…'`` — single-quoted. Everything up to the next ``'`` is literal; there
    are no escapes, so a single quote cannot appear inside.
  - ``"…"`` — double-quoted. ``\\\\``, ``\\"``, ``\\$``, a backslash before a backtick,
    ``\\n``, ``\\r`` and ``\\t`` are recognised escapes; any other ``\\x`` is kept as a
    literal backslash followed by ``x``.
  - unquoted — everything to the end of the line, with trailing whitespace
    stripped.

- A quoted value must close on the same line; there are no multi-line values.
- Nothing but whitespace may follow a closing quote.

Two deliberate departures from other dotenv readers
---------------------------------------------------
1. **An unescaped ``#`` inside an unquoted value is literal, not a comment.**
   Most readers strip from ``#`` to end of line. Secrets contain ``#``, and a
   reader that strips mid-line silently truncates a working token into a
   broken one that fails much later, somewhere else. Only a whole line that
   starts with ``#`` is a comment here.
2. **No interpolation and no command substitution.** ``$VAR``, ``${VAR}``,
   ``$(…)`` and backticks are stored as the literal characters typed.

Serialization is shell-safe
---------------------------
The serialized file is loaded in the sandbox with ``set -a; source .env``, so
`serialize_env_line` must produce a line bash reads as one inert assignment,
whatever the value holds: a value is left bare only when every character is in
a small set bash never treats specially, otherwise it is single-quoted, and a
value that contains a single quote, a newline or a carriage return is
double-quoted with ``\\``, ``"``, ``$`` and backtick escaped and line breaks
written as ``\\n``/``\\r``. Bash and `parse_env_file` read every such line back
to the same value, except that an escaped line break is the two characters
``\\n``/``\\r`` to bash. A NUL cannot be represented and is refused.

Reserved names
--------------
A key name that changes how the shell, a loader, git or an HTTP client
behaves (``PATH``, ``LD_PRELOAD``, ``BASH_ENV``, ``GIT_CONFIG_*``,
``HTTPS_PROXY`` …) is refused wherever a key is stored and left out of the
serialized file: exporting it into the sandbox would let whoever set the key
run code or redirect traffic in every later turn. `env_name_problem` is the
one rule.

Rejection is whole-file
-----------------------
`parse_env_file` reads every line, collects the problems it finds, and then
raises a single `EnvFileRejected`: a partially-applied secrets file is worse
than none. `EnvProblem` carries a line number and, when the name is known to
be valid, that name — **there is no field for a value**, by type, so no
rejection path can leak one into a log or a chat message.

Collisions with keys already stored are not this module's business; the caller
compares parsed names against what it holds.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from daimon.core.errors import DaimonError
from pydantic import BaseModel, ConfigDict

__all__ = [
    "ENV_NAME_PATTERN",
    "MAX_ENV_FILE_BYTES",
    "MAX_ENV_FILE_ENTRIES",
    "MAX_ENV_VALUE_BYTES",
    "EnvEntry",
    "EnvFileRejected",
    "EnvProblem",
    "EnvRejection",
    "decode_env_bytes",
    "env_name_problem",
    "is_reserved_env_name",
    "parse_env_file",
    "serialize_env_file",
    "serialize_env_line",
]

MAX_ENV_FILE_BYTES: Final[int] = 64 * 1024
MAX_ENV_FILE_ENTRIES: Final[int] = 200
MAX_ENV_VALUE_BYTES: Final[int] = 4096
ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

EnvRejection = Literal[
    "file_too_large",
    "not_utf8",
    "syntax",
    "bad_name",
    "reserved_name",
    "duplicate_name",
    "value_too_large",
    "too_many_entries",
    "empty",
]

#: Rejections are reported one kind at a time, most structural first: a file
#: whose lines do not parse has no meaningful duplicate or size report to give.
_REJECTION_PRIORITY: Final[tuple[EnvRejection, ...]] = (
    "syntax",
    "bad_name",
    "reserved_name",
    "duplicate_name",
    "value_too_large",
    "too_many_entries",
)

_EXPORT_PREFIX: Final[re.Pattern[str]] = re.compile(r"export[ \t]+")
_DOUBLE_QUOTE_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\",
    '"': '"',
    "$": "$",
    "`": "`",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
#: The only ASCII characters a value may hold and still be written bare. None
#: of them means anything to bash inside a word that follows ``NAME=`` (``~``
#: does, and ``#`` is kept out so a bare value never looks like a comment to
#: any reader). Non-ASCII characters are never special to bash and stay bare
#: too, unless they are whitespace at either end, which the parser strips.
_BARE_VALUE_CHARS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.,:/+=@%"
)
#: Characters a single-quoted value cannot hold: the quote itself, and the line
#: breaks the one-line grammar has no room for.
_NOT_SINGLE_QUOTABLE: Final[frozenset[str]] = frozenset("'\n\r")

#: Names that change how bash, a dynamic loader, an interpreter, git or an HTTP
#: client behaves once exported. Compared case-insensitively (curl honours
#: ``https_proxy``). ``GH_TOKEN``/``GITHUB_TOKEN`` stay allowed: storing a CLI
#: token under them is the documented use (`defaults/skills/cli-auth`).
_RESERVED_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "ENV",
        "BASH_ENV",
        "BASHOPTS",
        "SHELLOPTS",
        "CDPATH",
        "GLOBIGNORE",
        "IFS",
        "PROMPT_COMMAND",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "TMPDIR",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "CURL_HOME",
        "WGETRC",
    }
)
_RESERVED_ENV_PREFIXES: Final[tuple[str, ...]] = (
    "LD_",
    "DYLD_",
    "BASH_FUNC_",
    "GIT_",
    "PIP_",
    "UV_",
    "NPM_CONFIG_",
    "YARN_",
)


class EnvProblem(BaseModel):
    """One rejected line. Carries no value — that is the point of the type."""

    model_config = ConfigDict(frozen=True)

    name: str | None
    line: int


class EnvEntry(BaseModel):
    """One accepted `NAME=value` entry and the line it came from."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: str
    line: int


class EnvFileRejected(DaimonError):
    """The whole uploaded file was rejected; nothing in it was applied.

    `rejection` says why, `problems` names the offending lines. Neither this
    exception's `str()` nor any problem it carries contains a value.
    """

    def __init__(self, rejection: EnvRejection, problems: Sequence[EnvProblem] = ()) -> None:
        super().__init__(rejection)
        self.rejection: EnvRejection = rejection
        self.problems: tuple[EnvProblem, ...] = tuple(problems)

    def __str__(self) -> str:
        if not self.problems:
            return self.rejection
        lines = ", ".join(str(problem.line) for problem in self.problems)
        return f"{self.rejection} (lines {lines})"


def is_reserved_env_name(name: str) -> bool:
    """Whether exporting `name` into the sandbox would change how its tools behave."""
    upper = name.upper()
    return upper in _RESERVED_ENV_NAMES or upper.startswith(_RESERVED_ENV_PREFIXES)


def env_name_problem(name: str) -> Literal["bad_name", "reserved_name"] | None:
    """Why `name` cannot be stored as a key, or None when it can.

    The single rule every write path applies: a POSIX shell identifier that is
    not a reserved name.
    """
    if ENV_NAME_PATTERN.fullmatch(name) is None:
        return "bad_name"
    if is_reserved_env_name(name):
        return "reserved_name"
    return None


def decode_env_bytes(raw: bytes) -> str:
    """Decode uploaded bytes to text, enforcing the size cap first.

    The size and UTF-8 boundaries live here alone so no caller can parse text
    that was never checked. Raises `EnvFileRejected` with `"file_too_large"`
    or `"not_utf8"`.
    """
    if len(raw) > MAX_ENV_FILE_BYTES:
        raise EnvFileRejected("file_too_large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise EnvFileRejected("not_utf8") from err


def _parse_double_quoted(body: str) -> str | None:
    """Parse a `"…"` value, returning None when the line is malformed."""
    out: list[str] = []
    index = 1
    while index < len(body):
        char = body[index]
        if char == '"':
            return "".join(out) if not body[index + 1 :].strip() else None
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            out.append(_DOUBLE_QUOTE_ESCAPES.get(following, "\\" + following))
            index += 2
            continue
        out.append(char)
        index += 1
    return None


def _parse_single_quoted(body: str) -> str | None:
    """Parse a `'…'` value, returning None when the line is malformed."""
    close = body.find("'", 1)
    if close == -1 or body[close + 1 :].strip():
        return None
    return body[1:close]


def _parse_value(body: str) -> str | None:
    """Parse the right-hand side of an entry line; None means a syntax error."""
    if body.startswith('"'):
        return _parse_double_quoted(body)
    if body.startswith("'"):
        return _parse_single_quoted(body)
    return body


def parse_env_file(text: str) -> tuple[EnvEntry, ...]:
    """Parse the documented subset, or reject the whole file.

    Every line is parsed before anything is raised, so the person gets the
    full picture in one pass rather than one error per upload. Raises
    `EnvFileRejected`; returns at least one entry when it returns.
    """
    entries: list[EnvEntry] = []
    problems: dict[EnvRejection, list[EnvProblem]] = {kind: [] for kind in _REJECTION_PRIORITY}
    lines_by_name: dict[str, list[int]] = {}

    for number, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.removesuffix("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        content = line.strip()
        export_match = _EXPORT_PREFIX.match(content)
        if export_match is not None:
            content = content[export_match.end() :]
        name_part, separator, value_part = content.partition("=")
        if not separator:
            # No name is reported: the token left of a missing "=" may itself be
            # a pasted secret (base64 padding makes this less theoretical).
            problems["syntax"].append(EnvProblem(name=None, line=number))
            continue
        name = name_part.strip()
        name_problem = env_name_problem(name)
        if name_problem == "bad_name":
            problems["bad_name"].append(EnvProblem(name=None, line=number))
            continue
        if name_problem == "reserved_name":
            problems["reserved_name"].append(EnvProblem(name=name, line=number))
            continue
        value = _parse_value(value_part.strip())
        if value is None or "\0" in value:
            problems["syntax"].append(EnvProblem(name=name, line=number))
            continue
        if len(value.encode()) > MAX_ENV_VALUE_BYTES:
            problems["value_too_large"].append(EnvProblem(name=name, line=number))
            continue
        lines_by_name.setdefault(name, []).append(number)
        entries.append(EnvEntry(name=name, value=value, line=number))

    for name, numbers in lines_by_name.items():
        if len(numbers) > 1:
            problems["duplicate_name"].extend(EnvProblem(name=name, line=n) for n in numbers)

    if len(entries) > MAX_ENV_FILE_ENTRIES:
        problems["too_many_entries"].extend(
            EnvProblem(name=entry.name, line=entry.line) for entry in entries[MAX_ENV_FILE_ENTRIES:]
        )

    for kind in _REJECTION_PRIORITY:
        found = problems[kind]
        if found:
            raise EnvFileRejected(kind, sorted(found, key=lambda problem: problem.line))
    if not entries:
        raise EnvFileRejected("empty")
    return tuple(entries)


def _is_bare_char(char: str) -> bool:
    return char in _BARE_VALUE_CHARS or ord(char) > 0x7F


def serialize_env_line(name: str, value: str) -> str:
    """Render one `NAME=value` line that bash sources as one inert assignment.

    Bare when every character is in `_BARE_VALUE_CHARS` or non-ASCII, single-quoted when
    the value holds no single quote or line break, double-quoted with
    ``\\``, ``"``, ``$`` and backtick escaped otherwise — never a form in
    which bash expands, substitutes or splits anything.

    Staying bare wherever that is safe is load-bearing: the assembled bytes are
    hashed into the fingerprint that decides whether a running agent's mounted
    `.env` is still current, so quoting a value that did not need it would
    change every fingerprint at once.

    Raises `ValueError` for a name that is not a POSIX identifier or a value
    holding a NUL; neither message carries the value.
    """
    if ENV_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("env name must match [A-Za-z_][A-Za-z0-9_]*")
    if "\0" in value:
        raise ValueError(f"value for {name} contains a NUL byte")
    if value and value == value.strip() and all(_is_bare_char(char) for char in value):
        return f"{name}={value}"
    if not _NOT_SINGLE_QUOTABLE & set(value):
        return f"{name}='{value}'"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'{name}="{escaped}"'


def serialize_env_file(entries: Sequence[tuple[str, str]]) -> bytes:
    """Render `(name, value)` pairs as `.env` bytes with a trailing newline.

    Returns `b""` for no entries; the caller decides what an empty file means.
    """
    if not entries:
        return b""
    body = "\n".join(serialize_env_line(name, value) for name, value in entries)
    return (body + "\n").encode()
