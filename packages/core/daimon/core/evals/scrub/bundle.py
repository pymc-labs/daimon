"""Scrub a whole bundle, or one case, and sweep what is left.

`scrub_bundle` reads a valid bundle, rebuilds its blobs, scrubs every document, recomputes case
hashes and pins and returns the new files with a report of counts. Nothing real is returned,
logged or kept: the report names kinds, places and numbers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from daimon.core.evals.blobs import BLOB_DIR, ref_nodes
from daimon.core.evals.bundle import BundleHeader, build_files
from daimon.core.evals.format import (
    BlobEntry,
    JsonObject,
    ScrubStatus,
    SourceInfo,
    case_hash,
    sha256_hex,
)
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.fakes import keyed_hash
from daimon.core.evals.scrub.files import rebuild
from daimon.core.evals.scrub.sweep import Leftover, sweep_blob, sweep_document
from daimon.core.evals.validate import (
    BundleSource,
    InvalidDocument,
    LoadedRun,
    LoadedSuite,
    validate_bundle,
)
from pydantic import JsonValue

POLICY: Final = "default/1"


@dataclass
class ScrubReport:
    """What a scrub did, as numbers. `leftovers` are places a person must look at."""

    status: ScrubStatus
    replaced: dict[str, int] = field(default_factory=dict[str, int])
    blobs: dict[str, int] = field(default_factory=dict[str, int])
    leftovers: list[Leftover] = field(default_factory=list[Leftover])
    names_checked: bool = True
    cases: int = 0


@dataclass(frozen=True)
class ScrubbedBlob:
    data: bytes
    media_type: str
    scrub: str
    needs_human_check: bool
    reason: str | None
    """The `review_flags` reason cases using this blob get, or None."""


def scrub_blob(scrubber: Scrubber, data: bytes, media_type: str, name: str | None) -> ScrubbedBlob:
    done = rebuild(data, media_type, name, scrubber)
    if done is None:
        return ScrubbedBlob(data, media_type, "unscrubbed", True, "needs_human_check")
    reason = None
    if done.needs_human_check:
        if done.media_type.startswith("image/"):
            reason = "image_redacted"
        else:
            reason = "file_rebuilt" if done.scrub == "rebuilt" else "needs_human_check"
    kind = "clean" if done.data == data and done.scrub == "rebuilt" else done.scrub
    return ScrubbedBlob(done.data, done.media_type, kind, done.needs_human_check, reason)


def _obj(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _pin_key(pin: JsonObject) -> tuple[str, int, str]:
    return (str(pin.get("id")), int(str(pin.get("revision"))), str(pin.get("hash")))


def _flag(case: JsonObject, reason: str, detail: str) -> None:
    flags = case.get("review_flags")
    flags = flags if isinstance(flags, list) else []
    if not any(isinstance(f, dict) and f.get("reason") == reason for f in flags):
        flags.append({"reason": reason, "detail": detail})
    case["review_flags"] = flags


def scrub_case(
    scrubber: Scrubber, case: JsonObject, *, blob_reasons: Mapping[str, str] | None = None
) -> tuple[JsonObject, list[Leftover]]:
    """`case` with fakes, a scrub record, review flags and a fresh hash; plus its leftovers.

    Blobs the case references must already be scrubbed; `blob_reasons` (sha256 to flag reason)
    puts their checks on the case. Raw text is never kept: `source_ref` and `source_thread` become
    keyed hashes.
    """
    before = sum(scrubber.counts.values())
    keyed = {
        k: f"kh:{keyed_hash(scrubber.key, k, str(v))[:24]}"
        for k in ("source_thread",)
        if (v := case.get(k)) is not None
    }
    prov = case.get("provenance")
    ref = prov.get("source_ref") if isinstance(prov, dict) else None
    out = scrubber.value(case)
    assert isinstance(out, dict)
    out.update(keyed)
    provenance = out.get("provenance")
    if isinstance(provenance, dict) and ref is not None:
        provenance["source_ref"] = f"kh:{keyed_hash(scrubber.key, 'source_ref', str(ref))[:24]}"
    where = f"case {out.get('id')}"
    leftovers = sweep_document(scrubber, out, where)
    reasons = Counter(
        r
        for sha in {str(n["sha256"]) for n in ref_nodes(out)}
        if (r := (blob_reasons or {}).get(str(sha)))
    )
    for reason in reasons:
        _flag(out, reason, f"{reasons[reason]} file(s) need a person's check")
    clean = not leftovers and scrubber.names_checked
    if not clean:
        _flag(out, "scrub_uncertain", "Some private text may remain; check before sharing.")
    out["provenance"] = {
        **(provenance if isinstance(provenance, dict) else {"kind": "recorded"}),
        "scrub": {
            "status": "scrubbed" if clean else "partial",
            "policy": POLICY,
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entities": sum(scrubber.counts.values()) - before,
            "human_checked": False,
        },
    }
    out["hash"] = case_hash(out)
    return out, leftovers


@dataclass(frozen=True)
class _NewBlob:
    sha256: str
    size: int
    media_type: str
    state: str


def _retarget(node: JsonValue, mapping: Mapping[str, _NewBlob]) -> None:
    """Point blob references (and the attachment or file around them) at the scrubbed blobs."""
    if isinstance(node, list):
        for item in node:
            _retarget(item, mapping)
    elif isinstance(node, dict):
        blob = node.get("blob")
        new = (
            mapping.get(str(blob["sha256"]))
            if isinstance(blob, dict) and "sha256" in blob
            else None
        )
        if new and ({"id", "name", "media_type"} <= node.keys() or "path" in node):
            if new.state in ("rebuilt", "redacted"):
                node["state"] = new.state
            for key, value in (("media_type", new.media_type), ("size", new.size)):
                if key in node:
                    node[key] = value
        for value in node.values():
            _retarget(value, mapping)
    for ref in ref_nodes(node):
        if (new := mapping.get(str(ref["sha256"]))) is not None:
            ref["sha256"], ref["size"], ref["media_type"] = new.sha256, new.size, new.media_type


def _ref_names(documents: list[JsonObject]) -> dict[str, str]:
    return {
        str(r["sha256"]): str(r["name"]) for d in documents for r in ref_nodes(d) if r.get("name")
    }


def scrub_bundle(source: BundleSource, scrubber: Scrubber) -> tuple[dict[str, bytes], ScrubReport]:
    """The files of the scrubbed bundle, and what was done. Raises on a bundle that is invalid."""
    loaded = validate_bundle(source)
    loaded.raise_for_problems()
    manifest = loaded.manifest
    if manifest is None:
        raise InvalidDocument(["bundle.json is missing"])
    start = Counter(scrubber.counts)
    documents = [
        *(s.document for s in loaded.suites),
        *(c for s in loaded.suites for c in s.cases),
        *(d for r in loaded.runs for d in (r.document, *r.items, *r.scores, *r.reviews)),
    ]
    names = _ref_names(documents)
    report = ScrubReport(status="scrubbed", names_checked=scrubber.names_checked)
    mapping: dict[str, _NewBlob] = {}
    reasons: dict[str, str] = {}
    blobs_out: dict[BlobEntry, bytes] = {}
    for entry in manifest.blobs:
        data = source.read(f"{BLOB_DIR}{entry.sha256}")
        done = scrub_blob(scrubber, data, entry.media_type, names.get(entry.sha256))
        sha = sha256_hex(done.data)
        mapping[entry.sha256] = _NewBlob(sha, len(done.data), done.media_type, done.scrub)
        if done.reason:
            reasons[sha] = done.reason
        blobs_out[
            BlobEntry(
                sha256=sha, size=len(done.data), media_type=done.media_type,
                scrub=done.scrub,  # pyright: ignore[reportArgumentType]
                needs_human_check=done.needs_human_check,
            )
        ] = done.data  # fmt: skip
        report.blobs[done.scrub] = report.blobs.get(done.scrub, 0) + 1
        report.blobs["needs_human_check"] = (
            report.blobs.get("needs_human_check", 0) + done.needs_human_check
        )
        report.leftovers += (
            [Leftover(f"blob {sha[:12]}", "unsupported_blob", 1)]
            if done.scrub == "unscrubbed"
            else sweep_blob(
                scrubber, done.data, done.media_type, names.get(entry.sha256), f"blob {sha[:12]}"
            )
        )
    for doc in documents:
        _retarget(doc, mapping)
    new_hash: dict[tuple[str, int, str], str] = {}
    suites: list[LoadedSuite] = []
    partial: bool = bool(report.leftovers) or not scrubber.names_checked
    for suite in loaded.suites:
        cases: list[JsonObject] = []
        for case in suite.cases:
            old = (str(case["id"]), int(str(case["revision"])), str(case.get("hash")))
            fresh, left = scrub_case(scrubber, case, blob_reasons=reasons)
            new_hash[old] = str(fresh["hash"])
            report.leftovers += left
            status = _obj(_obj(fresh.get("provenance")).get("scrub")).get("status")
            partial = partial or bool(left) or status != "scrubbed"
            cases.append(fresh)
        doc = scrubber.value(suite.document)
        assert isinstance(doc, dict)
        report.leftovers += sweep_document(scrubber, doc, f"suite {doc.get('id')}")
        pins = doc.get("cases")
        for pin in pins if isinstance(pins, list) else []:
            if isinstance(pin, dict):
                pin["hash"] = new_hash.get(_pin_key(pin), pin.get("hash"))
        suites.append(LoadedSuite(doc, cases))
        report.cases += len(cases)
    runs: list[LoadedRun] = []
    for run in loaded.runs:
        rid = run.document.get("id")
        scrubbed = [
            [scrubber.value(d) for d in group] for group in (run.items, run.scores, run.reviews)
        ]
        items = [d for d in scrubbed[0] if isinstance(d, dict)]
        for item in items:
            ref = item.get("case")
            if isinstance(ref, dict):
                ref["hash"] = new_hash.get(_pin_key(ref), ref.get("hash"))
        doc = scrubber.value(run.document)
        assert isinstance(doc, dict)
        for group, label in zip(
            [[doc], *scrubbed], ["run", "item", "score", "review"], strict=False
        ):
            for n, d in enumerate(group):
                report.leftovers += sweep_document(scrubber, d, f"{label} {rid}#{n}")
        runs.append(
            LoadedRun(
                doc, items,
                [d for d in scrubbed[1] if isinstance(d, dict)],
                [d for d in scrubbed[2] if isinstance(d, dict)],
            )
        )  # fmt: skip
    partial = partial or bool(report.leftovers)
    report.status = "partial" if partial else "scrubbed"
    report.replaced = dict(scrubber.counts - start)
    header = BundleHeader(
        id=manifest.id,
        created_at=datetime.now(UTC),
        source=SourceInfo.model_validate(manifest.source.model_dump(exclude_none=True)),
        scrub_status=report.status,
        title=manifest.title,
        created_by=manifest.created_by,
        includes_identities=manifest.includes_identities,
    )
    return build_files(header, suites, runs, blobs_out), report
