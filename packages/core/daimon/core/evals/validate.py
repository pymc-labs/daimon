"""Validation of eval documents and bundles: the JSON schema plus the checks it cannot express.

Semantic checks: file and blob hashes against the manifest, case hashes against suite pins,
references between documents, exactly one requester per case, unique ids and scrub status.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Final, Literal, Protocol

from daimon.core.evals.blobs import BLOB_DIR, SHA256, blob_refs
from daimon.core.evals.effects import is_known_verb
from daimon.core.evals.format import (
    FORMAT_VERSION,
    Case,
    Comment,
    JsonObject,
    Manifest,
    ReviewQueue,
    Run,
    RunItem,
    Score,
    Suite,
    case_hash,
    sha256_hex,
)
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import JsonValue

DocType = Literal["Manifest", "Suite", "Case", "Run", "RunItem", "Score", "ReviewRecord"]
MAX_LINE_BYTES: Final = 8 * 1024 * 1024
MANIFEST_PATH: Final = "bundle.json"


class InvalidDocument(ValueError):
    """A document or bundle that fails validation; `problems` lists every reason."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems[:5]))
        self.problems = tuple(problems)


class BundleSource(Protocol):
    """The files of a bundle, from a folder or a zip."""

    def names(self) -> frozenset[str]: ...
    def read(self, path: str) -> bytes: ...


@cache
def root_schema() -> JsonObject:
    text = resources.files("daimon.core.evals").joinpath("schema/bundle-1.0.schema.json")
    return json.loads(text.read_text())


@cache
def _validator(kind: DocType) -> Draft202012Validator:
    root = root_schema()
    schema = {k: v for k, v in root.items() if k != "oneOf"} | {"$ref": f"#/$defs/{kind}"}
    return Draft202012Validator(schema, format_checker=FormatChecker())


def schema_problems(kind: DocType, document: JsonValue, where: str = "") -> list[str]:
    """Every place `document` breaks the schema for `kind`."""
    prefix = f"{where}: " if where else ""
    return [f"{prefix}{e.json_path}: {e.message}" for e in _validator(kind).iter_errors(document)]


def case_problems(
    document: JsonObject, *, where: str = "case", need_hash: bool = True
) -> list[str]:
    """Schema and semantic problems of one case."""
    problems = schema_problems("Case", document, where)
    if problems:
        return problems
    case = Case.model_validate(document)
    if case.hash is None:
        if need_hash:
            problems.append(f"{where}: has no hash")
    elif case.hash != case_hash(document):
        problems.append(f"{where}: hash does not match its content")
    people = case.surface.participants
    requesters = [p.id for p in people if p.is_requester]
    if len(requesters) != 1:
        problems.append(f"{where}: needs exactly one requester, has {len(requesters)}")
    problems += _duplicates(where, "participant", [p.id for p in people])
    messages = [*case.history, *case.turn.messages]
    problems += _duplicates(where, "message", [m.id for m in messages])
    authors = {p.id for p in people} | {case.surface.bot.id}
    problems += [
        f"{where}: message {m.id} is by {m.author}, who is not a participant"
        for m in messages
        if m.author not in authors
    ]
    return problems


def case_warnings(case: Case, where: str) -> list[str]:
    verbs = [e.verb for x in case.world.exchanges or [] for e in x.effects or []]
    verbs += [e.verb for e in case.expect.effects.items] if case.expect.effects else []
    verbs += case.expect.forbidden_effects or []
    return [f"{where}: unknown effect verb {v}" for v in sorted(set(verbs)) if not is_known_verb(v)]


def _duplicates(where: str, kind: str, ids: Iterable[str]) -> list[str]:
    return [f"{where}: {kind} id {i} is used {n} times" for i, n in Counter(ids).items() if n > 1]


def read_jsonl(data: bytes, where: str) -> list[JsonObject]:
    """The objects of a JSONL file; raises `InvalidDocument` on a bad line."""
    if data and not data.endswith(b"\n"):
        data += b"\n"
    lines: list[JsonObject] = []
    for number, line in enumerate(data.split(b"\n")[:-1], start=1):
        if len(line) > MAX_LINE_BYTES:
            raise InvalidDocument([f"{where}:{number}: line is over 8 MiB"])
        if not line.strip() or line.endswith(b"\r"):
            raise InvalidDocument([f"{where}:{number}: blank line or CRLF"])
        value: JsonValue = json.loads(line)
        if not isinstance(value, dict):
            raise InvalidDocument([f"{where}:{number}: not a JSON object"])
        lines.append(value)
    return lines


@dataclass
class LoadedSuite:
    document: JsonObject
    cases: list[JsonObject]


@dataclass
class LoadedRun:
    document: JsonObject
    items: list[JsonObject]
    scores: list[JsonObject]
    reviews: list[JsonObject]


@dataclass
class ValidationReport:
    """What `validate_bundle` found; `ok` when there are no problems. Warnings never refuse."""

    manifest: Manifest | None = None
    suites: list[LoadedSuite] = field(default_factory=list[LoadedSuite])
    runs: list[LoadedRun] = field(default_factory=list[LoadedRun])
    problems: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_for_problems(self) -> None:
        if self.problems:
            raise InvalidDocument(self.problems)


def _json(source: BundleSource, path: str) -> JsonObject:
    value: JsonValue = json.loads(source.read(path))
    if not isinstance(value, dict):
        raise InvalidDocument([f"{path}: not a JSON object"])
    return value


def validate_bundle(source: BundleSource) -> ValidationReport:
    """Validate a whole bundle. Never raises for bad content: problems land in the report."""
    report = ValidationReport()
    try:
        _check(source, report)
    except InvalidDocument as refused:
        report.problems += refused.problems
    except (ValueError, KeyError, OSError) as broken:  # bad JSON or a missing listed file
        report.problems.append(f"unreadable: {type(broken).__name__}")
    except RecursionError:
        report.problems.append("unreadable: JSON nested too deep")
    return report


def _newer(raw_manifest: JsonObject) -> str | None:
    found = raw_manifest.get("format_version")
    try:
        major, minor = (int(p) for p in str(found).split("."))
    except ValueError:
        return None
    ours = tuple(int(p) for p in FORMAT_VERSION.split("."))
    if (major, minor) <= ours:
        return None
    return f"{MANIFEST_PATH}: format {found} is newer than this Daimon reads ({FORMAT_VERSION})"


def _layout_problems(manifest: Manifest) -> list[str]:
    """Each suite and run at its own canonical paths, each listed in `files`, ids unique."""
    listed = {f.path for f in manifest.files}
    problems = _duplicates(MANIFEST_PATH, "suite", [s.id for s in manifest.contents.suites])
    problems += _duplicates(MANIFEST_PATH, "run", [r.id for r in manifest.contents.runs])
    problems += _duplicates(MANIFEST_PATH, "file", [f.path for f in manifest.files])
    problems += _duplicates(MANIFEST_PATH, "blob", [b.sha256 for b in manifest.blobs])
    paths: list[tuple[str | None, str]] = []
    for suite in manifest.contents.suites:
        base = f"suites/{suite.id}/"
        paths += [(suite.path, f"{base}suite.json"), (suite.cases_path, f"{base}cases.jsonl")]
    for run in manifest.contents.runs:
        base = f"runs/{run.id}/"
        paths += [(run.path, f"{base}run.json"), (run.items_path, f"{base}items.jsonl")]
        optional = ((run.scores_path, "scores.jsonl"), (run.reviews_path, "reviews.jsonl"))
        paths += [(p, f"{base}{n}") for p, n in optional if p]
    for path, want in paths:
        if path != want:
            problems.append(f"{MANIFEST_PATH}: {path} should be {want}")
        elif path not in listed:
            problems.append(f"{MANIFEST_PATH}: {path} is not in files")
    return problems


def _check(source: BundleSource, report: ValidationReport) -> None:
    names = {n for n in source.names() if not n.endswith("/")}
    if MANIFEST_PATH not in names:
        raise InvalidDocument([f"{MANIFEST_PATH} is missing"])
    raw_manifest = _json(source, MANIFEST_PATH)
    if newer := _newer(raw_manifest):
        raise InvalidDocument([newer])
    if problems := schema_problems("Manifest", raw_manifest, MANIFEST_PATH):
        raise InvalidDocument(problems)
    manifest = report.manifest = Manifest.model_validate(raw_manifest)
    if problems := _layout_problems(manifest):
        raise InvalidDocument(problems)
    _check_files(source, names, manifest, report)
    if report.problems:
        return  # documents are only trusted once every file matches the manifest
    listed_blobs = {b.sha256 for b in manifest.blobs}
    scrubbed = manifest.scrub.status == "scrubbed"
    suites: dict[str, Suite] = {}
    for entry in manifest.contents.suites:
        loaded = _check_suite(source, entry.path, entry.cases_path, report, scrubbed=scrubbed)
        if loaded is None:
            continue
        suite = Suite.model_validate(loaded.document)
        if (suite.id, suite.version) != (entry.id, entry.version) or len(
            loaded.cases
        ) != entry.case_count:
            report.problems.append(f"{entry.path}: does not match its manifest entry")
        suites[suite.id] = suite
        report.suites.append(loaded)
    for entry in manifest.contents.runs:
        loaded_run = _check_run(
            source,
            entry.path,
            entry.items_path,
            entry.scores_path,
            entry.reviews_path,
            suites,
            report,
        )
        if loaded_run is not None:
            if (
                loaded_run.document.get("id") != entry.id
                or len(loaded_run.items) != entry.item_count
            ):
                report.problems.append(f"{entry.path}: does not match its manifest entry")
            report.runs.append(loaded_run)
    documents: list[JsonValue] = [s.document for s in report.suites]
    documents += [c for s in report.suites for c in s.cases]
    documents += [d for r in report.runs for d in (r.document, *r.items, *r.scores, *r.reviews)]
    report.problems += [
        f"a document references blob {sha}, which the bundle lacks"
        for sha in sorted(blob_refs(documents) - listed_blobs)
    ]
    _check_scrub(manifest, report)


def _check_files(
    source: BundleSource, names: set[str], manifest: Manifest, report: ValidationReport
) -> None:
    listed = {f.path: f for f in manifest.files}
    for path, entry in listed.items():
        if path not in names:
            report.problems.append(f"{path}: listed but missing")
            continue
        data = source.read(path)
        if sha256_hex(data) != entry.sha256 or len(data) != entry.size:
            report.problems.append(f"{path}: hash or size does not match the manifest")
    blobs = {b.sha256: b for b in manifest.blobs}
    for sha, blob in blobs.items():
        path = f"{BLOB_DIR}{sha}"
        if path not in names:
            report.problems.append(f"blob {sha}: listed but missing")
            continue
        data = source.read(path)
        if sha256_hex(data) != sha or len(data) != blob.size:
            report.problems.append(f"blob {sha}: content does not match its name or size")
    for path in sorted(names - listed.keys() - {MANIFEST_PATH}):
        sha = path.removeprefix(BLOB_DIR)
        if not (path.startswith(BLOB_DIR) and SHA256.match(sha) and sha in blobs):
            report.problems.append(f"{path}: not listed in the manifest")


def _check_suite(
    source: BundleSource, path: str, cases_path: str, report: ValidationReport, *, scrubbed: bool
) -> LoadedSuite | None:
    document = _json(source, path)
    if problems := schema_problems("Suite", document, path):
        report.problems += problems
        return None
    suite = Suite.model_validate(document)
    cases = read_jsonl(source.read(cases_path), cases_path)
    by_id: dict[str, Case] = {}
    for number, raw in enumerate(cases, start=1):
        where = f"{cases_path}:{number}"
        if problems := case_problems(raw, where=where):
            report.problems += problems
            continue
        case = Case.model_validate(raw)
        by_id[case.id] = case
        report.warnings += case_warnings(case, where)
        status = case.provenance.scrub.status if case.provenance.scrub else None
        if scrubbed and status != "scrubbed":
            report.problems.append(
                f"{where}: case is {status or 'unscrubbed'} but the bundle says scrubbed"
            )
    report.problems += _duplicates(cases_path, "case", [str(c.get("id")) for c in cases])
    pins = {(p.id, p.revision, p.hash) for p in suite.cases}
    have = {(c.id, c.revision, c.hash or "") for c in by_id.values()}
    report.problems += [
        f"{path}: pins case {i} revision {r}, not in {cases_path} with that hash"
        for i, r, _ in sorted(pins - have)
    ]
    report.problems += [
        f"{cases_path}: case {i} is not pinned by the suite" for i, _, _ in sorted(have - pins)
    ]
    return LoadedSuite(document, cases)


def _check_run(
    source: BundleSource,
    path: str,
    items_path: str,
    scores_path: str | None,
    reviews_path: str | None,
    suites: dict[str, Suite],
    report: ValidationReport,
) -> LoadedRun | None:
    document = _json(source, path)
    if problems := schema_problems("Run", document, path):
        report.problems += problems
        return None
    run = Run.model_validate(document)
    loaded = LoadedRun(
        document,
        read_jsonl(source.read(items_path), items_path),
        read_jsonl(source.read(scores_path), scores_path) if scores_path else [],
        read_jsonl(source.read(reviews_path), reviews_path) if reviews_path else [],
    )
    suite = suites.get(run.suite.id)
    if suite is None:
        report.warnings.append(
            f"{path}: needs suite {run.suite.id} version {run.suite.version} where it is imported"
        )
    elif suite.version != run.suite.version:
        report.problems.append(
            f"{path}: ran suite version {run.suite.version}, the bundle has {suite.version}"
        )
    pins = {(p.id, p.revision, p.hash) for p in suite.cases} if suite else None
    arms = {a.id for a in run.arms}
    report.problems += _duplicates(path, "arm", [a.id for a in run.arms])
    items: list[RunItem] = []
    for number, raw in enumerate(loaded.items, start=1):
        where = f"{items_path}:{number}"
        if problems := schema_problems("RunItem", raw, where):
            report.problems += problems
            continue
        item = RunItem.model_validate(raw)
        items.append(item)
        if item.run_id != run.id:
            report.problems.append(f"{where}: belongs to run {item.run_id}, not {run.id}")
        if item.arm_id not in arms:
            report.problems.append(f"{where}: arm {item.arm_id} is not in the run")
        if pins is not None and (item.case.id, item.case.revision, item.case.hash) not in pins:
            report.problems.append(f"{where}: case {item.case.id} is not pinned by the suite")
    item_ids = {i.id for i in items}
    report.problems += _duplicates(items_path, "item", [i.id for i in items])
    report.problems += _duplicates(
        str(scores_path), "score", [str(s.get("id")) for s in loaded.scores]
    )
    report.problems += _duplicates(
        str(reviews_path), "review", [str(r.get("id")) for r in loaded.reviews]
    )
    for number, raw in enumerate(loaded.scores, start=1):
        where = f"{scores_path}:{number}"
        if problems := schema_problems("Score", raw, where):
            report.problems += problems
            continue
        score = Score.model_validate(raw)
        refs = [score.item_id] if score.item_id else []
        refs += [score.pair.a, score.pair.b] if score.pair else []
        report.problems += [
            f"{where}: item {i} is not in the run" for i in refs if i not in item_ids
        ]
    for number, raw in enumerate(loaded.reviews, start=1):
        where = f"{reviews_path}:{number}"
        if problems := schema_problems("ReviewRecord", raw, where):
            report.problems += problems
            continue
        if raw.get("kind") == "queue":
            queue = ReviewQueue.model_validate(raw)
            report.problems += [
                f"{where}: arm {a} is not in the run" for a in queue.arms or [] if a not in arms
            ]
        else:
            comment = Comment.model_validate(raw)
            if comment.item_id and comment.item_id not in item_ids:
                report.problems.append(f"{where}: item {comment.item_id} is not in the run")
    return loaded


def _check_scrub(manifest: Manifest, report: ValidationReport) -> None:
    unscrubbed = [b.sha256 for b in manifest.blobs if b.scrub == "unscrubbed"]
    if manifest.scrub.status == "scrubbed" and unscrubbed:
        report.problems.append(
            f"{len(unscrubbed)} blobs are unscrubbed but the bundle says scrubbed"
        )
    unchecked = sum(b.needs_human_check for b in manifest.blobs)
    if unchecked:
        report.warnings.append(f"{unchecked} blobs need a human check")
