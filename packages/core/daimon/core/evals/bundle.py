"""Bundle files: folder and zip sources, the writer, Ed25519 signatures and encryption.

Encrypted bundles (`*.daimon-eval.enc`) are the line `DAIMON-EVAL-ENC1`, a JSON header line
with the scrypt parameters, salt and nonce, then the AES-256-GCM ciphertext of the zip.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import zipfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Final, Literal

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from daimon.core.evals.blobs import blob_path
from daimon.core.evals.format import (
    BlobEntry,
    BundleContents,
    BundleScrub,
    FileEntry,
    JsonObject,
    Manifest,
    PrincipalRef,
    RunEntry,
    Signature,
    SourceInfo,
    SuiteEntry,
    canonical_json,
    sha256_hex,
    to_document,
)
from daimon.core.evals.validate import (
    MANIFEST_PATH,
    BundleSource,
    InvalidDocument,
    LoadedRun,
    LoadedSuite,
)

ENC_MAGIC: Final = b"DAIMON-EVAL-ENC1"
_SCRYPT: Final = {"n": 131072, "r": 8, "p": 1}
DEFAULT_MAX_BYTES: Final = 2 * 1024**3
MAX_ENTRIES: Final = 100_000
_ZIP_ERRORS: Final = (
    zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, KeyError, OSError,
)  # fmt: skip


class DirSource:
    """A bundle unpacked into a folder."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def names(self) -> frozenset[str]:
        return frozenset(
            p.relative_to(self._root).as_posix() for p in self._root.rglob("*") if p.is_file()
        )

    def read(self, path: str) -> bytes:
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise InvalidDocument([f"{path}: outside the bundle folder"])
        return target.read_bytes()


class ZipSource:
    """A zipped bundle. Refuses unsafe or repeated names, too many entries, oversized content
    and, past `deadline` (a `time.monotonic` value), any further read."""

    def __init__(
        self,
        data: bytes | IO[bytes],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        deadline: float | None = None,
    ) -> None:
        self.deadline = deadline
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data)
            infos = self._zip.infolist()
        except _ZIP_ERRORS:
            raise InvalidDocument(["not a bundle zip or encrypted bundle"]) from None
        if len(infos) > MAX_ENTRIES:
            raise InvalidDocument([f"bundle has more than {MAX_ENTRIES} files"])
        if sum(i.file_size for i in infos) > max_bytes:
            raise InvalidDocument([f"bundle unpacks to more than {max_bytes} bytes"])
        seen: set[str] = set()
        for info in infos:
            parts = PurePosixPath(info.filename).parts
            if info.filename.startswith("/") or ".." in parts or "\\" in info.filename:
                raise InvalidDocument([f"{info.filename}: unsafe path in zip"])
            if info.filename in seen:
                raise InvalidDocument([f"{info.filename}: in the zip twice"])
            seen.add(info.filename)

    def names(self) -> frozenset[str]:
        return frozenset(n for n in self._zip.namelist() if not n.endswith("/"))

    def read(self, path: str) -> bytes:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise InvalidDocument(["the bundle takes too long to read"])
        try:
            return self._zip.read(path)
        except _ZIP_ERRORS:
            raise InvalidDocument([f"{path}: unreadable in the zip"]) from None


def open_bundle(
    data: bytes | IO[bytes],
    *,
    passphrase: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    deadline: float | None = None,
) -> ZipSource:
    """A source over zip or encrypted-zip bytes, or a seekable file of them."""
    head = data[: len(ENC_MAGIC)] if isinstance(data, bytes) else data.read(len(ENC_MAGIC))
    if not isinstance(data, bytes):
        data.seek(0)
    if head == ENC_MAGIC:
        if not passphrase:
            raise InvalidDocument(["bundle is encrypted: give its passphrase"])
        data = decrypt(data if isinstance(data, bytes) else data.read(), passphrase)
    return ZipSource(data, max_bytes=max_bytes, deadline=deadline)


def open_path(path: Path, *, passphrase: str | None = None) -> BundleSource:
    """A source over a bundle folder, zip or encrypted file on disk."""
    return (
        DirSource(path) if path.is_dir() else open_bundle(path.read_bytes(), passphrase=passphrase)
    )


# ---------- encryption ----------


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {"kdf": "scrypt", **_SCRYPT, "salt": _b64(salt), "nonce": _b64(nonce)}
    sealed = AESGCM(_key(passphrase, salt, **_SCRYPT)).encrypt(nonce, data, None)
    return b"\n".join((ENC_MAGIC, json.dumps(header).encode(), sealed))


def decrypt(data: bytes, passphrase: str) -> bytes:
    try:
        magic, header_line, sealed = data.split(b"\n", 2)
        header = json.loads(header_line)
        params = {k: header[k] for k in _SCRYPT}
        salt, nonce = base64.b64decode(header["salt"]), base64.b64decode(header["nonce"])
    except (ValueError, KeyError, TypeError, RecursionError):
        raise InvalidDocument(["not a Daimon encrypted bundle"]) from None
    if magic != ENC_MAGIC or header.get("kdf") != "scrypt":
        raise InvalidDocument(["not a Daimon encrypted bundle"])
    if params != _SCRYPT:  # only what `encrypt` writes: costlier settings are a cheap attack
        raise InvalidDocument(["encryption parameters are not the ones Daimon writes"])
    try:
        return AESGCM(_key(passphrase, salt, **_SCRYPT)).decrypt(nonce, sealed, None)
    except (InvalidTag, ValueError):
        raise InvalidDocument(["wrong passphrase or damaged bundle"]) from None


# ---------- signatures ----------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def key_id(public_key: Ed25519PublicKey) -> str:
    """A short, stable id for a signing key: the first 16 hex of its sha256."""
    return sha256_hex(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))[:16]


def private_key(encoded: str) -> Ed25519PrivateKey:
    """An Ed25519 key from its base64 raw 32 bytes, as `DAIMON_EVALS__SIGNING_KEY` holds it."""
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded))


def public_key(encoded: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded))


def _unsigned(manifest: JsonObject) -> bytes:
    return canonical_json({k: v for k, v in manifest.items() if k != "signature"})


def sign(manifest: JsonObject, key: Ed25519PrivateKey) -> JsonObject:
    value = _b64(key.sign(_unsigned(manifest)))
    signature = Signature(key_id=key_id(key.public_key()), value=value)
    signed: JsonObject = {**manifest, "signature": to_document(signature)}
    return signed


SignatureState = Literal["unsigned", "unknown_key", "valid"]


def verify(manifest: JsonObject, trusted: Iterable[Ed25519PublicKey]) -> SignatureState:
    """Whether a known key signed the manifest. Raises `InvalidDocument` on a bad signature."""
    if manifest.get("signature") is None:
        return "unsigned"
    signature = Signature.model_validate(manifest["signature"])
    keys = {key_id(k): k for k in trusted}
    key = keys.get(signature.key_id)
    if key is None:
        return "unknown_key"
    try:
        key.verify(base64.b64decode(signature.value), _unsigned(manifest))
    except InvalidSignature:
        raise InvalidDocument(["the bundle signature does not match its content"]) from None
    return "valid"


# ---------- writing ----------


def scrub_refusal(manifest: Manifest, *, accept_unscrubbed: bool) -> str | None:
    """Why an import must stop on scrub status, or None. Only owners and admins may accept."""
    if manifest.scrub.status == "scrubbed" or accept_unscrubbed:
        return None
    return f"the bundle is {manifest.scrub.status}: an owner or an admin must accept it"


def dumps(document: JsonObject) -> bytes:
    return json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"


def dumps_lines(documents: Iterable[JsonObject]) -> bytes:
    return b"".join(json.dumps(d, ensure_ascii=False).encode() + b"\n" for d in documents)


@dataclass(frozen=True)
class BundleHeader:
    """The manifest fields a writer chooses; files, blobs and contents are computed."""

    id: str
    created_at: datetime
    source: SourceInfo
    scrub_status: Literal["scrubbed", "partial", "unscrubbed"]
    title: str | None = None
    created_by: PrincipalRef | None = None
    includes_identities: bool = False


def build_files(
    header: BundleHeader,
    suites: Iterable[LoadedSuite],
    runs: Iterable[LoadedRun],
    blobs: Mapping[BlobEntry, bytes],
    *,
    signing_key: Ed25519PrivateKey | None = None,
) -> dict[str, bytes]:
    """Every file of a bundle by path, with the manifest hashing all of them."""
    files = document_files(header, suites, runs, blobs, signing_key=signing_key)
    files.update({blob_path(b.sha256): blobs[b] for b in blobs})
    return files


def document_files(
    header: BundleHeader,
    suites: Iterable[LoadedSuite],
    runs: Iterable[LoadedRun],
    blobs: Iterable[BlobEntry],
    *,
    signing_key: Ed25519PrivateKey | None = None,
) -> dict[str, bytes]:
    """The manifest and document files of a bundle by path, without the blob files."""
    files: dict[str, bytes] = {}
    suite_entries: list[SuiteEntry] = []
    for suite in suites:
        sid = str(suite.document["id"])
        path, cases_path = f"suites/{sid}/suite.json", f"suites/{sid}/cases.jsonl"
        files[path], files[cases_path] = dumps(suite.document), dumps_lines(suite.cases)
        version = suite.document["version"]
        assert isinstance(version, int)
        suite_entries.append(
            SuiteEntry(
                id=sid,
                version=version,
                path=path,
                cases_path=cases_path,
                case_count=len(suite.cases),
            )
        )
    run_entries: list[RunEntry] = []
    for run in runs:
        rid, base = str(run.document["id"]), f"runs/{run.document['id']}/"
        files[f"{base}run.json"], files[f"{base}items.jsonl"] = (
            dumps(run.document),
            dumps_lines(run.items),
        )
        scores = f"{base}scores.jsonl" if run.scores else None
        reviews = f"{base}reviews.jsonl" if run.reviews else None
        if scores:
            files[scores] = dumps_lines(run.scores)
        if reviews:
            files[reviews] = dumps_lines(run.reviews)
        run_entries.append(
            RunEntry(
                id=rid,
                path=f"{base}run.json",
                items_path=f"{base}items.jsonl",
                scores_path=scores,
                reviews_path=reviews,
                item_count=len(run.items),
            )
        )
    entries = sorted(blobs, key=lambda b: b.sha256)
    manifest = Manifest(
        id=header.id,
        title=header.title,
        created_at=header.created_at,
        created_by=header.created_by,
        source=header.source,
        contents=BundleContents(suites=suite_entries, runs=run_entries),
        files=[
            FileEntry(path=p, sha256=sha256_hex(d), size=len(d)) for p, d in sorted(files.items())
        ],
        blobs=entries,
        scrub=BundleScrub(
            status=header.scrub_status, unchecked_blobs=sum(b.needs_human_check for b in entries)
        ),
        includes_identities=header.includes_identities,
    )
    document = to_document(manifest)
    if signing_key is not None:
        document = sign(document, signing_key)
    files[MANIFEST_PATH] = dumps(document)
    return files


def zip_add(archive: zipfile.ZipFile, path: str, data: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    archive.writestr(info, data, zipfile.ZIP_DEFLATED)


def to_zip(files: Mapping[str, bytes]) -> bytes:
    """A deterministic deflate zip of `files`, manifest first."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files, key=lambda p: (p != MANIFEST_PATH, p)):
            zip_add(archive, path, files[path])
    return out.getvalue()


def write_dir(files: Mapping[str, bytes], root: Path) -> None:
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
