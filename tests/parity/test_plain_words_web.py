"""The web app's text uses everyday words too.

`test_plain_words` scans the Python; this scans the strings and text in `web/src`, with the
same internal terms and the same " · " rule, plus no em dash. "Workspace" is the web app's
own word for a team, so it is allowed here. Mock data, the UI kit and tests are not text
anyone reads in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_no_foreign_platform_names import _REPO  # pyright: ignore[reportPrivateUsage]
from .test_plain_words import _internal_terms  # pyright: ignore[reportPrivateUsage]

_SRC = _REPO / "web" / "src"
_SKIPPED = ("/mock/", "/components/ui/", "/test/")
#: Whole files that may use a term, and why.
_ALLOWED_FILES: dict[str, str] = {}
#: Terms a part of the app may use, and why: (path prefix, term).
_ALLOWED_TERMS: dict[tuple[str, str], str] = {
    ("web/src/modules/agents/", "MCP"): "the agent builder's own word for a tool server",
}

_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w\"'`])//[^\n]*", re.DOTALL)
_STRING = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`")
_JSX_TEXT = re.compile(r">([^<>{}\n]+)<")


def _files() -> list[Path]:
    return [
        path
        for path in sorted(_SRC.rglob("*.ts*"))
        if not any(part in path.as_posix() for part in _SKIPPED)
        and ".test." not in path.name
        and path.relative_to(_REPO).as_posix() not in _ALLOWED_FILES
    ]


def _texts(path: Path) -> list[tuple[int, str]]:
    """Each string and run of JSX text with a space in it: prose, not a key or a path."""
    source = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), path.read_text())
    found: list[tuple[int, str]] = []
    for pattern in (_STRING, _JSX_TEXT):
        for m in pattern.finditer(source):
            text = next(g for g in m.groups() if g is not None)
            if re.search(r"\s", text) and re.search(r"[A-Za-z]{3}", text):
                found.append((source.count("\n", 0, m.start()) + 1, text))
    return found


def _allowed(rel: str, term: str) -> bool:
    return any(rel.startswith(prefix) and term == t for (prefix, t) in _ALLOWED_TERMS)


def test_web_text_uses_everyday_words() -> None:
    offences = []
    for path in _files():
        rel = path.relative_to(_REPO).as_posix()
        for line, text in _texts(path):
            terms = {t for t in _internal_terms(text, slack=True) if not _allowed(rel, t)}
            if terms:
                offences.append(f"{rel}:{line}: {sorted(terms)} in {text[:90]!r}")
    assert offences == [], (
        "web text uses an internal term; say it in everyday words:\n" + "\n".join(offences)
    )


def test_web_text_has_no_dot_separators_or_em_dashes() -> None:
    offences = [
        f"{path.relative_to(_REPO).as_posix()}:{line}: {text[:90]!r}"
        for path in _files()
        for line, text in _texts(path)
        if " · " in text or "—" in text
    ]
    assert offences == [], (
        'web text uses " · " or an em dash; use a comma or a new line:\n' + "\n".join(offences)
    )


def test_every_web_allowance_is_still_needed() -> None:
    used = {
        (prefix, term)
        for (prefix, term) in _ALLOWED_TERMS
        for path in _files()
        if path.relative_to(_REPO).as_posix().startswith(prefix)
        for _, text in _texts(path)
        if term in _internal_terms(text, slack=True)
    }
    assert set(_ALLOWED_TERMS) - used == set(), "no longer needed, remove them"


def test_the_web_scan_sees_text() -> None:
    sample = _SRC / "pages" / "Invite.tsx"
    texts = [text for _, text in _texts(sample)]
    assert "Accept and start" in texts
    assert any("Ask the person who sent it" in t for t in texts)
