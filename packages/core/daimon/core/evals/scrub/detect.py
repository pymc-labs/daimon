"""Finding what to replace in a string: secrets, ids, emails, hosts, known names, then names by NER.

Matching runs on a folded view of the text (`fold`): accents and invisible marks dropped,
`\\n`, `\\u00e9`, `%20` and HTML entity escapes decoded. Known names match by word, so
`alice_smith`, `AliceSmith` and `Alice%20Smith` all match "Alice Smith". Hits are mapped back to the
original text. Overlapping hits merge into one covering both, so a longer name found by NER
is never cut short by a shorter known part. Name detection (Presidio with a spaCy model) is
optional: without it `Ner.available` is False and only the dictionary and patterns apply.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import html
import multiprocessing
import re
from array import array
from bisect import insort
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Final, get_args

from daimon.core.evals.effects import KNOWN_VERBS
from daimon.core.evals.format import Platform
from daimon.core.evals.scrub.entities import Term, fold_char, normalize

SECRET: Final = "secret"
_P_SECRET, _P_EMAIL, _P_KNOWN, _P_ID, _P_HOST, _P_NER = 6, 5, 4, 4, 3, 1


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    kind: str
    priority: int
    of: str | None = None
    canon: str | None = None
    """The dictionary term this hit matched: its fake is keyed by the term, not the spelling."""
    text: str = ""
    """The folded text of the hit."""


# ---------- folding ----------

_ESCAPES: Final = re.compile(
    r"\\u[0-9a-fA-F]{4}|\\[nrt]|(?:%[0-9A-Fa-f]{2})+|&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-z]{2,8});"
)


def _decode(escape: str) -> str:
    """The text an escape stands for; one that decodes to a control character (`%10` in
    "%10.1.2.3%") stays as written, so it cannot swallow what follows."""
    out = _decoded(escape)
    return escape if any(c < " " and c not in "\t\n\r" for c in out) else out


def _decoded(escape: str) -> str:
    if escape.startswith("\\u"):
        point = int(escape[2:], 16)
        return escape if 0xD800 <= point < 0xE000 else fold_char(chr(point))
    if escape.startswith("\\"):
        return " "
    if escape.startswith("&"):
        decoded = html.unescape(escape)
        return " " if decoded.isspace() else "".join(fold_char(c) for c in decoded)
    try:
        decoded = bytes.fromhex(escape.replace("%", "")).decode("utf-8")
    except UnicodeDecodeError:
        return escape
    return "".join(fold_char(c) for c in decoded)


@dataclass(frozen=True)
class View:
    """`text` folded for matching; `starts`/`ends` map each folded char to the source span."""

    text: str
    starts: array[int] | None = None
    ends: array[int] | None = None

    def span(self, start: int, end: int) -> tuple[int, int]:
        if self.starts is None or self.ends is None:
            return start, end
        return self.starts[start], self.ends[end - 1]


_ASCII: Final = re.compile(r"[\x00-\x7f]+")


def fold(text: str) -> View:
    if text.isascii() and "\\" not in text and "%" not in text and "&" not in text:
        return View(text)
    chars: list[str] = []
    starts = array("i")  # arrays, not lists: a long text has a million of each
    ends = array("i")

    def emit(out: str, start: int, end: int) -> None:
        chars.append(out)
        starts.extend([start] * len(out))
        ends.extend([end] * len(out))

    def plain(start: int, end: int) -> None:
        """`text[start:end]`, which holds no escape: runs of ASCII are copied as they are."""
        pos = start
        for m in _ASCII.finditer(text, start, end):
            for i in range(pos, m.start()):
                emit(fold_char(text[i]), i, i + 1)
            chars.append(m.group())
            starts.extend(range(m.start(), m.end()))
            ends.extend(range(m.start() + 1, m.end() + 1))
            pos = m.end()
        for i in range(pos, end):
            emit(fold_char(text[i]), i, i + 1)

    pos = 0
    for m in _ESCAPES.finditer(text):
        plain(pos, m.start())
        emit(_decode(m.group()), m.start(), m.end())
        pos = m.end()
    plain(pos, len(text))
    return View("".join(chars), starts, ends)


# ---------- known terms, matched by word ----------

_TOKEN: Final = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_]+")
_JOINERS: Final = frozenset(" \t\r\n_-.+")
_MIN_JOINED: Final = 6


def _joins(gap: str) -> bool:
    return len(gap) <= 3 and all(c in _JOINERS for c in gap)


def _same_gap(found: str, wanted: str) -> bool:
    return found.casefold() == wanted.casefold() or (_joins(found) and _joins(wanted))


@functools.lru_cache(maxsize=16)
def _words(view: str) -> tuple[tuple[int, int, str], ...]:
    """The words of `view`: the same text is matched against several indexes in a row."""
    return tuple((m.start(), m.end(), m.group().casefold()) for m in _TOKEN.finditer(view))


# Function words, never a name on their own; with `common_words` they guard one-word terms.
_STOP: Final = frozenset(
    {"a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for", "from", "has",
     "have", "he", "her", "his", "how", "i", "if", "in", "is", "it", "its", "me", "my", "no",
     "not", "of", "on", "or", "our", "she", "so", "than", "that", "the", "their", "them", "then",
     "there", "they", "this", "to", "up", "us", "was", "we", "what", "when", "where", "which",
     "who", "why", "will", "with", "you", "your"}
)  # fmt: skip


@functools.cache
def common_words() -> frozenset[str]:
    """Everyday English words (Faker's and spaCy's lists when installed): no one-word term
    matches them alone, and no fake is one."""
    words = set(_STOP)
    try:
        from faker.providers.lorem.en_US import Provider

        listed: list[str] = list(Provider.word_list)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        words.update(w.casefold() for w in listed)
        from spacy.lang.en.stop_words import STOP_WORDS

        words.update(w.casefold() for w in STOP_WORDS)
    except ImportError:
        pass
    return frozenset(words)


@functools.cache
def english_words() -> frozenset[str]:
    """`common_words` and the words an installed English spaCy pipeline lemmatizes, with their
    regular forms, less Faker's person names: no learned name is only these."""
    try:
        from faker.providers.person.en_US import Provider

        names = {w.casefold() for w in [*Provider.first_names, *Provider.last_names]}
        return common_words() | (_dictionary() - names)
    except (ImportError, OSError, KeyError, ValueError):  # no or another kind of model
        return common_words()


def _dictionary() -> set[str]:
    """The words an installed English spaCy pipeline lemmatizes, with their regular forms."""
    import spacy.util
    from spacy.lookups import Lookups

    for model in sorted(m for m in spacy.util.get_installed_models() if m.startswith("en_")):
        version = spacy.util.get_package_version(model)
        path = spacy.util.get_package_path(model) / f"{model}-{version}" / "lemmatizer" / "lookups"
        if path.is_dir():
            break
    else:
        return set()
    tables = Lookups().from_disk(path)  # pyright: ignore[reportUnknownMemberType]
    index, irregular = tables.get_table("lemma_index"), tables.get_table("lemma_exc")
    words: set[str] = set()
    for pos, endings in (("noun", ("s", "es")), ("verb", ("s", "es", "d", "ed", "ing")),
                         ("adj", ()), ("adv", ())):  # fmt: skip
        for word in filter(str.isalpha, index[pos]):
            words.update((word, word[:-1] + ("ies" if pos == "noun" else "ing")))
            words.update(word + end for end in endings)
        words.update(filter(str.isalpha, irregular[pos]))
    return words


@dataclass(frozen=True)
class _Entry:
    words: tuple[str, ...]
    gaps: tuple[str, ...]
    term: Term
    alone: bool = False
    """A one-word term: matched as a whole word only."""
    exact: str | None = None
    """A common word that is a name: matched only written this way."""


def _whole(view: str, start: int, end: int) -> bool:
    """Whether `view[start:end]` is a word of its own, not part of one ("PrimRose")."""
    before = view[start - 1] if start else ""
    after = view[end] if end < len(view) else ""
    return not before.isalpha() and not after.isalpha()


@dataclass
class _Bucket:
    """The entries that start with one word, by all their words; `sizes` longest first."""

    sizes: list[int] = field(default_factory=list[int])
    entries: dict[tuple[str, ...], list[_Entry]] = field(
        default_factory=dict[tuple[str, ...], list[_Entry]]
    )


class TermIndex:
    """Terms matched by word. `strict` guards one-word terms: whole words only, and a common word
    only when capitalized and written as in the dictionary. A match costs one lookup per term
    length, however many terms there are."""

    def __init__(self, *, strict: bool = False) -> None:
        self._first: dict[str, _Bucket] = {}
        self._strict = strict

    def add(self, term: Term) -> None:
        view = fold(term.text).text
        found = _words(view)
        if not found:
            return
        words = tuple(w for _, _, w in found)
        gaps = tuple(view[a[1] : b[0]] for a, b in zip(found, found[1:], strict=False))
        alone = len(words) == 1
        exact = None
        if self._strict and alone and words[0] in common_words():
            if not view.strip()[:1].isupper():
                return  # "test", "status": a lowercase common word is never matched alone
            exact = view.strip()
        entries = [_Entry(words, gaps, term, alone and self._strict, exact)]
        joined = "".join(words)
        if len(words) > 1 and all(_joins(g) for g in gaps) and len(joined) >= _MIN_JOINED:
            entries.append(_Entry((joined,), (), term))  # ALICESMITH, alicesmith
        for entry in entries:
            bucket = self._first.setdefault(entry.words[0], _Bucket())
            if len(entry.words) not in bucket.sizes:
                insort(bucket.sizes, len(entry.words), key=lambda n: -n)
            bucket.entries.setdefault(entry.words, []).append(entry)

    def find(self, view: str, *, alone: bool = True) -> Iterator[tuple[int, int, Term]]:
        """Matches in `view`; without `alone`, one-word terms are left out."""
        found = _words(view)
        words = [w for _, _, w in found]
        for i, (start, _, word) in enumerate(found):
            if (bucket := self._first.get(word)) is None:
                continue
            for n in (n for n in bucket.sizes if i + n <= len(found)):
                if not (entries := bucket.entries.get(tuple(words[i : i + n]))):
                    continue
                end = found[i + n - 1][1]
                gaps = [view[found[i + j - 1][1] : found[i + j][0]] for j in range(1, n)]
                match = next(
                    (
                        e for e in entries
                        if all(map(_same_gap, gaps, e.gaps))
                        and not (e.alone and not alone)
                        and (e.exact is None or view[start:end] == e.exact)
                        and not (e.alone and not _whole(view, start, end))
                    ),
                    None,
                )  # fmt: skip
                if match is not None:
                    yield start, end, match.term
                    break


# ---------- patterns ----------

_SECRETS: Final = [
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bxox[abprsce]-[A-Za-z0-9-]{10,}\b",
        r"\bxoxe\.xox[a-z]-[A-Za-z0-9-]{10,}",
        r"\bxapp-[A-Za-z0-9-]{10,}\b",
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b",
        r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{10,}\b",
        r"\bwhsec_[A-Za-z0-9]{16,}\b",
        r"\bhf_[A-Za-z0-9]{30,}\b",
        r"\bnpm_[A-Za-z0-9]{30,}\b",
        r"\bpypi-[A-Za-z0-9_-]{30,}",
        r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
        r"\bya29\.[A-Za-z0-9_-]{20,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b",
        r"hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+",
        r"\bdata:[\w/+.-]+;base64,[A-Za-z0-9+/=]{16,}",
        r"\b(?:SK|AC)[0-9a-f]{32}\b",
    )
]
# Key names that hold secrets, in `KEY=value` text and as JSON keys. `auth` and `authorization`
# but not `author`.
_PW_LABEL: Final = r"(?:pass(?:word|wd|phrase|code)?|(?<![a-z])(?:pwd|pin(?:_?code)?))"
_KEY_LABEL: Final = (
    r"(?:secret|token|api[_-]?key|private[_-]?key|access[_-]?key|credential|auth(?!or(?!i[sz]))"
    r"|(?:encryption|signing|account|master|client|license|shared[_-]?access)[_-]?key"
    r"|session[_-]?(?:id|key))"
)
SECRET_NAME: Final = re.compile(
    rf"pass(?:word|wd|phrase|code)?(?![a-z])|(?<![a-z])(?:pwd|pin(?:_?code)?)(?![a-z])"
    rf"|{_KEY_LABEL}|cookie",
    re.IGNORECASE,
)
# The secret is group `v` of each: the label before it stays readable. A quoted value is taken
# whole, a password's to the end of the line, any other labelled value as one word. Quantifiers
# are bounded: a long run of word characters must not make matching quadratic.
_LABEL: Final = rf"(?:{_PW_LABEL}|{_KEY_LABEL})[\w.-]{{0,40}}[\"']?[ \t]*[:=][ \t]*"
_SECRET_VALUES: Final = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"\bBearer\s+(?P<v>[A-Za-z0-9._~+/=-]{16,})",
        r"\bToken\s+(?P<v>[A-Za-z0-9_.-]{20,})",
        r"^\s*(?:proxy-)?authorization\s*:\s*(?:bearer\s+)?(?P<v>.+?)\s*$",
        r"^\s*(?:set-)?cookie\s*:\s*(?P<v>.+?)\s*$",
        rf"{_LABEL}([\"'])(?P<v>(?:(?!\1)[^\n])+)\1",
        rf"{_PW_LABEL}[\w.-]{{0,40}}[\"']?[ \t]*[:=][ \t]*"
        r"(?P<v>[^\s\"'][^\n\"',;&]*?)[ \t]*(?=[\"',;&\n]|$)",
        rf"{_LABEL}(?:(?:bearer|basic|token)\s+)?(?P<v>[^\s\"',;&]{{6,}})",
    )
]
# `Basic` and a base64 `user:password`.
_BASIC: Final = re.compile(r"\bBasic\s+(?P<v>[A-Za-z0-9+/]{8,}={0,2})(?![\w+/=])")
# A login in a home folder path.
_HOME: Final = re.compile(
    r"(?:\b[A-Za-z]:[\\/]+(?:Users|Documents and Settings)|(?<![\w.])/(?:home|Users))[\\/]+"
    r"(?P<v>[^\\/\s\"'<>:*?|]+)"
)
_GENERIC_LOGINS: Final = frozenset(
    {"public", "default", "shared", "all users", "default user", "guest", "user", "runner",
     "ubuntu", "root", "admin", "administrator", "vscode", "node", "app", "me", "you"}
)  # fmt: skip
_URL: Final = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_URL_USERINFO: Final = re.compile(r"//(?P<v>[^/@\s:?#]+:[^@\s?#]+)@")
_URL_SECRET_PARAM: Final = re.compile(
    r"[?&;#](?:[\w.-]{0,40}?(?:token|secret|signature|credential|pass(?:word|wd)?|api[_-]?key"
    r"|private[_-]?key|auth(?!or(?!i[sz])))[\w.-]{0,40}|x-amz-[\w-]+|x-goog-[\w-]+|sig|key|apikey|code"
    r"|t|pwd|hm|session|sessionid|sid|jwt|k|pw|pin)=([^&#;\s]+)",
    re.IGNORECASE,
)
_EMAIL: Final = re.compile(
    r"(?<![\w.%+-])(?!git@github\.com:)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_IDS: Final = re.compile(
    r"(?<!\d)\d{15,20}(?!\d)"  # Discord snowflakes and other long ids
    r"|\b[UWCDGT](?=[A-Z0-9]*\d)[A-Z0-9]{8,12}\b"  # Slack ids
    r"|(?<![0-9A-Za-z])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"  # UUIDs
    r"|(?<![0-9A-Za-z])(?=[a-f]*\d)(?=\d*[a-f])[0-9a-f]{32}(?![0-9A-Za-z])"  # page and doc ids
    r"|\b(?:19|28|29):[0-9A-Za-z_-]{8,}(?:@thread\.[A-Za-z0-9]+|@unq\.gbl\.spaces)?"  # Teams ids
)
_DOC_IDS: Final = re.compile(
    r"(?:docs|drive|sheets|slides)\.google\.com/(?:[\w-]+/)*?(?:d/|folders/|open\?id=)([\w-]{20,})"
)
_PHONE: Final = re.compile(
    r"(?<![\w+])(?:\+\d{8,14}"
    r"|(?:\+\d{1,3}[-. ]?)?(?:\(\d{2,4}\)[-. ]?|\d{2,4}[-. ])\d{3,4}[-. ]\d{3,5})(?![\w-])"
)
# What may be a phone number in another country's format ("06 12 34 56 78"), for `phonenumbers`
# to check; not a decimal, a list of them, a date, an IP or bare digits.
_PHONE_LIKE: Final = re.compile(r"(?<![\w.+])(?:\+|\(\d)?\d[\d ().-]{5,18}\d(?!\w)")
_PHONE_DOTS: Final = re.compile(r"\d{2,}(?:\.\d{2,}){2,}")
_PHONE_JOIN: Final = re.compile(r"[ ()+-]")
_ISO_DATE: Final = re.compile(r"\d{4}-\d\d-\d\d")
_PHONE_REGIONS: Final = ("US", "GB", "DE", "FR", "IL", "IN", "CA", "BR")
_HOST: Final = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan|intranet|home\.arpa)\b", re.IGNORECASE
)
# Any other host name, unless it is a public service's (only generic subdomains: `docs.google.com`
# stays, `zq-backups.s3.amazonaws.com` names a customer); the TLDs leave
# out file extensions ("notes.md", "app.py"), and a country may follow one ("shop.com.pk").
_PUBLIC_HOST: Final = re.compile(
    r"(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|net|org|io|co|ai|dev|app|so|me|us"
    r"|uk|de|fr|nl|eu|ca|au|in|jp|ch|se|no|fi|dk|es|it|pl|be|at|br|nz|sg|ie|il|xyz|info|biz"
    r"|cloud|tech|site|online|gg|ly|tv|fm)(?:\.[a-z]{2})?(?!\w|\.[a-z])",
    re.IGNORECASE,
)
_PUBLIC_SERVICES: Final = frozenset(
    {"github.com", "githubusercontent.com", "gitlab.com", "bitbucket.org", "google.com",
     "googleapis.com", "youtube.com", "notion.so", "notion.site", "slack.com", "discord.com",
     "discord.gg", "microsoft.com", "office.com", "sharepoint.com", "atlassian.net", "zoom.us",
     "linkedin.com", "x.com", "twitter.com", "wikipedia.org", "stackoverflow.com", "python.org",
     "pypi.org", "npmjs.com", "cloudflare.com"}
)  # fmt: skip
# Daimon, its chat platforms, its effect verbs, the services they name and public hosts, less
# everyday words ("Drive Capital" is a name).
_EVERYDAY: Final = frozenset(
    {"calendar", "chat", "docs", "drive", "git", "office", "python", "sheets", "slides", "teams",
     "web"}
)  # fmt: skip
PUBLIC_NAMES: Final = frozenset(
    name
    for name in (
        "daimon", *get_args(Platform), *KNOWN_VERBS,
        *(p for v in KNOWN_VERBS for p in v.split(".")[: 2 if v.startswith("google.") else 1]),
        *(h.split(".")[0] for h in _PUBLIC_SERVICES),
    )
    if len(name) >= 3 and name not in _EVERYDAY
)  # fmt: skip
# A hash: hex with a letter, not starting or ending in an id ("123…789abc" is an id and "abc").
_DIGEST: Final = re.compile(
    r"(?<![^\W_])(?=[0-9]*[a-fA-F])(?![0-9]{17})[0-9a-fA-F]{16,}(?<![0-9]{17})(?![^\W_])"
)
# Only a standard scheme is no name; a custom one ("acme-app://") may hold one.
_SCHEME: Final = re.compile(
    r"\b(?:https?|ftps?|sftp|ssh|git|file|wss?|s3|gs|jdbc|postgres(?:ql)?|mysql|redis|mongodb"
    r"|amqp|ldaps?)(?:\+[a-z0-9]+)?://",
    re.IGNORECASE,
)
# The tenant part of a hosted service's address names the customer.
_TENANT_HOST: Final = re.compile(
    r"\b([a-z0-9][a-z0-9-]{1,62})\.(?:atlassian\.net|zoom\.us|slack\.com|sharepoint\.com"
    r"|(?:my\.)?salesforce\.com|lightning\.force\.com|my\.site\.com|zendesk\.com|freshdesk\.com"
    r"|okta\.com|service-now\.com|webex\.com|notion\.site|monday\.com|bamboohr\.com"
    r"|myworkday\.com|looker\.com|auth0\.com|onmicrosoft\.com|github\.io|gitlab\.io"
    r"|herokuapp\.com|vercel\.app|netlify\.app|azurewebsites\.net|hubspot\.com|pagerduty\.com"
    r"|statuspage\.io|readthedocs\.io)\b",
    re.IGNORECASE,
)
_SHARED_HOSTS: Final = frozenset(
    {"www", "app", "api", "files", "hooks", "status", "support", "help", "docs", "login",
     "auth", "id", "my", "account", "accounts", "developer", "developers", "community", "drive",
     "mail", "raw", "gist", "s3", "en"}
)  # fmt: skip
_IP: Final = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"  # CGNAT, tailnets
)
_REPOS: Final = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:github|gitlab|bitbucket)\.(?:com|org)[/:]([\w.-]+/[\w-][\w.-]*?)"
        r"(?=\.git\b|[/\s?#\"')\]]|$)",
        r"(?:raw\.githubusercontent\.com|api\.github\.com/repos)/([\w.-]+/[\w-][\w.-]*?)"
        r"(?=\.git\b|[/\s?#\"')\]]|$)",
        r"(?:^|\s)(?:-R|--repo)[\s=]+([\w.-]+/[\w.-]+)",
    )
]
# The account in a social profile address (`/in/<slug>`, `/<handle>`, `/@<name>`).
_PROFILES: Final = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\blinkedin\.com/(?:in|company|school|pub)/(?P<v>[\w%.-]+)",
        r"(?<![\w-])(?:twitter|x)\.com/@?(?!(?:i|home|search|intent|share|hashtag|explore|settings"
        r"|login|messages|notifications|compose|status)\b)(?P<v>\w+)",
        r"\byoutube\.com/(?:@|(?:c|user|channel)/)(?P<v>[\w%.-]+)",
    )
]
# Phone numbers are `_phones`': Presidio's phone recognizer took half of a scrub's time.
_NER_KINDS: Final = {"PERSON": "person", "ORGANIZATION": "company", "LOCATION": "place"}
_NER_CHUNK: Final = 50_000
_NER_CACHE: Final = 100_000
_NER_PIECES: Final = 250_000
_LINE: Final = re.compile(r"[^\n]+")
_NER_BATCH: Final = 64
# spaCy holds a whole batch at once (25 chunks of 50k characters took 1.4 GB): one chunk each.
_NER_BATCH_CHARS: Final = 50_000
# spaCy labels Presidio has no entity for: ignored instead of warned about on every call.
_NER_IGNORED: Final = (
    "CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "MONEY", "DATE", "TIME", "LANGUAGE", "LAW",
    "EVENT", "PRODUCT", "WORK_OF_ART", "NORP",
)  # fmt: skip


def namey(text: str) -> bool:
    """Whether NER is worth running: free text, or a word that starts with a capital."""
    return " " in text or any(c.isupper() for c in text)


def _group(m: re.Match[str], kind: str, priority: int, base: int = 0) -> Hit:
    group = "v" if "v" in m.re.groupindex else 1
    return Hit(base + m.start(group), base + m.end(group), kind, priority)


def _basic(value: str) -> bool:
    try:
        return b":" in base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except ValueError:
        return False


def _engine(model: str) -> object | None:
    """Presidio over `model`, or None when it or the library is missing."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.nlp_engine.ner_model_configuration import (
            MODEL_TO_PRESIDIO_ENTITY_MAPPING,
        )
        from spacy.language import Language

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model}],
                "ner_model_configuration": {
                    "labels_to_ignore": list(_NER_IGNORED),
                    "model_to_presidio_entity_mapping": {
                        **MODEL_TO_PRESIDIO_ENTITY_MAPPING,
                        "FAC": "LOCATION",
                    },
                },
            }
        )
        nlp_engine = provider.create_engine()
        nlp = getattr(nlp_engine, "nlp", {}).get("en")
        assert isinstance(nlp, Language)
        # Only names are read: every other pipe (parser, tagger...) unless NER reads its output.
        for name in nlp.pipe_names:
            if name != "ner" and "ner" not in getattr(
                nlp.get_pipe(name), "listening_components", ()
            ):
                nlp.disable_pipe(name)
        return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    except (ImportError, OSError, ValueError):
        return None


def _analyze(engine: object, texts: list[str]) -> list[list[tuple[int, int, str]]]:
    """Names in each of `texts` (each at most `_NER_CHUNK` long), run through spaCy in batches."""
    from presidio_analyzer import AnalyzerEngine

    assert isinstance(engine, AnalyzerEngine)
    out: list[list[tuple[int, int, str]]] = []
    batches = engine.nlp_engine.process_batch  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    for group in _groups(texts, _NER_BATCH):
        for text, artifacts in batches(group, "en", batch_size=len(group)):
            found = engine.analyze(
                text, language="en", entities=list(_NER_KINDS), score_threshold=0.4,
                nlp_artifacts=artifacts,
            )  # fmt: skip
            out.append([(f.start, f.end, _NER_KINDS[f.entity_type]) for f in found])
    return out


def _groups(texts: list[str], count: int) -> list[list[str]]:
    """`texts` in order, in groups of at most `count` texts and `_NER_BATCH_CHARS` characters
    (a text over the limit is a group of its own)."""
    groups: list[list[str]] = []
    size = 0
    for text in texts:
        if not groups or len(groups[-1]) >= count or size + len(text) > _NER_BATCH_CHARS:
            groups.append([])
            size = 0
        groups[-1].append(text)
        size += len(text)
    return groups


_WORKER: dict[str, object] = {}


def _worker_init(model: str) -> None:
    _WORKER["engine"] = _engine(model)


def _worker_ready() -> bool:
    return _WORKER["engine"] is not None


def _worker_analyze(texts: list[str]) -> list[list[tuple[int, int, str]]]:
    return _analyze(_WORKER["engine"], texts)


class Ner:
    """Presidio over a spaCy model, built on first use. `available` tells whether it loaded.

    Results are cached per line, so a scrub, its sweep and the learning pass run the model once
    on each line.
    `prefetch` runs many texts in batches. With `jobs` above 1 the model runs only in that many
    worker processes.
    """

    def __init__(self, model: str, *, jobs: int = 1) -> None:
        self._model = model
        self._jobs = jobs
        self._engine: object | None = None
        self._ready: bool | None = None
        self._cache: dict[bytes, tuple[tuple[int, int, str], ...]] = {}
        self._pool: ProcessPoolExecutor | None = None

    @property
    def available(self) -> bool:
        if self._ready is None:
            if self._jobs > 1:
                self._pool = ProcessPoolExecutor(
                    self._jobs, mp_context=multiprocessing.get_context("spawn"),
                    initializer=_worker_init, initargs=(self._model,),
                )  # fmt: skip
                self._ready = self._pool.submit(_worker_ready).result()
            else:
                self._engine = _engine(self._model)
                self._ready = self._engine is not None
        return self._ready

    def _run(self, texts: list[str]) -> list[list[tuple[int, int, str]]]:
        if not self.available:
            return [[] for _ in texts]
        if self._pool is None:
            return _analyze(self._engine, texts)
        count = max(1, min(_NER_BATCH, -(-len(texts) // self._jobs)))
        return [r for part in self._pool.map(_worker_analyze, _groups(texts, count)) for r in part]

    def prefetch(self, texts: Iterable[str]) -> None:
        """Run the model over the lines of `texts` not seen yet, in batches."""
        self._found([p for t in texts for _, p in _pieces(t)])

    def find(self, text: str) -> list[Hit]:
        """Names in `text`."""
        pieces = _pieces(text)
        found = self._found([p for _, p in pieces])
        return [Hit(base + a, base + b, k, _P_NER) for base, p in pieces for a, b, k in found[p]]

    def _found(self, pieces: list[str]) -> dict[str, tuple[tuple[int, int, str], ...]]:
        """Names in each piece: cached, or run now in batches."""
        out: dict[str, tuple[tuple[int, int, str], ...]] = {}
        todo: list[str] = []
        for piece in dict.fromkeys(pieces):
            if (cached := self._cache.get(_digest(piece))) is None:
                todo.append(piece)
            else:
                out[piece] = cached
        for piece, names in zip(todo, self._run(todo) if todo else [], strict=True):
            if len(self._cache) >= _NER_PIECES:
                self._cache.clear()
            out[piece] = self._cache[_digest(piece)] = tuple(names)
        return out


def _pieces(text: str) -> list[tuple[int, str]]:
    """`text` cut at line ends (and at a space within every `_NER_CHUNK` characters) into what
    the model reads, with offsets: after a replacement, only the lines it changed are read
    again."""
    pieces: list[tuple[int, str]] = []
    for m in _LINE.finditer(text):
        start, end = m.span()
        while start < end:
            cut = min(end, start + _NER_CHUNK)
            if cut < end and (space := text.rfind(" ", start + 1, cut)) > 0:
                cut = space
            if namey(piece := text[start:cut]):
                pieces.append((start, piece))
            start = cut
    return pieces


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()


def _outside(digests: bytearray, hit: Hit) -> bool:
    """Whether `hit` neither starts nor ends inside a hash ("ace" in "3ace…", an id in one)."""
    starts_in = hit.start > 0 and digests[hit.start - 1] and digests[hit.start]
    return not (starts_in or (digests[hit.end] and digests[hit.end - 1]))


def _past(view: str, mask: bytearray, start: int, stop: int, step: int) -> int:
    """The first index from `start` toward `stop` past kept text and the signs after it: a hit
    that begins or ends in a kept name keeps it ("daimon" in "daimon.acme")."""
    i = start
    while i != stop and mask[i]:
        i += step
    while i != start and i != stop and not view[i].isalnum():
        i += step
    return i


def _phones(text: str) -> Iterator[tuple[int, int]]:
    import phonenumbers

    for m in _PHONE_LIKE.finditer(text):
        s = m.group()
        if "." in s and not _PHONE_DOTS.fullmatch(s) or "." not in s and not _PHONE_JOIN.search(s):
            continue
        if _ISO_DATE.search(s):
            continue
        for region in _PHONE_REGIONS:
            found = list(phonenumbers.PhoneNumberMatcher(s, region))  # valid numbers only
            yield from ((m.start() + f.start, m.start() + f.end) for f in found)
            if found:
                break


class Detector:
    """`keep` names are no hit: a hit made only of them and joiners is dropped. A name the
    model finds is cut to what it adds to them, or dropped if that is only everyday words."""

    def __init__(
        self, terms: Iterable[Term], ner: Ner | None = None, keep: Iterable[str] = ()
    ) -> None:
        self._terms: dict[str, Term] = {}
        self._index = TermIndex(strict=True)
        self._ner = ner
        self._kept = TermIndex()
        self.kept: set[str] = set()
        """The kept names, normalized."""
        for name in keep:
            self._kept.add(Term("phrase", name))
            self.kept.add(normalize(name))
        self._found: dict[tuple[bytes, bool, bool], tuple[Hit, ...]] = {}
        self.add(terms)

    @property
    def ner_available(self) -> bool:
        return self._ner is not None and self._ner.available

    def prefetch(self, texts: Iterable[str]) -> None:
        """Run NER ahead over `texts`, in batches, for `find` to reuse."""
        if self._ner is not None:
            self._ner.prefetch(v for t in texts if namey(v := fold(t).text))

    def add(self, terms: Iterable[Term]) -> list[Term]:
        """Add `terms` not known yet (by normalized text); returns the ones added."""
        added: list[Term] = []
        for term in sorted(terms, key=lambda t: len(t.text), reverse=True):
            norm = normalize(term.text)
            if norm and norm not in self._terms:
                self._terms[norm] = term
                self._index.add(term)
                added.append(term)
        if added:
            self._found.clear()
        return added

    def find(self, text: str, *, names: bool = True, words: bool = True) -> list[Hit]:
        """Non-overlapping hits in `text`, in order. `names` adds the NER pass; without `words`,
        one-word terms are left out (for JSON keys, which share words with names). Kept per
        text until the dictionary grows: a scrub and its sweep look at most texts twice."""
        key = (_digest(text), names, words)
        if (done := self._found.get(key)) is None:
            if len(self._found) >= _NER_CACHE:
                self._found.clear()
            done = self._found[key] = tuple(self._find(text, names=names, words=words))
        return list(done)

    def _find(self, text: str, *, names: bool, words: bool) -> list[Hit]:
        view = fold(text)
        digests = bytearray(len(view.text) + 1)
        for m in _DIGEST.finditer(view.text):  # no name or number starts or ends in a hash
            digests[m.start() : m.end()] = b"\1" * (m.end() - m.start())
        hits = [h for h in self._patterns(view.text) if h.kind == SECRET or _outside(digests, h)]
        found = [
            Hit(start, end, term.kind, _P_KNOWN, term.of, canon=term.text)
            for start, end, term in self._index.find(view.text, alone=words)
        ]
        if names and self._ner is not None and namey(view.text):
            found += self._ner.find(view.text)
        # A scheme or a public service's address is never a name ("github" in github.com).
        fixed = bytearray(len(view.text))
        for a, b in [m.span() for m in _SCHEME.finditer(view.text)] + _public_suffixes(view.text):
            fixed[a:b] = b"\1" * (b - a)
        hits += [h for h in found if fixed.find(1, h.start, h.end) < 0 and _outside(digests, h)]
        if self.kept:
            hits = self._unkept(view.text, hits)
        out: list[Hit] = []
        for hit in _resolve(hits):
            start, end = view.span(hit.start, hit.end)
            out.append(replace(hit, start=start, end=end, text=view.text[hit.start : hit.end]))
        return out

    def _unkept(self, view: str, hits: list[Hit]) -> list[Hit]:
        mask = bytearray(len(view))
        for a, b, _ in self._kept.find(view):
            mask[a:b] = b"\1" * (b - a)
        if not any(mask):
            return hits
        out: list[Hit] = []
        everyday = english_words()
        for hit in hits:
            if hit.kind == SECRET or mask.find(1, hit.start, hit.end) < 0:
                out.append(hit)
                continue
            rest = "".join(" " if mask[i] else view[i] for i in range(hit.start, hit.end))
            words = [w for _, _, w in _words(rest)]
            if not words or hit.priority == _P_NER and all(w in everyday for w in words):
                continue
            start, end = hit.start, hit.end
            if hit.priority == _P_NER:
                start = _past(view, mask, hit.start, hit.end, 1)
                end = _past(view, mask, hit.end - 1, hit.start - 1, -1) + 1
            out.append(replace(hit, start=start, end=end))
        return out

    def term_for(self, text: str) -> Term | None:
        return self._terms.get(normalize(text))

    def _patterns(self, text: str) -> Iterator[Hit]:
        for p in _SECRETS:
            yield from (Hit(m.start(), m.end(), SECRET, _P_SECRET) for m in p.finditer(text))
        for p in _SECRET_VALUES:
            yield from (_group(m, SECRET, _P_SECRET) for m in p.finditer(text))
        for m in _BASIC.finditer(text):
            if _basic(m.group("v")):
                yield _group(m, SECRET, _P_SECRET)
        for m in _HOME.finditer(text):
            if m.group("v").casefold() not in _GENERIC_LOGINS:
                yield _group(m, "handle", _P_ID)
        for url in _URL.finditer(text):
            for p in (_URL_USERINFO, _URL_SECRET_PARAM):
                for m in p.finditer(url.group()):
                    yield _group(m, SECRET, _P_SECRET, url.start())
        for m in _EMAIL.finditer(text):
            term = self._terms.get(normalize(m.group()))
            yield Hit(m.start(), m.end(), "email", _P_EMAIL, term.of if term else None)
        yield from (Hit(m.start(), m.end(), "id", _P_ID) for m in _IDS.finditer(text))
        yield from (_group(m, "id", _P_ID) for m in _DOC_IDS.finditer(text))
        yield from (Hit(m.start(), m.end(), "phone", _P_ID) for m in _PHONE.finditer(text))
        yield from (Hit(a, b, "phone", _P_ID) for a, b in _phones(text))
        for p in _REPOS:
            yield from (_group(m, "repo", _P_HOST) for m in p.finditer(text))
        for p in _PROFILES:
            yield from (_group(m, "handle", _P_HOST) for m in p.finditer(text))
        yield from (Hit(m.start(), m.end(), "host", _P_HOST) for m in _HOST.finditer(text))
        for m in _PUBLIC_HOST.finditer(text):
            if _public_suffix(m.group()) is None:
                yield Hit(m.start(), m.end(), "host", _P_HOST)
        for m in _TENANT_HOST.finditer(text):
            if m.group(1).casefold() not in _SHARED_HOSTS:
                yield _group(m, "company", _P_HOST)
        yield from (Hit(m.start(), m.end(), "ip", _P_HOST) for m in _IP.finditer(text))


def _public_suffix(host: str) -> int | None:
    """Where the public service's own name starts in `host` (`docs.google.com`: 5), or None.
    Only generic labels (or a tenant's, replaced on its own) may come before it."""
    labels = host.casefold().split(".")
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in _PUBLIC_SERVICES:
            if all(label in _SHARED_HOSTS for label in labels[:i]) or _TENANT_HOST.fullmatch(host):
                return sum(len(label) + 1 for label in labels[:i])
            return None
    return None


def _public_suffixes(text: str) -> list[tuple[int, int]]:
    return [
        (m.start() + at, m.end())
        for m in _PUBLIC_HOST.finditer(text)
        if (at := _public_suffix(m.group())) is not None
    ]


def _merge(a: Hit, b: Hit) -> Hit:
    """One hit covering two that cross. A part never wins over the whole: the kind comes from
    the hit that spans both when there is one, and a secret makes the whole a secret."""
    start, end = min(a.start, b.start), max(a.end, b.end)
    top = max((a, b), key=lambda h: (h.priority, h.end - h.start))
    if (top.start, top.end) == (start, end):
        return top
    if SECRET in (a.kind, b.kind):
        return Hit(start, end, SECRET, _P_SECRET)
    wide = next((h for h in (a, b) if (h.start, h.end) == (start, end)), None)
    if wide is not None:
        return replace(wide, priority=top.priority)
    return Hit(start, end, top.kind, top.priority)


def _resolve(hits: list[Hit]) -> list[Hit]:
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (h.start, -h.end)):
        if kept and hit.start < kept[-1].end:
            kept[-1] = _merge(kept[-1], hit)
        else:
            kept.append(hit)
    return kept
