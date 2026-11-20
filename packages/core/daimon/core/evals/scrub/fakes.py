"""Consistent fakes. A real value always gets the same fake: it is derived from a keyed hash.

The map keeps only `HMAC(key, kind + normalized value)` and the fake, so it holds no raw data and
can be stored with the deployment. Without the key nobody can test a guess against it.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final

from daimon.core.evals.scrub.entities import normalize

if TYPE_CHECKING:
    from faker import Faker

_SUFFIXES: Final = ("Labs", "Group", "Systems", "Partners", "Works", "Industries", "Holdings")
_ATTEMPTS: Final = 30
_DERIVED: Final = ("person_first", "person_last", "company_first")


def scrub_key(key: bytes, salt: str) -> bytes:
    """The key fakes are made with: the deployment's key bound to a tenant (or an offline salt),
    so the same name gets different fakes in different tenants and a guess made in one says
    nothing about another."""
    return hmac.new(key, f"daimon.evals.scrub\0{salt}".encode(), hashlib.sha256).digest()


def key_check(key: bytes) -> str:
    """Names a scrub key without revealing it, so a map is never mixed across keys."""
    return hmac.new(key, b"check", hashlib.sha256).hexdigest()[:16]


def keyed_hash(key: bytes, kind: str, real: str) -> str:
    return hmac.new(key, f"{kind}\0{normalize(real)}".encode(), hashlib.sha256).hexdigest()


def shape_preserving(real: str, rng: random.Random) -> str:
    """Random characters of the same classes: digits stay digits, a leading id letter stays."""
    out: list[str] = []
    for i, ch in enumerate(real):
        if ch.isdigit():
            out.append(str(rng.randrange(1 if i == 0 else 0, 10)))
        elif ch.isupper() and ch.isascii() and i > 0:
            out.append(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        elif ch.islower() and ch.isascii():
            out.append(rng.choice("abcdefghijklmnopqrstuvwxyz"))
        else:
            out.append(ch)
    return "".join(out)


def adapt(matched: str, fake: str) -> str:
    """`fake` written the way `matched` was: case, and joined like a slug when it was one."""
    if len(matched) > 1 and matched.isupper():
        fake = fake.upper()
    elif matched.islower():
        fake = fake.lower()
    if not any(c.isspace() for c in matched) and " " in fake:
        slug = re.search(r"[-_.+]", matched)
        fake = fake.replace(" ", slug.group() if slug else "-" if matched.islower() else "")
    return fake


class FakeMap:
    """Real to fake, by kind. `known` rows come from storage; new ones collect in `added`."""

    def __init__(
        self,
        key: bytes,
        *,
        known: Mapping[tuple[str, str], str] | None = None,
        forbidden: Iterable[str] = (),
    ) -> None:
        self._key = key
        self._known = dict(known or {})
        words = {normalize(f) for f in forbidden}
        self._forbidden_words = {w for w in words if " " not in w}
        self._forbidden_phrases = [w for w in words if " " in w]
        self._taken: set[tuple[str, str]] = {(k, normalize(v)) for (k, _), v in self._known.items()}
        self.added: dict[tuple[str, str], str] = {}
        self._faker: Faker | None = None

    def _gen(self) -> Faker:
        if self._faker is None:
            from faker import Faker

            self._faker = Faker("en_US")
        return self._faker

    def forbid(self, words: Iterable[str]) -> None:
        """Real words learned later, which no new fake may hold."""
        for word in map(normalize, words):
            if " " in word:
                self._forbidden_phrases.append(word)
            else:
                self._forbidden_words.add(word)

    def values(self) -> list[str]:
        """Every fake handed out so far, for the sweep to tell fakes from leftovers."""
        return [*self._known.values(), *self.added.values()]

    def fake(self, kind: str, real: str, *, of: str | None = None) -> str:
        if kind == "company" and of:
            return self.fake("company", of)  # a short form takes the company's own fake
        digest = keyed_hash(self._key, kind, real)
        found = self._known.get((kind, digest)) or self.added.get((kind, digest))
        if found is not None:
            return found
        candidate = ""
        for attempt in range(_ATTEMPTS + 1):
            candidate = self._make(kind, real, of, digest, attempt)
            if attempt == _ATTEMPTS:
                candidate = f"{candidate} {attempt}"
            clash = (kind, normalize(candidate)) in self._taken
            if kind in _DERIVED or not (clash or self._has_real(candidate)):
                break
        self._taken.add((kind, normalize(candidate)))
        self.added[(kind, digest)] = candidate
        return candidate

    def _has_real(self, candidate: str) -> bool:
        """Whether `candidate` holds a known real word, which a sweep would then flag."""
        text = normalize(candidate)
        return bool(set(re.findall(r"[\w']+", text)) & self._forbidden_words) or any(
            p in text for p in self._forbidden_phrases
        )

    def _make(self, kind: str, real: str, of: str | None, digest: str, attempt: int) -> str:
        seed = int(hashlib.sha256(f"{digest}:{attempt}".encode()).hexdigest()[:16], 16)
        rng, faker = random.Random(seed), self._gen()
        faker.seed_instance(seed)
        if kind in _DERIVED:
            whole = "company" if kind == "company_first" else "person"
            parts = self.fake(whole, of or real).split()
            return parts[-1] if kind == "person_last" else parts[0]
        if kind == "person":
            first = faker.first_name()
            return first if len(real.split()) == 1 else f"{first} {faker.last_name()}"
        if kind in ("email", "handle"):
            return self._account(kind, of, faker, attempt)
        if kind == "company":
            return f"{faker.last_name()} {rng.choice(_SUFFIXES)}"
        if kind in ("project", "channel", "host"):
            words = f"{faker.word()} {faker.word()}"
            return f"{words.replace(' ', '-')}.example.test" if kind == "host" else words.title()
        if kind == "repo":
            owner, _, name = real.partition("/")
            owner_fake = adapt(owner.lower(), self.fake("company", owner))
            if not name:
                return owner_fake
            return f"{owner_fake}/{adapt(name.lower(), self.fake('project', name))}"
        if kind == "phrase":
            return " ".join(faker.words(3))
        if kind == "place":
            return faker.city()
        if kind == "ip":
            return f"192.0.2.{rng.randrange(1, 255)}"
        return shape_preserving(real, rng)  # id, phone and other shaped values

    def _account(self, kind: str, of: str | None, faker: Faker, attempt: int) -> str:
        if of:
            parts = re.sub(r"[^a-z ]", "", self.fake("person", of).lower()).split()
            if kind == "email":
                local = ".".join(parts[:1] + parts[-1:]) if len(parts) > 1 else parts[0]
            else:
                local = "".join(parts[:1] + [p[0] for p in parts[1:]])
        else:
            local = re.sub(r"[^a-z0-9._-]", "", faker.user_name().lower())
        local = f"{local}{attempt or ''}"
        return f"{local}@example.com" if kind == "email" else local
