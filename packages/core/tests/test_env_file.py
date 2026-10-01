"""Tests for `daimon.core.env_file`: the literal `.env` subset, and its refusals.

The parser's whole job is to be boring and predictable about other people's
secrets, so these tests pin the documented subset line by line, prove the
rejection paths name lines rather than contents, and round-trip nasty values
through the serializer.
"""

from __future__ import annotations

import pytest
from daimon.core.env_file import (
    MAX_ENV_FILE_BYTES,
    MAX_ENV_FILE_ENTRIES,
    MAX_ENV_VALUE_BYTES,
    EnvFileRejected,
    decode_env_bytes,
    env_alias_of,
    env_alias_shadowed,
    env_collision_line,
    env_import_collisions,
    env_name_hard_denied,
    env_name_member_writable,
    env_name_problem,
    env_row_skip_reason,
    env_shadow_phrase,
    is_reserved_env_name,
    parse_env_file,
    serialize_env_file,
    serialize_env_line,
)

_DOCUMENTED_SUBSET: list[tuple[str, str, str]] = [
    ("plain", "A=1", "1"),
    ("empty value", "A=", ""),
    ("spaces around the equals", "A =  1", "1"),
    ("trailing whitespace stripped", "A=1   ", "1"),
    ("export prefix", "export A=1", "1"),
    ("export with a tab", "export\tA=1", "1"),
    ("single quotes keep spaces", "A='  spaced  '", "  spaced  "),
    ("single quotes keep backslashes", r"A='a\nb'", r"a\nb"),
    ("double quotes keep spaces", 'A="  spaced  "', "  spaced  "),
    ("double-quoted newline escape", r'A="a\nb"', "a\nb"),
    ("double-quoted carriage return escape", r'A="a\rb"', "a\rb"),
    ("double-quoted tab escape", r'A="a\tb"', "a\tb"),
    ("double-quoted quote escape", r'A="a\"b"', 'a"b'),
    ("double-quoted backslash escape", r'A="a\\b"', "a\\b"),
    ("unrecognised escape stays literal", r'A="a\qb"', r"a\qb"),
    ("hash inside an unquoted value is literal", "A=pa#ss", "pa#ss"),
    ("hash inside a quoted value is literal", 'A="pa#ss"', "pa#ss"),
    ("equals inside a value", "A=b=c", "b=c"),
    ("equals inside a quoted value", 'A="b=c"', "b=c"),
    ("CRLF line ending", "A=1\r", "1"),
    ("quotes inside an unquoted value", "A=say'hi", "say'hi"),
    ("single-quoted segments joined by an escaped quote", r"A='it'\''s'", "it's"),
    ("value that is only an escaped quote", r"A=''\'''", "'"),
]


@pytest.mark.parametrize(
    ("label", "line", "expected"),
    _DOCUMENTED_SUBSET,
    ids=[case[0] for case in _DOCUMENTED_SUBSET],
)
def test_parse_env_file_accepts_documented_subset(label: str, line: str, expected: str) -> None:
    entries = parse_env_file(line)
    assert len(entries) == 1, f"{label}: exactly one entry should parse from {line!r}"
    assert entries[0].name == "A", f"{label}: the name should be A"
    assert entries[0].value == expected, f"{label}: value should be {expected!r}"


def test_parse_env_file_ignores_comments_and_blank_lines() -> None:
    text = "# leading comment\n\n   \n  # indented comment\nA=1\n\nB=2\n# trailing comment\n"
    entries = parse_env_file(text)
    assert [(entry.name, entry.value, entry.line) for entry in entries] == [
        ("A", "1", 5),
        ("B", "2", 7),
    ], "comments and blank lines are skipped, and kept line numbers are 1-based file lines"


def test_parse_env_file_rejects_whole_file_when_a_name_is_invalid() -> None:
    text = "GOOD=1\nBAD-NAME=super-secret-value\n9LEADING=another-secret\n"
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(text)
    error = caught.value
    assert error.rejection == "bad_name", "an unusable name rejects the file as bad_name"
    assert [problem.line for problem in error.problems] == [2, 3], (
        "every offending line number is reported, not just the first"
    )
    assert all(problem.name is None for problem in error.problems), (
        "an unusable name is never echoed back: the token may itself be a secret"
    )
    rendered = str(error)
    for secret in ("super-secret-value", "another-secret", "BAD-NAME", "9LEADING"):
        assert secret not in rendered, f"str(EnvFileRejected) must not contain {secret!r}"


def test_parse_env_file_rejects_the_whole_file_rather_than_applying_the_good_lines() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("GOOD=1\nBAD-NAME=2\n")
    assert caught.value.rejection == "bad_name", (
        "one bad line rejects the upload; a half-applied secrets file is worse than none"
    )


def test_parse_env_file_reports_every_duplicate_line_number() -> None:
    text = "A=1\nB=2\nA=3\nC=4\nA=5\n"
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(text)
    error = caught.value
    assert error.rejection == "duplicate_name", "a repeated name rejects the file"
    assert [problem.line for problem in error.problems] == [1, 3, 5], (
        "all three lines that set A are reported, in file order"
    )
    assert {problem.name for problem in error.problems} == {"A"}, (
        "a valid duplicated name is safe to name and identifies the problem"
    )


@pytest.mark.parametrize(
    "name", ["LD_PRELOAD", "PATH", "BASH_ENV", "GIT_CONFIG_KEY_0", "https_proxy", "NODE_OPTIONS"]
)
def test_parse_env_file_rejects_a_reserved_name_and_names_it(name: str) -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(f"OK=1\n{name}=/tmp/x.so\n")
    assert caught.value.rejection == "reserved_name", f"{name} must be refused as reserved"
    assert [(p.name, p.line) for p in caught.value.problems] == [(name, 2)]


def test_parse_env_file_rejects_a_nul_in_a_value() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("A=x\0y\n")
    assert caught.value.rejection == "syntax", "a NUL has no shell representation"


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("LD_PRELOAD", True),
        ("ld_library_path", True),
        ("DYLD_INSERT_LIBRARIES", True),
        ("GIT_SSH_COMMAND", True),
        ("GIT_ASKPASS", True),
        ("PYTHONSTARTUP", True),
        ("PROMPT_COMMAND", True),
        ("HTTP_PROXY", True),
        ("NO_PROXY", True),
        ("PIP_INDEX_URL", True),
        ("LC_ALL", True),
        ("LC_CTYPE", True),
        ("lc_all", True),
        ("LANG", True),
        ("LANGUAGE", True),
        ("GCONV_PATH", True),
        ("NLSPATH", True),
        ("LOCPATH", True),
        ("HOSTALIASES", True),
        ("RES_OPTIONS", True),
        ("LOCALDOMAIN", True),
        ("TERMINFO", True),
        ("TERMCAP", True),
        ("OPENAI_API_KEY", False),
        ("GH_TOKEN", False),
        ("GITHUB_TOKEN", False),
        ("PATHWAY_KEY", False),
        ("LANGUAGE_API_KEY", False),
    ],
)
def test_is_reserved_env_name(name: str, reserved: bool) -> None:
    assert is_reserved_env_name(name) is reserved
    assert (env_name_problem(name) == "reserved_name") is reserved


def test_parse_env_file_does_not_interpolate_or_substitute() -> None:
    text = 'A=$HOME\nB=${HOME}\nC=$(whoami)\nD="`whoami`"\nE="$HOME/x"\n'
    values = {entry.name: entry.value for entry in parse_env_file(text)}
    assert values == {
        "A": "$HOME",
        "B": "${HOME}",
        "C": "$(whoami)",
        "D": "`whoami`",
        "E": "$HOME/x",
    }, "variables, braces, command substitution and backticks are stored literally"


@pytest.mark.parametrize(
    ("label", "text", "line"),
    [
        ("unterminated double quote", 'A="oops\n', 1),
        ("unterminated single quote", "A='oops\n", 1),
        ("trailing junk after a closing quote", 'B=1\nA="ok" junk\n', 2),
        ("quoted value split across lines", 'A="one\ntwo"\n', 1),
    ],
    ids=["double", "single", "trailing junk", "multi-line"],
)
def test_parse_env_file_rejects_unterminated_quote_with_its_line_number(
    label: str, text: str, line: int
) -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(text)
    error = caught.value
    assert error.rejection == "syntax", f"{label}: an unclosed quote is a syntax rejection"
    assert line in [problem.line for problem in error.problems], (
        f"{label}: line {line} should be named in the problems"
    )


def test_parse_env_file_rejects_a_line_with_no_equals_without_naming_it() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("A=1\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj\n")
    error = caught.value
    assert error.rejection == "syntax", "a line that is not NAME=value is a syntax rejection"
    assert [(problem.line, problem.name) for problem in error.problems] == [(2, None)], (
        "a line with no '=' may be a bare pasted secret; report the line, never the token"
    )


def test_parse_env_file_rejects_an_empty_file() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("# only a comment\n\n")
    assert caught.value.rejection == "empty", "a file with no entries is rejected as empty"
    assert caught.value.problems == (), "an empty file has no offending line to point at"


def test_parse_env_file_rejects_a_value_over_the_value_cap() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("SMALL=1\nBIG=" + "x" * (MAX_ENV_VALUE_BYTES + 1) + "\n")
    error = caught.value
    assert error.rejection == "value_too_large", "an oversize value rejects the file"
    assert [(problem.line, problem.name) for problem in error.problems] == [(2, "BIG")], (
        "the oversize value's line and (valid) name are reported, never its content"
    )


def test_parse_env_file_accepts_a_value_at_exactly_the_value_cap() -> None:
    value = "x" * MAX_ENV_VALUE_BYTES
    entries = parse_env_file(f"BIG={value}\n")
    assert entries[0].value == value, "the cap is inclusive: exactly MAX_ENV_VALUE_BYTES is fine"


def test_parse_env_file_measures_value_size_in_bytes_not_characters() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("BIG=" + "é" * (MAX_ENV_VALUE_BYTES // 2 + 1) + "\n")
    assert caught.value.rejection == "value_too_large", (
        "two-byte characters count as two bytes, as they will on disk"
    )


def test_parse_env_file_rejects_more_entries_than_the_entry_cap() -> None:
    text = "".join(f"K{index}=v\n" for index in range(MAX_ENV_FILE_ENTRIES + 2))
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(text)
    error = caught.value
    assert error.rejection == "too_many_entries", "more than the cap rejects the file"
    assert [problem.line for problem in error.problems] == [
        MAX_ENV_FILE_ENTRIES + 1,
        MAX_ENV_FILE_ENTRIES + 2,
    ], "the entries past the cap are the ones reported"


def test_parse_env_file_accepts_exactly_the_entry_cap() -> None:
    text = "".join(f"K{index}=v\n" for index in range(MAX_ENV_FILE_ENTRIES))
    assert len(parse_env_file(text)) == MAX_ENV_FILE_ENTRIES, "the entry cap is inclusive"


def test_parse_env_file_reports_syntax_before_duplicates() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("A=1\nA=2\nnot an entry line\n")
    assert caught.value.rejection == "syntax", (
        "a file whose lines do not parse has no meaningful duplicate report to give"
    )


_NASTY_VALUES: list[tuple[str, str]] = [
    ("empty", ""),
    ("plain", "hunter2"),
    ("leading space", " lead"),
    ("trailing space", "trail "),
    ("only spaces", "   "),
    ("double quote", 'say "hi"'),
    ("single quote", "it's"),
    ("both quotes", '"it\'s"'),
    ("backslash", "a\\b"),
    ("trailing backslash", "ab\\"),
    ("newline", "line1\nline2"),
    ("carriage return", "line1\rline2"),
    ("tab", "a\tb"),
    ("hash", "pa#ss"),
    ("starts with hash", "#notacomment"),
    ("equals", "a=b=c"),
    ("dollar", "$HOME/${X}/$(id)"),
    ("backtick", "`whoami`"),
    ("unicode", "café-π-密钥"),
    ("starts with a quote", '"quoted-looking'),
    ("looks like export", "export A=1"),
    ("4 KiB blob", "x" * MAX_ENV_VALUE_BYTES),
    ("escape sequence text", r"literal \n and \t"),
    ("tilde", "~/x"),
    ("semicolon", "a;b"),
    ("pipe and ampersand", "a|b&c"),
    ("redirects", "a>b<c"),
    ("glob", "*?[a]"),
    ("braces and parens", "{a}(b)"),
    ("quote and dollar", "it's $(id)"),
    ("quote and backtick", "it's `id`"),
    ("leading non-breaking space", "\u00a0x"),
]


@pytest.mark.parametrize(("label", "value"), _NASTY_VALUES, ids=[case[0] for case in _NASTY_VALUES])
def test_env_values_round_trip_exactly(label: str, value: str) -> None:
    raw = serialize_env_file([("SECRET", value)])
    entries = parse_env_file(decode_env_bytes(raw))
    assert len(entries) == 1, f"{label}: one serialized entry should parse back as one entry"
    assert entries[0].value == value, f"{label}: value must round-trip byte-for-byte"


def test_serialize_env_line_leaves_a_plain_value_unquoted() -> None:
    assert serialize_env_line("KEY", "plain-value") == "KEY=plain-value", (
        "quoting a value that does not need it would change every stored .env fingerprint"
    )


def test_serialize_env_line_quotes_and_escapes_a_value_that_needs_it() -> None:
    assert serialize_env_line("KEY", 'a"b\\c\nd\te$`') == 'KEY="a\\"b\\\\c\\nd\te\\$\\`"', (
        "quote, backslash, newline, dollar and backtick are escaped inside a double-quoted value"
    )


def test_serialize_env_line_single_quotes_a_value_with_shell_metacharacters() -> None:
    assert serialize_env_line("KEY", "$(id);`x`") == "KEY='$(id);`x`'", (
        "a value bash would expand is single-quoted, where nothing is special"
    )


def test_serialize_env_line_writes_a_single_quote_without_a_backslash_inside_quotes() -> None:
    assert serialize_env_line("KEY", 'it\'s "$x"') == r"""KEY='it'\''s "$x"'""", (
        "a quote closes, is escaped outside any quotes, and reopens; nothing is escaped "
        "inside quotes, so no locale can merge a backslash into a character"
    )


@pytest.mark.parametrize("name", ["1A", "A-B", "A B", "", "A=B"])
def test_serialize_env_line_refuses_a_name_that_is_not_an_identifier(name: str) -> None:
    with pytest.raises(ValueError, match="env name"):
        serialize_env_line(name, "v")


def test_serialize_env_line_refuses_a_nul_without_echoing_the_value() -> None:
    with pytest.raises(ValueError, match="NUL") as caught:
        serialize_env_line("KEY", "sec\0ret")
    assert "sec" not in str(caught.value), "the error must not carry the value"


def test_serialize_env_file_is_empty_bytes_for_no_entries() -> None:
    assert serialize_env_file([]) == b"", "no entries means no file content at all"


def test_serialize_env_file_joins_lines_with_a_trailing_newline() -> None:
    assert serialize_env_file([("A", "1"), ("B", "2")]) == b"A=1\nB=2\n", (
        "lines are newline-joined with a trailing newline"
    )


def test_decode_env_bytes_rejects_oversize_before_parsing() -> None:
    raw = b"A=" + b"x" * MAX_ENV_FILE_BYTES
    with pytest.raises(EnvFileRejected) as caught:
        decode_env_bytes(raw)
    assert caught.value.rejection == "file_too_large", (
        "the size cap is checked on bytes, before any decode or parse work"
    )
    assert caught.value.problems == (), "a whole-file rejection points at no line"


def test_decode_env_bytes_accepts_exactly_the_file_cap() -> None:
    raw = b"A=" + b"x" * (MAX_ENV_FILE_BYTES - 2)
    assert decode_env_bytes(raw).startswith("A=xxx"), "the file cap is inclusive"


def test_decode_env_bytes_rejects_non_utf8() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        decode_env_bytes(b"A=\xff\xfe\x00binary\n")
    assert caught.value.rejection == "not_utf8", "undecodable bytes are rejected as not_utf8"
    assert isinstance(caught.value.__cause__, UnicodeDecodeError), (
        "the decode failure is preserved as the cause"
    )


def test_decode_env_bytes_accepts_utf8_text() -> None:
    assert decode_env_bytes("A=café\n".encode()) == "A=café\n", "valid utf-8 decodes unchanged"


#: Every known way an environment NAME alone makes some later tool run code or
#: redirect traffic. None may be storable, by anyone, under any write path.
_EXEC_ON_ENV_NAMES: list[str] = [
    "BASH_ENV",
    "ENV",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "NODE_OPTIONS",
    "PERL5OPT",
    "PERL5LIB",
    "RUBYOPT",
    "RUBYLIB",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "TAR_OPTIONS",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_EXTERNAL_DIFF",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "LESSOPEN",
    "LESSCLOSE",
    "BROWSER",
    "PAGER",
    "MANPAGER",
    "EDITOR",
    "VISUAL",
    "PROMPT_COMMAND",
    "PS4",
    "BASH_FUNC_x%%",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "PIP_INDEX_URL",
    "NPM_CONFIG_REGISTRY",
    "GOFLAGS",
    "GOPROXY",
    "CARGO_HOME",
    "KUBECONFIG",
    "DOCKER_HOST",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "GCONV_PATH",
    "NLSPATH",
    "LC_ALL",
    "LC_CTYPE",
    "LANG",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "PATH",
    "SHELL",
    "TERMINFO",
    "INPUTRC",
    # R / Julia / Lua / PHP / pytest / OpenSSL / Electron / Jupyter code loaders
    "R_PROFILE_USER",
    "R_PROFILE",
    "R_ENVIRON_USER",
    "R_ENVIRON",
    "R_LIBS_USER",
    "R_LIBS",
    "R_USER",
    "JULIA_DEPOT_PATH",
    "JULIA_LOAD_PATH",
    "LUA_INIT",
    "LUA_INIT_5_4",
    "PHP_INI_SCAN_DIR",
    "PYTEST_PLUGINS",
    "PYTEST_ADDOPTS",
    "OPENSSL_MODULES",
    "OPENSSL_ENGINES",
    "OPENSSL_CONF",
    "ELECTRON_RUN_AS_NODE",
    "IPYTHONDIR",
    "JUPYTER_CONFIG_DIR",
    "JUPYTER_PATH",
    "MPLBACKEND",
    "MPLCONFIGDIR",
    # shell / libc
    "ZDOTDIR",
    "BASH_XTRACEFD",
    "HISTFILE",
    "GLIBC_TUNABLES",
    "MALLOC_CONF",
    "MALLOC_ARENA_MAX",
    "TCMALLOC_RELEASE_RATE",
    # build toolchain
    "CC",
    "CXX",
    "CPP",
    "LD",
    "AR",
    "LDSHARED",
    "MAKEFILES",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
    "MAKEFLAGS",
    "CMAKE_TOOLCHAIN_FILE",
    # config dirs / keyrings / TLS
    "XDG_CONFIG_HOME",
    "XDG_DATA_DIRS",
    "GCLOUD_CONFIG_DIR",
    "AZURE_CONFIGDIR",
    "GNUPGHOME",
    "PGPASSFILE",
    "PGSERVICEFILE",
    "PGSSLROOTCERTFILE",
    "PGSYSCONFDIR",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "SSLKEYLOGFILE",
    "GIT_BROWSER",
    "WWW_BROWSER",
    # endpoint / host retargeting and tool config
    "TF_CLI_ARGS",
    "TF_CLI_CONFIG_FILE",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "GH_HOST",
    "GITHUB_API_URL",
    "CLOUDSDK_API_ENDPOINT_OVERRIDES_STORAGE",
    "HELM_PLUGINS",
    "CONDA_PREFIX",
    "BUNDLE_GEMFILE",
    "POETRY_REPOSITORIES_X_URL",
]


@pytest.mark.parametrize("name", _EXEC_ON_ENV_NAMES)
def test_every_exec_on_env_name_is_hard_denied_for_everyone(name: str) -> None:
    assert is_reserved_env_name(name), f"{name} must be hard-denied"
    # An admin is refused too; a name that is not even an identifier (e.g.
    # BASH_FUNC_x%%) is caught one step earlier, as bad_name.
    assert env_name_problem(name, is_admin=True) in ("reserved_name", "bad_name"), (
        f"{name} must be refused even for an admin"
    )
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(f"OK_TOKEN=1\n{name}=x\n")
    assert caught.value.rejection in ("reserved_name", "bad_name")


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "ACME_PAT",
    ],
)
def test_ordinary_credential_names_are_writable_by_a_member(name: str) -> None:
    assert env_name_member_writable(name), f"{name} is an ordinary credential a member may add"
    assert env_name_problem(name, is_admin=False) is None


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "SERVICE_HOST",
        "MY_DSN",
        "NOTES",
        "CONFIG",
        "X",
        # identity / targeting names retarget a credential: admin-only
        "SNOWFLAKE_USER",
        "JIRA_USERNAME",
        "GCP_PROJECT_ID",
        "AZURE_TENANT",
        "OPENAI_ORG",
        "STRIPE_ACCOUNT",
        "SLACK_WORKSPACE",
        "BILLING_EMAIL",
        "AWS_REGION",
        "GOOGLE_CLOUD_PROJECT",
    ],
)
def test_a_member_cannot_write_a_non_credential_name_but_an_admin_can(name: str) -> None:
    # None of these is a tool control, so an admin may set them…
    assert env_name_problem(name, is_admin=True) is None, f"an admin may set {name}"
    # …but a member may not: redirect targets and free-form names are refused.
    assert env_name_problem(name, is_admin=False) == "not_credential_name"


def test_a_member_upload_rejects_a_non_credential_name() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("API_KEY=ok\nDATABASE_URL=postgres://x\n", member_writable_only=True)
    assert caught.value.rejection == "not_credential_name"
    assert [p.name for p in caught.value.problems] == ["DATABASE_URL"]


def test_an_admin_upload_accepts_a_non_credential_name_that_is_not_hard_denied() -> None:
    entries = parse_env_file("API_KEY=ok\nDATABASE_URL=postgres://x\n")
    assert {e.name for e in entries} == {"API_KEY", "DATABASE_URL"}


@pytest.mark.parametrize(
    "name", ["NPM_TOKEN", "CARGO_REGISTRY_TOKEN", "UV_PUBLISH_TOKEN", "DOCKER_PASSWORD"]
)
def test_opaque_secret_exceptions_escape_their_prefix_and_nothing_else(name: str) -> None:
    """Each exception is a plain secret its tool never reads as config or a path."""
    assert not env_name_hard_denied(name)
    neighbour = name.split("_", 1)[0] + "_CONFIG_USERCONFIG"
    assert env_name_hard_denied(neighbour), "the prefix still blocks everything else"


# --- follow-up: more exec-on-env names, path-valued keys, alias groups -------

_FOLLOWUP_HARD_DENY: list[str] = [
    "RSYNC_RSH",
    "CVS_RSH",
    "GIT_PASSCOMMAND",
    "SSHPASSCOMMAND",
    "SVN_SSH",
    "CONFIG_SITE",
    "GCC_EXEC_PREFIX",
    "CCACHE_PREFIX",
    "RUSTC_WORKSPACE_WRAPPER",
    "RUSTDOC",
    "COR_PROFILER",
    "COR_PROFILER_PATH",
    "CORECLR_PROFILER",
    "CORECLR_ENABLE_PROFILING",
    "DOTNET_ADDITIONAL_DEPS",
    "LUA_PATH",
    "LUA_PATH_5_4",
    "LUA_CPATH",
    "LUA_CPATH_5_4",
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
]


@pytest.mark.parametrize("name", _FOLLOWUP_HARD_DENY)
def test_followup_exec_on_env_names_are_hard_denied(name: str) -> None:
    assert env_name_hard_denied(name), f"{name} must be refused for everyone"
    assert env_name_problem(name, is_admin=True) == "reserved_name"


@pytest.mark.parametrize(
    "name", ["KAFKA_CLIENT_KEY", "ETCDCTL_KEY", "DB_PASSWORD_FILE", "SERVICE_ACCOUNT_KEY_FILE"]
)
def test_path_valued_key_names_are_admin_only(name: str) -> None:
    """A name that holds a PATH the tool opens is not a secret a member may add."""
    assert not env_name_hard_denied(name), f"an admin may still set {name}"
    assert env_name_problem(name, is_admin=False) == "not_credential_name"
    assert env_name_problem(name, is_admin=True) is None


def test_member_suffix_hint_lists_exactly_the_accepted_suffixes() -> None:
    from daimon.core.env_file import MEMBER_SECRET_SUFFIX_HINT

    for suffix in ("_KEY", "_API_KEY", "_KEY_ID", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD"):
        assert suffix in MEMBER_SECRET_SUFFIX_HINT, f"the refusal must name {suffix}"
        assert env_name_member_writable(f"ACME{suffix}"), f"ACME{suffix} must be writable"


@pytest.mark.parametrize(
    ("adding", "held", "shadowed"),
    [
        ("GITHUB_TOKEN", ["GH_TOKEN"], "GH_TOKEN"),
        ("GH_TOKEN", ["GITHUB_TOKEN"], "GITHUB_TOKEN"),
        ("ANTHROPIC_AUTH_TOKEN", ["ANTHROPIC_API_KEY"], "ANTHROPIC_API_KEY"),
        ("AWS_SESSION_TOKEN", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"], "AWS_ACCESS_KEY_ID"),
        ("OPENAI_KEY", ["OPENAI_API_KEY"], "OPENAI_API_KEY"),
        ("GH_TOKEN", ["OPENAI_API_KEY"], None),
        ("GH_TOKEN", ["GH_TOKEN"], None),
    ],
)
def test_env_alias_shadowed(adding: str, held: list[str], shadowed: str | None) -> None:
    assert env_alias_shadowed(adding, held) == shadowed


def test_an_import_collides_on_an_alias_and_says_which_key() -> None:
    entries = parse_env_file("OPENAI_API_KEY=a\nGITHUB_TOKEN=secret-b\n")
    held = {"GH_TOKEN"}
    collisions = env_import_collisions(entries, held)
    assert [e.name for e in collisions] == ["GITHUB_TOKEN"]
    line = env_collision_line(collisions[0], held)
    assert "GITHUB_TOKEN would replace GH_TOKEN" in line
    assert "secret-b" not in line, "no value in the line"


def test_skip_reason_is_the_assemblers_rule() -> None:
    assert env_row_skip_reason("API_KEY", "v") is None
    assert env_row_skip_reason("TAR_OPTIONS", "v") == "reserved_name"
    assert env_row_skip_reason("A-B", "v") == "bad_name"
    assert env_row_skip_reason("API_KEY", "a\0b") == "nul_in_value"


@pytest.mark.parametrize("name", ["VIMINIT", "EXINIT"])
def test_editor_init_commands_are_hard_denied(name: str) -> None:
    assert env_name_hard_denied(name)


@pytest.mark.parametrize("name", ["POSTGRES_SSL_KEY", "MQTT_TLS_KEY"])
def test_tls_key_file_names_are_admin_only(name: str) -> None:
    assert not env_name_hard_denied(name)
    assert env_name_problem(name, is_admin=False) == "not_credential_name"
    assert env_name_problem(name, is_admin=True) is None


@pytest.mark.parametrize(
    ("adding", "held"),
    [
        ("AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"),
        ("GLAB_TOKEN", "GITLAB_TOKEN"),
        ("FLY_ACCESS_TOKEN", "FLY_API_TOKEN"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
        ("NODE_AUTH_TOKEN", "NPM_TOKEN"),
    ],
)
def test_more_alias_groups(adding: str, held: str) -> None:
    assert env_alias_shadowed(adding, [held]) == held
    assert env_alias_shadowed(held, [adding]) == adding


def test_a_file_with_both_names_of_an_alias_pair_is_refused() -> None:
    """Whichever name the tool prefers would silently win: the file is ambiguous."""
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("OPENAI_API_KEY=a\nGH_TOKEN=b\nGITHUB_TOKEN=c\n")
    assert caught.value.rejection == "alias_pair"
    assert [(p.name, p.line) for p in caught.value.problems] == [("GITHUB_TOKEN", 3)]


def test_alias_pair_refusal_copy_names_the_key_not_the_value() -> None:
    from daimon.core.continuity.messages import render_env_import_rejected

    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("GH_TOKEN=secret-b\nGITHUB_TOKEN=secret-c\n")
    text = render_env_import_rejected(
        caught.value.rejection, caught.value.problems, target_name="tester"
    )
    assert "GITHUB_TOKEN" in text and "same tool" in text
    assert "secret-" not in text


# --- aliases vs credential families ------------------------------------------


def test_a_fresh_agent_may_import_a_whole_aws_family_in_one_file() -> None:
    entries = parse_env_file(
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\nAWS_SECRET_ACCESS_KEY=s\nAWS_SESSION_TOKEN=t\n"
    )
    assert [e.name for e in entries] == [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ]
    assert env_import_collisions(entries, held=()) == ()


def test_the_two_aws_session_token_names_are_still_mutually_exclusive() -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file("AWS_SESSION_TOKEN=a\nAWS_SECURITY_TOKEN=b\n")
    assert caught.value.rejection == "alias_pair"


@pytest.mark.parametrize(
    ("adding", "held"),
    [
        ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
        ("AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"),
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    ],
)
def test_adding_to_a_stored_family_changes_it(adding: str, held: str) -> None:
    """A member must not graft a session token or secret onto someone else's access key."""
    assert env_alias_shadowed(adding, [held]) == held
    assert env_alias_of(adding, [held]) is None, "a family member is not an alias"
    assert env_shadow_phrase(adding, held) == f"would change the credential {held} belongs to"


def test_an_import_onto_a_stored_family_collides_and_says_so() -> None:
    entries = parse_env_file("AWS_SESSION_TOKEN=t\n")
    held = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    collisions = env_import_collisions(entries, held)
    assert [e.name for e in collisions] == ["AWS_SESSION_TOKEN"]
    assert "would change the credential AWS_ACCESS_KEY_ID belongs to" in env_collision_line(
        collisions[0], held
    )


@pytest.mark.parametrize(
    "name", ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]
)
def test_git_commit_identity_names_are_writable_by_everyone(name: str) -> None:
    """Commit identity is plain text to git, never a command, path or config."""
    from daimon.core.env_file import env_name_hard_denied, env_name_member_writable

    assert not env_name_hard_denied(name)
    assert env_name_member_writable(name)


@pytest.mark.parametrize(
    "name",
    [
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_ASKPASS",
        "GIT_EXEC_PATH",
        "GIT_PROXY_COMMAND",
        "GIT_AUTHOR_DATE",
        "GIT_DIR",
    ],
)
def test_every_other_git_name_stays_refused(name: str) -> None:
    from daimon.core.env_file import env_name_hard_denied

    assert env_name_hard_denied(name)
