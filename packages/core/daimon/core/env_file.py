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
    are no escapes inside. Segments joined by ``\\'`` concatenate with a
    literal ``'`` at each join, as in the shell: ``'it'\\''s'`` is ``it's``.
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
a small set bash never treats specially, otherwise it is single-quoted (a
``'`` becomes ``'\\''``), and a value that contains a newline or a carriage
return is double-quoted with ``\\``, ``"``, ``$`` and backtick escaped and line breaks
written as ``\\n``/``\\r``. Bash and `parse_env_file` read every such line back
to the same value, except that an escaped line break is the two characters
``\\n``/``\\r`` to bash. A NUL cannot be represented and is refused.

Two layers of name policy
-------------------------
Values are safe to `source` (above), but the NAME a value is exported under
is itself a capability: ``LD_PRELOAD``, ``BASH_ENV``, ``PYTHONSTARTUP``,
``TAR_OPTIONS`` and hundreds like them make some later tool run code or
redirect traffic. A finite denylist cannot keep up, so the rule is
structural, in two layers.

- **Hard deny** (`env_name_hard_denied`) — applied everywhere a key is
  stored *and* when the mounted `.env` is assembled, for everyone including
  admins. Any name that looks like an interpreter/tool control: a set of
  exact names, plus suffix and prefix classes (``*_OPTIONS``, ``*OPTS``,
  ``*_PATH``/``PATH``, ``*_COMMAND``, ``*STARTUP``, ``*RC``, ``*_PROXY``,
  ``*_CONFIG``, ``*_PRELOAD``, ``LD_*``, ``GIT_*``, ``LC_*`` …) and the ones
  that redirect an SDK's own traffic (``*_BASE_URL``, ``*_API_BASE``,
  ``*_ENDPOINT``, ``*_INDEX_URL``, ``*_REGISTRY*``).
- **Member allowlist** (`env_name_member_writable`) — applied when a
  non-admin writes a key (a Discord/Slack form or upload submitted by a
  non-admin, ``request_agent_key``, ``self_write_file`` and every agent
  key). A member may only write ``^[A-Z][A-Z0-9_]{1,63}$`` that ends in a
  secret suffix (``_KEY``, ``_KEY_ID``, ``_TOKEN``, ``_SECRET``,
  ``_PASSWORD``, ``_PASSPHRASE``, ``_PAT``) and is not hard-denied — never an
  identity or targeting name (``*_USER``, ``*_ID``, ``*_ORG``, ``*_REGION``)
  or a ``*_URL``/``*_HOST`` (redirection), with ``GH_TOKEN``/``GITHUB_TOKEN``
  kept for the documented CLI use. An admin may write any name that is not
  hard-denied.

`env_name_problem(name, is_admin=…)` is the one entry every write path calls;
the store and the `.env` assembler apply the hard-deny layer alone, since
neither knows who is writing.

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
from collections.abc import Iterable, Sequence
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
    "env_alias_of",
    "env_alias_shadowed",
    "env_collision_line",
    "env_import_collisions",
    "env_related_held",
    "env_row_skip_reason",
    "env_shadow_phrase",
    "MEMBER_SECRET_SUFFIX_HINT",
    "env_name_hard_denied",
    "env_name_member_writable",
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
    "not_credential_name",
    "duplicate_name",
    "alias_pair",
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
    "not_credential_name",
    "duplicate_name",
    "alias_pair",
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
#: Characters a single-quoted value cannot hold: the line breaks the one-line
#: grammar has no room for.
_NOT_SINGLE_QUOTABLE: Final[frozenset[str]] = frozenset("\n\r")

#: Suffixes that name a SECRET value and nothing else. A non-admin member may
#: only write a key whose name ends in one of these. Identity and targeting
#: suffixes (``_USER``, ``_ID``, ``_EMAIL``, ``_ACCOUNT``, ``_ORG``,
#: ``_PROJECT``, ``_REGION`` …) are admin-only: they retarget a credential or,
#: like ``R_LIBS_USER``, load code.
_MEMBER_ALLOW_SUFFIXES: Final[tuple[str, ...]] = (
    "_KEY",
    "_API_KEY",
    "_KEY_ID",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_PASSPHRASE",
    "_PAT",
)
#: Suffixes a member never writes: a value under them points the agent's own
#: tooling somewhere else.
#: ``*_CLIENT_KEY``, ``*_SSL_KEY``, ``*_TLS_KEY`` and ``ETCDCTL_KEY`` end in
#: ``_KEY`` but name a TLS key *file* the tool opens, not a secret value.
#: ``*_FILE`` names a path for the same reason; it never ends in a secret
#: suffix, so it is admin-only by construction, and listed here to make that
#: explicit. The ``*_FILE`` names that make a tool EXECUTE the file are
#: hard-denied instead.
_MEMBER_DENY_SUFFIXES: Final[tuple[str, ...]] = (
    "_URL",
    "_HOST",
    "_URI",
    "_ENDPOINT",
    "_CLIENT_KEY",
    "_SSL_KEY",
    "_TLS_KEY",
    "_FILE",
)
_MEMBER_DENY_EXACT: Final[frozenset[str]] = frozenset({"ETCDCTL_KEY"})
#: Bare secret words a member may use as a whole key name, plus the two names
#: kept for the documented CLI-token use (`defaults/skills/cli-auth`).
_MEMBER_ALLOW_EXACT: Final[frozenset[str]] = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "TOKEN",
        "KEY",
        "APIKEY",
        "API_KEY",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "PASSPHRASE",
        "PAT",
    }
)
#: Secret names that a hard-deny prefix or substring would otherwise catch but
#: that their tool reads only as an opaque credential, never as configuration
#: or a path. Exempt from Layer 1; still subject to Layer 2 like any name.
_HARD_DENY_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {"NPM_TOKEN", "CARGO_REGISTRY_TOKEN", "UV_PUBLISH_TOKEN", "DOCKER_PASSWORD"}
)
#: The accepted member suffixes, as refusal copy shows them.
MEMBER_SECRET_SUFFIX_HINT: Final[str] = (
    ", ".join(_MEMBER_ALLOW_SUFFIXES[:-1]) + " or " + _MEMBER_ALLOW_SUFFIXES[-1]
)
_MEMBER_NAME_SHAPE: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]{1,63}")

#: HARD DENY. Exact names whose mere presence hands control to some later tool.
#: Compared case-insensitively (curl honours ``https_proxy``, tar ``TAR_OPTIONS``).
#: ``GH_TOKEN``/``GITHUB_TOKEN`` are NOT here — they are plain credentials.
_HARD_DENY_EXACT: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "IFS",
        "ENV",
        "BASH_ENV",
        "BASHOPTS",
        "SHELLOPTS",
        "CDPATH",
        "GLOBIGNORE",
        "FIGNORE",
        "PROMPT_COMMAND",
        "PS0",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "READLINE_LINE",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "MANPAGER",
        "MANOPT",
        "BROWSER",
        "LESS",
        "LESSOPEN",
        "LESSCLOSE",
        "LESSSECURE",
        "LESSKEY",
        "TAR_OPTIONS",
        "ZIPOPT",
        "ZIP",
        "UNZIP",
        "GZIP",
        "XZ_OPT",
        "XZ_DEFAULTS",
        "BZIP2",
        "GCONV_PATH",
        "NLSPATH",
        "LOCPATH",
        "HOSTALIASES",
        "RESOLV_HOST_CONF",
        "RES_OPTIONS",
        "LOCALDOMAIN",
        "TERMINFO",
        "TERMINFO_DIRS",
        "TERMCAP",
        "INPUTRC",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "GIT_PAGER",
        "KUBECONFIG",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "BOTO_CONFIG",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "CURL_HOME",
        "WGETRC",
        "GITCONFIG",
        "LANG",
        "LANGUAGE",
        "GOFLAGS",
        "GOPROXY",
        "GOPRIVATE",
        "GOSUMDB",
        "GONOSUMDB",
        "GONOSUMCHECK",
        "GOPATH",
        "GOROOT",
        "GOBIN",
        "GOENV",
        "GOCACHE",
        "GOMODCACHE",
        "GOTOOLCHAIN",
        "RUSTFLAGS",
        "RUSTDOCFLAGS",
        "RUSTC_WRAPPER",
        "RUSTC",
        "PHP_INI_SCAN_DIR",
        "PYTEST_PLUGINS",
        "PYTEST_ADDOPTS",
        "OPENSSL_MODULES",
        "OPENSSL_ENGINES",
        "OPENSSL_CONF",
        "ELECTRON_RUN_AS_NODE",
        "IPYTHONDIR",
        "MPLBACKEND",
        "MPLCONFIGDIR",
        "ZDOTDIR",
        "BASH_XTRACEFD",
        "HISTFILE",
        "GLIBC_TUNABLES",
        "CC",
        "CXX",
        "CPP",
        "LD",
        "AR",
        "LDSHARED",
        "MAKEFILES",
        "GNUPGHOME",
        "PGSYSCONFDIR",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "GH_HOST",
        "GITHUB_API_URL",
        "SSLKEYLOGFILE",
        "SVN_SSH",
        "CONFIG_SITE",
        "GCC_EXEC_PREFIX",
        "CCACHE_PREFIX",
        "RUSTC_WORKSPACE_WRAPPER",
        "RUSTDOC",
        "DOTNET_ADDITIONAL_DEPS",
        "CLOUDSDK_PYTHON",
        "COVERAGE_PROCESS_START",
        "COVERAGE_RCFILE",
        "NODE_REPL_EXTERNAL_MODULE",
        "PYENV_VERSION",
        "LESSKEYIN",
        "LESSKEY_SYSTEM",
        "LESSEDIT",
        "MANROFFOPT",
        "AS",
        "NM",
        "RANLIB",
        "STRIP",
        "FC",
        "MAKE",
        "MAKESHELL",
        "GOINSECURE",
        "GONOPROXY",
        "GODEBUG",
        "FCEDIT",
        "ANSIBLE_VAULT_PASSWORD_FILE",
        "VIMINIT",
        "EXINIT",
    }
)
#: HARD DENY prefixes.
_HARD_DENY_PREFIXES: Final[tuple[str, ...]] = (
    "LD_",
    "DYLD_",
    "BASH_FUNC_",
    "GIT_",
    "LC_",
    "SSL_CERT_",
    "PIP_",
    "UV_",
    "NPM_",
    "YARN_",
    "PNPM_",
    "CARGO_",
    "RUSTUP_",
    "R_",
    "JULIA_",
    "LUA_INIT",
    "JUPYTER_",
    "MALLOC_",
    "TCMALLOC_",
    "CMAKE_",
    "XDG_",
    "TF_CLI_",
    "CLOUDSDK_API_ENDPOINT_OVERRIDES_",
    "HELM_",
    "CONDA_",
    "BUNDLE_",
    "POETRY_",
    "COR_PROFILER",
    "CORECLR_",
    "LUA_PATH",
    "LUA_CPATH",
    "MAVEN_",
    "GRADLE_",
    "DOCKER_",
    "PYTHON",
    "PERL",
    "RUBY",
)
#: HARD DENY suffixes: any name ending one of these is a tool/interpreter
#: control or a traffic redirect, whatever its prefix.
_HARD_DENY_SUFFIXES: Final[tuple[str, ...]] = (
    "PATH",
    "_OPTIONS",
    "OPTS",
    "_OPT",
    "_ENV",
    "STARTUP",
    "_COMMAND",
    "_CMD",
    "ASKPASS",
    "_PROXY",
    "_HOME",
    "RC",
    "_CONFIG",
    "_CONF",
    "_PRELOAD",
    "_LIBRARY",
    "_LIBRARY_PATH",
    "_INSERT_LIBRARIES",
    "_EDITOR",
    "_PAGER",
    "_SHELL",
    "_HOOK",
    "_HOOKS",
    "_CA_BUNDLE",
    "_CA_CERTS",
    "_CERT_FILE",
    "_CERT_DIR",
    "_BASE_URL",
    "_API_BASE",
    "_ENDPOINT",
    "_INDEX_URL",
    "_REGISTRY",
    "_REGISTRIES",
    "_MIRROR",
    "_MIRRORS",
    "_CREDENTIALS",
    "_CREDENTIALS_FILE",
    "_TOOL_OPTIONS",
    "FLAGS",
    "_CONFIG_DIR",
    "CONFIGDIR",
    "_BROWSER",
    "_RSH",
    "PASSCOMMAND",
)
#: HARD DENY substrings: a redirect target can sit mid-name (``*_REGISTRY_*``).
_HARD_DENY_SUBSTRINGS: Final[tuple[str, ...]] = (
    "_REGISTRY",
    "_BASE_URL",
    "_INDEX_URL",
    "_ENDPOINT_URL",
)
#: libpq reads a file path from any ``PG…FILE`` (``PGPASSFILE``, ``PGSERVICEFILE``).
_HARD_DENY_PATTERN: Final[re.Pattern[str]] = re.compile(r"PG[A-Z0-9_]*FILE")


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


def env_name_hard_denied(name: str) -> bool:
    """LAYER 1. Whether exporting `name` would hand control to some later tool.

    Structural, not a bare list: a set of exact names plus prefix, suffix and
    substring classes covering interpreter and archiver option variables,
    loader/locale/CA overrides, package-manager config and the endpoint
    variables that redirect an SDK's own traffic. Applied to every stored key
    and again when the `.env` is assembled, for admins too.
    """
    upper = name.upper()
    if upper in _HARD_DENY_EXCEPTIONS:
        return False
    if upper in _HARD_DENY_EXACT:
        return True
    if _HARD_DENY_PATTERN.fullmatch(upper) is not None:
        return True
    if upper.startswith(_HARD_DENY_PREFIXES):
        return True
    if upper.endswith(_HARD_DENY_SUFFIXES):
        return True
    return any(part in upper for part in _HARD_DENY_SUBSTRINGS)


def env_name_member_writable(name: str) -> bool:
    """LAYER 2. Whether a non-admin may write a key under `name`.

    A short upper-snake name that ends in one of `_MEMBER_ALLOW_SUFFIXES`
    (the list `MEMBER_SECRET_SUFFIX_HINT` shows people) and is not hard-denied,
    not a path/redirect name (`_MEMBER_DENY_SUFFIXES`: ``*_URL``, ``*_HOST``,
    ``*_FILE``, ``*_CLIENT_KEY`` …) and not ``ETCDCTL_KEY``; plus bare secret
    words and the two documented CLI-token names. An admin is not bound by
    this — only by `env_name_hard_denied`.
    """
    if env_name_hard_denied(name):
        return False
    if name in _MEMBER_ALLOW_EXACT:
        return True
    if name in _MEMBER_DENY_EXACT:
        return False
    if _MEMBER_NAME_SHAPE.fullmatch(name) is None:
        return False
    if name.endswith(_MEMBER_DENY_SUFFIXES):
        return False
    return name.endswith(_MEMBER_ALLOW_SUFFIXES)


#: ALIASES: mutually exclusive names one tool reads as the same credential, so
#: adding one where another is stored retargets that tool as surely as
#: overwriting it (`gh` prefers ``GH_TOKEN`` over ``GITHUB_TOKEN``). Any member
#: added while another is held is a REPLACEMENT of the held one, and a file
#: that sets two of them is ambiguous.
_ALIAS_GROUPS: Final[tuple[frozenset[str], ...]] = (
    frozenset({"GH_TOKEN", "GITHUB_TOKEN"}),
    frozenset({"GITLAB_TOKEN", "GLAB_TOKEN"}),
    frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"}),
    frozenset({"AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"}),
    frozenset({"OPENAI_API_KEY", "OPENAI_KEY"}),
    frozenset({"FLY_API_TOKEN", "FLY_ACCESS_TOKEN"}),
    frozenset({"NPM_TOKEN", "NODE_AUTH_TOKEN"}),
    frozenset({"GOOGLE_API_KEY", "GEMINI_API_KEY"}),
    frozenset({"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}),
)
#: FAMILIES: complementary names one tool reads TOGETHER as one credential
#: (the AWS SDK pairs the access key with whatever secret and session token
#: sit beside it). A fresh agent may receive a whole family at once, in one
#: file; adding or changing any member while another is stored changes the
#: credential the stored member belongs to, so it is a REPLACEMENT too.
_CREDENTIAL_FAMILIES: Final[tuple[frozenset[str], ...]] = (
    frozenset(
        {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"}
    ),
)


def _first_held(name: str, groups: Sequence[frozenset[str]], held: set[str]) -> str | None:
    for group in groups:
        if name in group:
            for other in sorted(group - {name}):
                if other in held:
                    return other
    return None


def env_alias_of(name: str, held: Iterable[str]) -> str | None:
    """A held name that is a mutually exclusive alias of `name`, or None."""
    return _first_held(name, _ALIAS_GROUPS, set(held))


def env_alias_shadowed(name: str, held: Iterable[str]) -> str | None:
    """The held key that adding `name` would retarget or change, or None.

    An alias of `name` (it would be shadowed) or another member of its
    credential family (the stored credential would change). `name` itself
    being held is an ordinary replacement and not this function's business.
    """
    held_set = set(held)
    return _first_held(name, _ALIAS_GROUPS, held_set) or _first_held(
        name, _CREDENTIAL_FAMILIES, held_set
    )


def env_related_held(name: str, held: Iterable[str]) -> frozenset[str]:
    """The held names that are aliases of `name` or members of its family.

    Snapshotted before a write and compared under the key-set lock: a change
    between the two means someone added or removed a related credential after
    the replacement gate was decided, so the write must not proceed. An
    unchanged set — an admin rotating `AWS_SECRET_ACCESS_KEY` beside a stored
    `AWS_ACCESS_KEY_ID` — is not a conflict.
    """
    related: set[str] = set()
    for group in (*_ALIAS_GROUPS, *_CREDENTIAL_FAMILIES):
        if name in group:
            related |= group - {name}
    return frozenset(related & set(held))


def env_shadow_phrase(name: str, held_name: str) -> str:
    """How adding `name` affects `held_name`, for refusal copy (no values)."""
    if env_alias_of(name, [held_name]) is not None:
        return f"would replace {held_name}, which the same tool reads as this credential"
    return f"would change the credential {held_name} belongs to"


def env_import_collisions(entries: Sequence[EnvEntry], held: Iterable[str]) -> tuple[EnvEntry, ...]:
    """Entries of an import that would replace a held key, directly or by alias.

    A whole-file import only ever adds, so both count: the same name, and a
    different name the same tool reads as a held credential.
    """
    held_set = set(held)
    return tuple(
        entry
        for entry in entries
        if entry.name in held_set or env_alias_shadowed(entry.name, held_set) is not None
    )


def env_collision_line(entry: EnvEntry, held: Iterable[str]) -> str:
    """One refusal line for an import collision: names and a line number, no value."""
    held_set = set(held)
    if entry.name in held_set:
        return f"line {entry.line}: {entry.name} is already set."
    shadowed = env_alias_shadowed(entry.name, held_set)
    assert shadowed is not None, "only a colliding entry gets a collision line"
    return f"line {entry.line}: {entry.name} {env_shadow_phrase(entry.name, shadowed)}."


def env_row_skip_reason(name: str, value: str) -> str | None:
    """Why a stored row is left out of the mounted `.env`, or None if it mounts.

    The one rule for both the assembler and anything that names mounted keys:
    a non-identifier, a hard-denied name, or a NUL in the value.
    """
    if ENV_NAME_PATTERN.fullmatch(name) is None:
        return "bad_name"
    if env_name_hard_denied(name):
        return "reserved_name"
    if "\0" in value:
        return "nul_in_value"
    return None


def is_reserved_env_name(name: str) -> bool:
    """Back-compat alias for the hard-deny layer."""
    return env_name_hard_denied(name)


def env_name_problem(
    name: str, *, is_admin: bool = False
) -> Literal["bad_name", "reserved_name", "not_credential_name"] | None:
    """Why `name` cannot be stored as a key by this writer, or None when it can.

    ``bad_name`` — not a POSIX identifier at all.
    ``reserved_name`` — hard-denied for everyone (Layer 1).
    ``not_credential_name`` — a non-admin wrote a name outside the member
    allowlist (Layer 2); an admin could have.

    Callers that do not know the writer (the store, the `.env` assembler) leave
    `is_admin` at its default and so apply Layer 1 alone.
    """
    if ENV_NAME_PATTERN.fullmatch(name) is None:
        return "bad_name"
    if env_name_hard_denied(name):
        return "reserved_name"
    if not is_admin and not env_name_member_writable(name):
        return "not_credential_name"
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
    """Parse a `'…'` value, returning None when the line is malformed.

    Adjacent segments joined by ``\\'`` (the shell's ``'it'\\''s'``) read as one
    value with a literal ``'`` at each join.
    """
    out: list[str] = []
    start = 0
    while True:
        close = body.find("'", start + 1)
        if close == -1:
            return None
        out.append(body[start + 1 : close])
        rest = body[close + 1 :]
        if rest.startswith("\\''"):
            out.append("'")
            start = close + 3
            continue
        return "".join(out) if not rest.strip() else None


def _parse_value(body: str) -> str | None:
    """Parse the right-hand side of an entry line; None means a syntax error."""
    if body.startswith('"'):
        return _parse_double_quoted(body)
    if body.startswith("'"):
        return _parse_single_quoted(body)
    return body


def parse_env_file(text: str, *, member_writable_only: bool = False) -> tuple[EnvEntry, ...]:
    """Parse the documented subset, or reject the whole file.

    Every line is parsed before anything is raised, so the person gets the
    full picture in one pass rather than one error per upload. Raises
    `EnvFileRejected`; returns at least one entry when it returns.

    Hard-denied names are rejected always. `member_writable_only=True` (an
    upload submitted by a non-admin) additionally rejects any name outside the
    member credential allowlist, with `"not_credential_name"`.
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
        name_problem = env_name_problem(name, is_admin=not member_writable_only)
        if name_problem == "bad_name":
            problems["bad_name"].append(EnvProblem(name=None, line=number))
            continue
        if name_problem == "reserved_name":
            problems["reserved_name"].append(EnvProblem(name=name, line=number))
            continue
        if name_problem == "not_credential_name":
            problems["not_credential_name"].append(EnvProblem(name=name, line=number))
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

    # Two mutually exclusive aliases: whichever the tool prefers silently wins,
    # so the file is ambiguous. Each later name of a pair is reported against
    # the earlier one. Members of one credential FAMILY (the AWS bundle) belong
    # together and may arrive in one file.
    seen: list[str] = []
    for entry in entries:
        if env_alias_of(entry.name, seen) is not None:
            problems["alias_pair"].append(EnvProblem(name=entry.name, line=entry.line))
        seen.append(entry.name)

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

    Bare when every character is in `_BARE_VALUE_CHARS` or non-ASCII;
    single-quoted (a ``'`` written as ``'\\''``) when the value holds no line
    break; double-quoted with ``\\``, ``"``, ``$`` and backtick escaped
    otherwise — never a form in which bash expands, substitutes or splits
    anything.

    Only the double-quoted form uses a backslash inside quotes, and a
    backslash can be swallowed as the trail byte of a multibyte character in
    a locale such as BIG5 or GBK. That form is kept to values with a line
    break, and the locale variables are reserved names, so no key can switch
    the sandbox into such a locale.

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
        quoted = value.replace("'", "'\\''")
        return f"{name}='{quoted}'"
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
