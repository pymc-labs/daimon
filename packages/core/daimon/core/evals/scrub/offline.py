"""Scrub or sweep a bundle on disk with no database: what `daimon evals bundle scrub` runs.

The dictionary file, the key, a salt (the tenant or project the data belongs to) and a map file
(keyed hashes to fakes, no real values) are the only inputs. Reuse the same key, salt and map for
every bundle of a suite so fakes stay the same; a map made with another key or salt is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

from daimon.core.evals.blobs import BLOB_DIR, ref_nodes
from daimon.core.evals.bundle import (
    ENC_MAGIC,
    encrypt,
    open_path,
    to_zip,
    write_dir,
)
from daimon.core.evals.format import JsonObject
from daimon.core.evals.scrub.bundle import ScrubReport, pseudonymous, scrub_bundle
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import load_entities
from daimon.core.evals.scrub.fakes import key_check, scrub_key
from daimon.core.evals.scrub.sweep import Leftover, sweep_blob, sweep_document
from daimon.core.evals.validate import InvalidDocument, validate_bundle

MIN_KEY: Final = 16


def _read_map(path: Path, check: str) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("check") != check:
        raise InvalidDocument(["the map was made with another key or salt"])
    return {(r["kind"], r["hash"]): r["fake"] for r in data["entries"]}


def load_scrubber(
    entities: Path, key: str, *, salt: str, map_path: Path | None = None, ner_model: str | None
) -> Scrubber:
    if len(key) < MIN_KEY:
        raise InvalidDocument([f"the scrub key must be at least {MIN_KEY} characters"])
    if not salt.strip():
        raise InvalidDocument(["the salt must not be empty"])
    check = key_check(scrub_key(key.encode(), salt))
    known = _read_map(map_path, check) if map_path is not None else {}
    return Scrubber(
        load_entities(entities), key.encode(), salt=salt, ner_model=ner_model, known=known
    )


def save_map(scrubber: Scrubber, path: Path) -> None:
    """Write the map, past entries and new, owner-readable only: it is private data."""
    rows = _read_map(path, scrubber.key_check)
    rows.update(scrubber.fakes.added)
    entries = [{"kind": k, "hash": h, "fake": f} for (k, h), f in sorted(rows.items())]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"check": scrubber.key_check, "entries": entries}, out, indent=1)


def _write(files: dict[str, bytes], target: Path, *, as_zip: bool, passphrase: str | None) -> None:
    """Write `files` as a folder, a zip, or an encrypted zip (replacing what is there)."""
    if as_zip:
        data = to_zip(files)
        target.write_bytes(encrypt(data, passphrase) if passphrase else data)
        return
    scratch = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
    write_dir(files, scratch)
    if target.exists():
        shutil.rmtree(target)
    scratch.rename(target)


def scrub_path(
    path: Path, out: Path | None, scrubber: Scrubber, *, passphrase: str | None = None
) -> ScrubReport:
    """Scrub the bundle at `path` into `out`, or in place. Nothing is written if it is invalid."""
    encrypted = path.is_file() and path.read_bytes().startswith(ENC_MAGIC)
    files, report = scrub_bundle(open_path(path, passphrase=passphrase), scrubber)
    target = out or path
    _write(files, target, as_zip=path.is_file(), passphrase=passphrase if encrypted else None)
    return report


def leftovers_of(
    path: Path, scrubber: Scrubber, *, passphrase: str | None = None
) -> list[Leftover]:
    """What a scrub would still change in the bundle, plus blobs a person must look at."""
    source = open_path(path, passphrase=passphrase)
    report = validate_bundle(source)
    report.raise_for_problems()
    manifest = report.manifest
    assert manifest is not None
    found: list[Leftover] = []
    documents: list[tuple[str, JsonObject]] = [
        *((f"suite {s.document.get('id')}", s.document) for s in report.suites),
        *((f"case {c.get('id')}", c) for s in report.suites for c in s.cases),
    ]
    for run in report.runs:
        rid = run.document.get("id")
        documents.append((f"run {rid}", run.document))
        documents += [
            (f"{kind} {rid}#{n}", d)
            for kind, group in (("item", run.items), ("score", run.scores), ("review", run.reviews))
            for n, d in enumerate(group)
        ]
    names = {
        str(r["sha256"]): str(r["name"])
        for _, d in documents
        for r in ref_nodes(d)
        if r.get("name")
    }
    found += sweep_document(
        scrubber,
        {
            "title": manifest.title,
            "label": manifest.source.label,
            "instance": manifest.source.instance,
        },
        "bundle",
    )
    if not pseudonymous(manifest.created_by):
        found.append(Leftover("bundle", "person", 1))
    for where, document in documents:
        found += sweep_document(scrubber, document, where)
    for blob in manifest.blobs:
        where = f"blob {blob.sha256[:12]}"
        if blob.scrub == "unscrubbed" or (
            blob.media_type.startswith("image/") and blob.scrub != "redacted"
        ):
            found.append(Leftover(where, "unscrubbed_blob", 1))
        elif blob.needs_human_check:
            found.append(Leftover(where, "needs_human_check", 1))
        found += sweep_blob(
            scrubber,
            source.read(f"{BLOB_DIR}{blob.sha256}"),
            blob.media_type,
            names.get(blob.sha256),
            where,
        )
    if not scrubber.names_checked:
        found.append(Leftover("bundle", "names_unchecked", 1))
    return found
