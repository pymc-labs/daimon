"""Leaks found in review: each probe must come out with nothing real left, or be marked partial."""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daimon.core.evals.bundle import BundleHeader, DirSource, build_files, write_dir
from daimon.core.evals.format import BlobEntry, SourceInfo, case_hash, sha256_hex
from daimon.core.evals.from_thread import ThreadRecord, build_cases
from daimon.core.evals.scrub import detect, offline
from daimon.core.evals.scrub.detect import Detector, Hit, Ner
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import KnownEntities, load_entities, terms_of
from daimon.core.evals.scrub.fakes import adapt
from daimon.core.evals.scrub.files import rebuild
from daimon.core.evals.scrub.offline import (
    leftovers_of,
    load_scrubber,
    save_learned,
    save_map,
    scrub_path,
)
from daimon.core.evals.validate import InvalidDocument, LoadedSuite, validate_bundle
from faker import Faker
from PIL import Image

KEY = b"k" * 32
EXAMPLE = Path(__file__).parent / "example_bundle"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
REAL = ("alice", "smith", "renée", "renee", "acme", "globex", "priya", "raman", "hunter2")
ENTITIES = KnownEntities.model_validate(
    {
        "people": [
            {
                "name": "Alice Smith",
                "emails": ["alice@acme-corp.com"],
                "ids": ["123456789012345678"],
            },
            {"name": "Renée Dubois"},
            {"name": "Priya Raman"},
        ],
        "companies": ["Acme Corp", "Globex Industries"],
    }
)


def _scrubber(entities: KnownEntities = ENTITIES, salt: str = "t1") -> Scrubber:
    return Scrubber(entities, KEY, salt=salt, ner_model=None)


def _real_in(text: str) -> list[str]:
    folded = text.casefold()
    return [r for r in REAL if r in folded]


@pytest.mark.parametrize(
    "text",
    [
        "file alice_smith_cv.docx",
        "AliceSmith and ALICESMITH",
        "line1\\nAlice Smith said hi",
        "Ren\\u00e9e Dubois",
        "Renée Dubois and Renee Dubois and Re​née Dubois",
        "https://x.example/?q=Alice%20Smith&e=alice%40acme-corp.com",
        "Acme said ok; ACME CORP; acme-corp; AcmeCorp",
        "DATABASE_URL=postgres://admin:hunter2hunter2@db.example.com:5432/prod",
        "redis://svc:hunter2hunter2@cache.example.com/0",
        "https://x.zoom.us/j/1234567?pwd=hunter2hunter2abcd",
        "aws_secret_access_key = hunter2hunter2/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "sk_live_51Hxhunter2hunter2 glpat-hunter2hunter2abcdef hf_hunter2hunter2hunter2hunter2abcdef",
        "https://docs.google.com/document/d/1hunter2hunter2hunter2hunter2hunter2abc/edit",
        "see https://acme.atlassian.net/browse/X-1",
        "call +1-415-555-0199 today",
    ],
)
def test_text_when_encoded_or_secret_then_nothing_real_left(text: str) -> None:
    out = _scrubber().text(text)
    assert _real_in(out) == [], out
    assert "415-555-0199" not in out, out


def test_common_names_when_many_fakes_exist_then_still_replaced() -> None:
    people = [f"Person{i} Surname{i}" for i in range(300)]
    common = ["John Smith", "Mary Wilson", "David Brown", "Linda Thomas"]
    scrubber = _scrubber(KnownEntities.model_validate({"people": people + common}))
    for person in people:
        scrubber.text(f"{person} wrote")
    for name in common:
        assert name not in scrubber.text(f"Yesterday {name} asked."), name


def test_value_when_keys_ids_and_nested_json_then_scrubbed_and_nothing_left() -> None:
    scrubber = _scrubber()
    doc = {
        "hash": "Alice Smith",
        "schema": "acme_corp_prod",
        "DB_PASSWORD": "hunter2hunter2",
        "privateKey": "abcdefghijklmnop",
        "AWS_SECRET_ACCESS_KEY": "abcdefghijklmnop",
        "owner": "moonshot-org",
        "repo": "moonshot-x",
        "html_url": "https://github.com/moonshot-org/moonshot-x",
        "user_id": 123456789012345678,
        "body": json.dumps({"author": "Renée Dubois", "note": "Hi,\nRenée here"}),
    }
    out = scrubber.value(doc)
    dumped = json.dumps(out, ensure_ascii=False)
    assert _real_in(dumped) == [] and "moonshot" not in dumped, dumped
    assert "abcdefghijklmnop" not in dumped and "123456789012345678" not in dumped, dumped
    assert scrubber.residue_of(out) == {}, "a second look finds nothing"


def test_overlap_when_known_first_name_inside_a_wider_name_then_whole_name_faked() -> None:
    scrubber = _scrubber(KnownEntities.model_validate({"people": ["Alice Jones", "Carol Smith"]}))
    out = scrubber.text("Yesterday Carol Smith and Alice Jones asked.")
    assert "Carol" not in out and "Smith" not in out and "Alice" not in out, out


def test_salt_when_different_then_different_fakes() -> None:
    assert _scrubber(salt="t1").text("Alice Smith") != _scrubber(salt="t2").text("Alice Smith")


def _docx() -> bytes:
    jpeg = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(jpeg, "JPEG")
    parts = {
        "[Content_Types].xml": "<Types/>",
        "word/_rels/document.xml.rels": '<Relationships><Relationship Id="r1" '
        'Target="media/alice_smith.jpeg"/><Relationship Id="r2" TargetMode="External" '
        'Target="file:///C:/Users/psmith/q3.docx"/></Relationships>',
        "word/document.xml": f"<w:document {W}><w:body><w:p><w:r><w:t>Alice Smith met Priya "
        'Raman.</w:t></w:r></w:p><w:p><w:del w:author="Priya Raman"><w:r><w:delText>Note '
        "from Globex Industries</w:delText></w:r></w:del></w:p></w:body></w:document>",
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc">'
        "<dc:creator>Priya Raman</dc:creator><cp:keywords>psmith</cp:keywords>"
        "<x>lead <b/>Alice Smith tail</x></cp:coreProperties>",
        "docProps/app.xml": "<Properties><Company>Globex Industries</Company></Properties>",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)
        z.writestr("word/media/alice_smith.jpeg", jpeg.getvalue())
        z.writestr("word/embeddings/oleObject1.bin", b"raw Alice Smith data")
    return out.getvalue()


def test_docx_when_metadata_deletions_and_embedded_parts_then_nothing_real_and_names_kept() -> None:
    built = rebuild(_docx(), DOCX, "a.docx", _scrubber())
    assert built is not None
    with zipfile.ZipFile(io.BytesIO(built.data)) as z:
        names = z.namelist()
        dumped = b"".join(z.read(n) for n in names).decode("utf-8", "ignore")
        rels = z.read("word/_rels/document.xml.rels").decode()
    media = next(n for n in names if n.startswith("word/media/"))
    assert f'Target="media/{media.rsplit("/", 1)[1]}"' in rels, "renamed parts keep rels valid"
    assert _real_in(dumped) == [] and "psmith" not in dumped, dumped


def _record(files: list[dict[str, object]], result: object = None) -> ThreadRecord:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    calls = [{"tool": "crm_get", "args": {}, "result": result}] if result is not None else []
    return ThreadRecord.model_validate(
        {
            "platform": "slack", "ref": "r1", "events_recorded": True,
            "people": [
                {"id": "U1", "name": "Renée Dubois", "groups": ["globex-account-team"]},
                {"id": "UB", "name": "bot", "is_bot": True},
            ],
            "messages": [
                {"id": "1", "author": "U1", "at": at, "text": "look", "files": files, "calls": calls},
                {"id": "2", "author": "UB", "at": at + timedelta(minutes=1), "text": "done"},
            ],
        }
    )  # fmt: skip


def test_thread_when_file_cannot_be_scrubbed_then_missing_and_groups_faked() -> None:
    record = _record(
        [{"name": "notes.zip", "media_type": "application/zip", "data": "PK Renée".encode()}]
    )
    (built,) = build_cases(record, _scrubber(record.entities()))
    (attachment,) = built.case["turn"]["messages"][0]["attachments"]  # type: ignore[index]
    assert attachment["state"] == "missing" and built.blobs == {}, "raw files are never stored"
    assert _real_in(json.dumps(built.case, ensure_ascii=False)) == [], "groups are faked too"


def test_thread_when_tool_returns_binary_payload_then_dropped_and_world_incomplete() -> None:
    image = base64.b64encode(b"\x89PNG" + bytes(range(256))).decode()
    inner = json.dumps({"author": "Renée Dubois"})
    content = [{"type": "text", "text": inner}, {"type": "image", "data": image, "mimeType": "x"}]
    record = _record([], result={"content": content})
    (built,) = build_cases(record, _scrubber(record.entities()))
    dumped = json.dumps(built.case, ensure_ascii=False)
    assert image not in dumped and _real_in(dumped) == [], dumped
    flags = [f["reason"] for f in built.case["review_flags"]]  # type: ignore[attr-defined]
    assert "world_incomplete" in flags, flags


def test_bundle_when_manifest_names_people_then_scrubbed_and_map_bound_to_key(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, source)
    manifest = json.loads((source / "bundle.json").read_text())
    manifest["title"] = "Alice Smith - Globex Industries escalations"
    manifest["created_by"] = {"kind": "label", "id": "alice-smith-laptop", "display_name": "Alice"}
    manifest["source"] = {"kind": "deployment", "label": "globex-prod", "instance": "https://g.io"}
    (source / "bundle.json").write_text(json.dumps(manifest))
    names, fakes = tmp_path / "names.json", tmp_path / "fakes.json"
    names.write_text(ENTITIES.model_dump_json())
    scrubber = load_scrubber(
        names, "0123456789abcdef0123", salt="t1", map_path=fakes, ner_model=None
    )
    assert "person" in {x.kind for x in leftovers_of(source, scrubber)}, "a raw label is flagged"
    scrub_path(source, None, scrubber)
    save_map(scrubber, fakes)
    out = (source / "bundle.json").read_text()
    assert _real_in(out) == [] and "g.io" not in out and "laptop" not in out, out
    kinds = {x.kind for x in leftovers_of(source, scrubber)}
    assert kinds <= {"names_unchecked"}, "the manifest sweeps clean"
    with pytest.raises(InvalidDocument):
        load_scrubber(names, "0123456789abcdef0123", salt="t2", map_path=fakes, ner_model=None)


# ---------- second review pass ----------


class _Names(Ner):
    """A stand-in for NER: the given words are people."""

    def __init__(self, *names: str) -> None:
        super().__init__("none")
        self._names = re.compile("|".join(map(re.escape, names)))

    @property
    def available(self) -> bool:
        return True

    def find(self, text: str) -> list[Hit]:
        return [Hit(m.start(), m.end(), "person", 1) for m in self._names.finditer(text)]

    def prefetch(self, texts: Iterable[str]) -> None:
        pass


def test_name_when_equal_to_an_issued_fake_then_still_replaced_and_sweep_clean() -> None:
    scrubber = _scrubber()
    fake = scrubber.fakes.fake("person", "Dana")
    scrubber.detector = Detector(terms_of(ENTITIES), _Names("Dana", fake))
    out = scrubber.text(f"Thanks Dana, and thanks {fake}.")
    assert out.count(fake) == 1, "the real name equal to a fake is replaced too"
    assert scrubber.residue(out) == {}, "fakes are not leftovers"


def test_fakes_when_dictionary_is_large_then_readable() -> None:
    faker = Faker("en_US")
    faker.seed_instance(7)
    people = sorted({faker.name() for _ in range(3000)})
    scrubber = _scrubber(KnownEntities.model_validate({"people": people}))
    for name in people[:200]:
        out = scrubber.text(f"{name} said hi; later {name.split()[-1]} left")
        assert not re.search(r"\d", out), out


def test_sweep_when_ner_widens_a_fake_then_clean_unless_a_real_word_joins() -> None:
    scrubber = _scrubber()
    fake = adapt("AcmeCorp", scrubber.fakes.fake("company", "Acme Corp"))
    scrubber.detector = Detector([], _Names(f"{fake} Team", f"{fake}_report", f"{fake} Johnson"))
    assert scrubber.residue(f"{fake} Team") == {}
    assert scrubber.residue(f"{fake}_report") == {}, "lowercase common words are no name"
    assert scrubber.residue(f"{fake} Johnson") == {"person": 1}


def test_text_when_a_fake_lets_ner_find_a_name_beside_it_then_replaced() -> None:
    scrubber = _scrubber()
    fake = scrubber.fakes.fake("person", "Dana")
    scrubber.detector = Detector([], _Names("Dana", f"{fake} and Kim"))
    out = scrubber.text("Thanks Dana and Kim.")
    assert "Kim" not in out and fake not in out
    assert scrubber.residue(out) == {}


@pytest.mark.parametrize(
    "doc",
    [
        {"api_keys": ["hunter2hunter2"]},
        {"tokens": [{"v": "hunter2hunter2"}]},
        {"credentials": {"value": "hunter2hunter2", "user": "hunter2"}},
        {"headers": {"Authorization": "Basic aHVudGVyMjpodW50ZXIy"}},
        {"headers": [{"name": "Authorization", "value": "Token hunter2hunter2"}]},
        {"headers": [["Proxy-Authorization", "hunter2"]]},
        {"pwd": "hunter2", "pin": 1234, "signingKey": "hunter2", "AccountKey": "hunter2"},
        {"phone": 14155550199},
    ],
)
def test_value_when_secret_in_nested_or_list_shapes_then_dropped(doc: dict[str, object]) -> None:
    scrubber = _scrubber()
    assert scrubber.residue_of(doc), "the sweep sees it"  # type: ignore[arg-type]
    out = json.dumps(scrubber.value(doc))  # type: ignore[arg-type]
    assert "hunter2" not in out and "1234" not in out and "4155550199" not in out, out
    assert scrubber.residue_of(json.loads(out)) == {}


@pytest.mark.parametrize(
    "text",
    [
        "password=abc123",
        "password: correct horse battery staple",
        'login with {"pwd": "s3cr3t"}',
        "AccountKey=hunter2hunter2==;EndpointSuffix=core",
        "ENCRYPTION_KEY=hunter2hunter2",
        "Authorization: Bearer hunter2",
        "Authorization: Basic aHVudGVyMjpodW50ZXIy",
        "send Basic aHVudGVyMjpodW50ZXIy",
        "postgres://u:hunter2/xx@db.example.com/d",
        "https://x.example/?k=hunter2hunter2",
        "SK" + "0123456789abcdef0123456789abcdef",
        "&#65;lice Smith and Alice&nbsp;Smith",
        "note: " + base64.b64encode(b"Alice Smith").decode(),
        "state=" + base64.urlsafe_b64encode(b"Alice Smith, hunter2!?>").decode(),
        "C:\\Users\\asmith\\x.txt /home/asmith/x",
    ],
)
def test_text_when_labelled_encoded_or_home_path_then_nothing_left(text: str) -> None:
    scrubber = _scrubber(KnownEntities.model_validate({"people": ["Alice Smith"]}))
    out = scrubber.text(text)
    leaked = ("hunter2", "abc123", "horse", "staple", "s3cr3t", "0123456789abcdef", "asmith")
    assert _real_in(out) == [] and not [x for x in leaked if x in out], out
    assert scrubber.residue(out) == {}, "the sweep finds nothing, nor its own markers"
    for word in re.findall(r"[A-Za-z0-9+/_-]{12,}={0,2}", out):
        body = word.rstrip("=")
        if len(body) % 4 != 1:
            decoded = base64.urlsafe_b64decode(body.replace("+", "-").replace("/", "_") + "==")
            assert b"Alice" not in decoded, out


def test_docx_when_embedded_files_nest_too_deep_then_refused() -> None:
    def zipped(parts: dict[str, bytes]) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in parts.items():
                z.writestr(name, data)
        return out.getvalue()

    doc = zipped({"[Content_Types].xml": b"<Types/>"})
    for _ in range(4):
        doc = zipped({f"e{i}.docx": doc for i in range(20)})
    assert rebuild(doc, DOCX, "a.docx", _scrubber()) is None, "flagged, not unpacked"


def test_entities_when_yaml_is_broken_then_error_names_no_line(tmp_path: Path) -> None:
    bad = tmp_path / "names.yaml"
    bad.write_text('people:\n  - "Alice Smith\n  - x: [\n')
    with pytest.raises(ValueError, match="line") as failed:
        load_entities(bad)
    assert "Alice" not in str(failed.value)


def test_value_when_bundle_document_then_keys_and_schema_fields_kept() -> None:
    scrubber = _scrubber(KnownEntities(phrases=["Kestrel Ops"], projects=["relay"]))
    scrubber.learn({"text": "write to helperbot@corp.example", "project": "weight"})
    case = {
        "schema": "daimon.eval.case/1",
        "world": {"mcp_servers": [{"name": "tools", "kind": "relay"}]},
        "turn": {"messages": [{"kind": "message", "text": "helperbot owns Kestrel Ops"}]},
        "ext": {"version": 1, "weight": 2.0, "Kestrel Ops": "a"},
    }
    out = scrubber.value(case)
    assert out["world"] == case["world"], "enums keep their values"
    assert list(out["ext"])[:2] == ["version", "weight"], "keys keep lowercase common words"
    assert "Kestrel Ops" not in json.dumps(out) and "helperbot" not in json.dumps(out)
    suite = scrubber.value({"schema": "daimon.eval.suite/1", "cases": [{"split": "test"}]})
    assert suite["cases"] == [{"split": "test"}]


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("id d1e3a32d1766 and Jane Roe", "d1e3a32d1766"),  # no digit term from "(+3)"
        ("PrimRose garden", "PrimRose garden"),  # one-word terms: whole words only
        ("see https://github.com/x and notes.md", "https://github.com/x and notes.md"),
        ("at docs.google.com/d and github.com", "docs.google.com/d and github.com"),
    ],
)
def test_text_when_word_inside_another_or_url_scheme_then_kept(text: str, kept: str) -> None:
    entities = KnownEntities.model_validate({"people": ["Jane Roe (+3)", "Rose Dunn"]})
    scrubber = _scrubber(
        entities.model_copy(update={"phrases": ["https", "test", "github", "google"]})
    )
    out = scrubber.text(text)
    assert kept in out and "Roe" not in out, out


@pytest.mark.parametrize(
    "real",
    [
        "100.90.141.55",
        "288838c5-de50-80c4-b6fb-f19866be187c",
        "288838c5de5080c4b6fbf19866be187c",
        "files.personal-site.co.uk",
        "shop.personal-site.com.pk",
    ],
)
def test_text_when_tailnet_ip_hex_id_or_private_host_then_faked_same_shape(real: str) -> None:
    out = _scrubber().text(f"see {real}/x and docs.google.com")
    fake = out.split()[1].removesuffix("/x")
    assert real not in out and "docs.google.com" in out, out
    if "-" in real and real[0].isdigit() or len(real) == 32:
        assert re.fullmatch(re.sub(r"[0-9a-f]", "[0-9a-f]", real), fake), fake


def test_bundle_when_ids_and_enums_hold_names_then_ids_faked_alike_enums_kept_and_valid(
    tmp_path: Path,
) -> None:
    """Ids get one id-safe fake wherever they are; enum values are kept; pins follow the cases."""
    loaded = validate_bundle(DirSource(EXAMPLE))
    assert loaded.manifest is not None
    data = b"<p>Acme Corp report</p>"
    blob = BlobEntry(
        sha256=sha256_hex(data), size=len(data), media_type="text/html", scrub="unscrubbed"
    )
    raw_case = loaded.suites[0].cases[0]
    world = raw_case["world"]
    assert isinstance(world, dict) and isinstance(world["exchanges"], list)
    exchanges = [{**x, "id": "x-Acme"} for x in world["exchanges"] if isinstance(x, dict)]
    case = {**raw_case, "id": "288838c5-de50-80c4-b6fb-f19866be187c"}
    case["world"] = {**world, "exchanges": exchanges}
    case["hash"] = case_hash(case)
    pin = {"id": case["id"], "revision": 1, "hash": case["hash"]}
    suite = LoadedSuite({**loaded.suites[0].document, "cases": [pin]}, [case])
    ref = blob.model_dump(include={"sha256", "size", "media_type"})
    output = {"id": "o1", "name": "a.html", "media_type": "text/html", "blob": ref}
    call = {"seq": 1, "surface": "mcp", "origin": "recorded", "exchange_id": "x-Acme"}
    enclosure = {"mode": "enclosed", "backend": "anthropic"}
    run = loaded.runs[0]
    item = {**run.items[0], "case": pin, "outputs": [output], "world_calls": [call]}
    items = [{**item, "enclosure": enclosure}]
    header = BundleHeader(
        id="b", created_at=datetime.now(UTC), source=SourceInfo(kind="converter"),
        scrub_status="unscrubbed",
    )  # fmt: skip
    source = DirSource(EXAMPLE)
    blobs = {b: source.read(f"blobs/sha256/{b.sha256}") for b in loaded.manifest.blobs}
    files = build_files(header, [suite], [replace(run, items=items)], {**blobs, blob: data})
    write_dir(files, tmp_path / "raw")
    scrubber = _scrubber(ENTITIES.model_copy(update={"companies": ["Acme Corp", "Anthropic"]}))
    scrub_path(tmp_path / "raw", tmp_path / "out", scrubber)
    report = validate_bundle(DirSource(tmp_path / "out"))
    assert report.ok, report.problems
    done = report.suites[0].cases[0]
    assert "288838c5" not in str(done["id"]), "the case id is faked and its pins follow"
    fake = json.dumps(done["world"]).split('"id": "', 1)[1].split('"', 1)[0]
    out_item = report.runs[0].items[0]
    assert "Acme" not in fake and re.fullmatch(r"x-[A-Za-z0-9._:-]+", fake), fake
    assert out_item["world_calls"] == [{**call, "exchange_id": fake}], "references follow the id"
    assert out_item["enclosure"] == enclosure, "an enum value is never faked"


# ---------- memory ----------


def test_ner_when_many_texts_then_cache_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-text cache is cleared at its limit, so a long scrub never grows without bound."""
    monkeypatch.setattr(detect, "_NER_CACHE", 5)

    class _Fixed(Ner):
        @property
        def available(self) -> bool:
            return True

        def _run(self, texts: list[str]) -> list[list[tuple[int, int, str]]]:
            return [[(0, 1, "person")] for _ in texts]

    ner = _Fixed("none")
    for n in range(50):
        ner.find(f"text {n}")
    ner.prefetch(f"more {n}" for n in range(50))
    assert len(ner._cache) <= 5, "the cache grew past its limit"  # pyright: ignore[reportPrivateUsage]


def test_ner_when_texts_are_long_then_batches_are_bounded_by_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spaCy holds a whole batch of documents at once: a batch is capped by characters too."""
    monkeypatch.setattr(detect, "_NER_BATCH_CHARS", 100)
    texts = ["x" * 40] * 7 + ["y" * 500] + ["z"] * 100
    groups = detect._groups(texts, 64)  # pyright: ignore[reportPrivateUsage]
    assert [t for g in groups for t in g] == texts, "texts keep their order"
    assert all(len(g) == 1 or sum(map(len, g)) <= 100 for g in groups), "a batch is too large"
    assert all(len(g) <= 64 for g in groups), "a batch has too many texts"
    assert len(groups) < len(texts), "texts are still batched"


# ---------- third review pass ----------


def _fake_beside(*names: str) -> tuple[Scrubber, str]:
    scrubber = _scrubber()
    fake = scrubber.fakes.fake("person", "Dana")
    scrubber.detector = Detector([], _Names("Dana", *(f"{fake} {n}" for n in names)))
    return scrubber, fake


@pytest.mark.parametrize("beside", ["okonkwo", "Price", "Okonkwo"])
def test_text_when_ner_widens_a_fake_by_a_name_or_capitalized_word_then_replaced(
    beside: str,
) -> None:
    scrubber, fake = _fake_beside(beside)
    assert scrubber.residue(f"{fake} {beside}") == {"person": 1}, "the sweep sees the surname"
    out = scrubber.text(f"Contact Dana {beside} at HQ")
    assert beside not in out, "a lowercase or capitalized word beside a fake can be a surname"
    assert scrubber.residue(out) == {}


def test_value_when_free_json_key_is_a_single_name_then_replaced_and_sweep_sees_it() -> None:
    scrubber = _scrubber()
    doc = {"alice": {"status": 1}, "acme_alice_notes": 2, "Priya": 3, "status": 4}
    assert sum(scrubber.residue_of(doc).values()) == 4, "the sweep reads keys too"
    out = scrubber.value(doc)
    assert _real_in(json.dumps(out)) == [], out
    assert "status" in out, "a lowercase common word is no name"
    assert scrubber.residue_of(out) == {}


@pytest.mark.parametrize(
    "host",
    [
        "zq-backups-7.s3.amazonaws.com",
        "mydb.c1x2y3.us-east-1.rds.amazonaws.com",
        "k3j2.execute-api.us-east-1.amazonaws.com",
        "svc.example.com",
        "zqbucket.storage.googleapis.com",
    ],
)
def test_text_when_host_has_a_customer_label_under_a_big_service_then_faked(host: str) -> None:
    out = _scrubber().text(f"see https://{host}/x and docs.google.com and api.github.com")
    assert host.split(".")[0] not in out, out
    assert "docs.google.com" in out and "api.github.com" in out, "generic labels stay public"


@pytest.mark.parametrize(
    ("url", "private"),
    [
        ("https://raw.githubusercontent.com/zqcorp/zqrepo/main/x.py", ["zqcorp", "zqrepo"]),
        ("https://api.github.com/repos/zqcorp/zqrepo/issues", ["zqcorp", "zqrepo"]),
        ("https://www.linkedin.com/in/jane-roe-123abc", ["jane-roe"]),
        ("https://twitter.com/janeroe99", ["janeroe99"]),
        ("https://x.com/jroe", ["jroe"]),
        ("https://www.youtube.com/@RoeLabs", ["RoeLabs"]),
    ],
)
def test_text_when_repo_or_profile_address_then_owner_and_account_replaced(
    url: str, private: list[str]
) -> None:
    out = _scrubber().text(f"see {url} and x.com/home")
    assert not any(p in out for p in private), out
    assert url.split("/")[2] in out and "x.com/home" in out, "the service stays"


def test_value_when_tool_or_effect_name_holds_a_known_name_then_replaced_in_the_same_form() -> None:
    scrubber = _scrubber()
    case = {
        "schema": "daimon.eval.case/1",
        "world": {
            "exchanges": [
                {"tool": "acme_crm_lookup", "effects": [{"verb": "acme_crm.contact.update"}]},
                {"tool": "search_*"},
            ]
        },
        "expect": {"forbidden_effects": ["acme_crm.contact.delete"]},
    }
    assert scrubber.residue_of(case) == {"company_first": 3}, "the sweep reads tool and verb names"
    out = scrubber.value(case)
    exchanges = out["world"]["exchanges"]
    assert _real_in(json.dumps(out)) == [], out
    assert re.fullmatch(r"[a-z0-9_]+_crm_lookup", exchanges[0]["tool"]), exchanges[0]
    assert re.fullmatch(r"[a-z0-9_]+_crm(\.[a-z0-9_]+)+", exchanges[0]["effects"][0]["verb"])
    assert re.fullmatch(r"[a-z0-9_]+_crm(\.[a-z0-9_]+)+", out["expect"]["forbidden_effects"][0])
    assert exchanges[1]["tool"] == "search_*", "a name with no known name is kept"
    assert scrubber.residue_of(out) == {}


def test_text_when_custom_scheme_then_names_in_it_replaced_but_standard_scheme_kept() -> None:
    scrubber = _scrubber(ENTITIES.model_copy(update={"phrases": ["https"]}))
    out = scrubber.text("open acme-app://x and acme+ssh://h and https://docs.google.com")
    assert "acme" not in out and "https://docs.google.com" in out, out


def test_scrub_when_batches_share_a_learned_file_then_names_carry_over(tmp_path: Path) -> None:
    names, learned = tmp_path / "names.json", tmp_path / "learned.json"
    names.write_text(ENTITIES.model_dump_json())
    key = "0123456789abcdef0123"
    first = load_scrubber(names, key, salt="t", learned_path=learned, ner_model=None)
    first.detector = Detector(terms_of(ENTITIES), _Names("Okonkwo"))
    first.learn({"text": "ask Okonkwo"})
    assert "Okonkwo" not in first.text("hi Okonkwo again"), "learned within the run"
    save_learned(first, learned)
    assert learned.stat().st_mode & 0o777 == 0o600, "the file holds real names"
    second = load_scrubber(names, key, salt="t", learned_path=learned, ner_model=None)
    assert "Okonkwo" not in second.text("later: Okonkwo"), "read at the start of the next run"
    assert second.learned == [], "terms read from the file are not learned again"
    second.learn({"text": "and ask Raman Singh"})
    second.detector = Detector([], _Names("Kowalczyk"))
    second.learn({"text": "ask Kowalczyk"})
    save_learned(second, learned)
    texts = {r["text"] for r in json.loads(learned.read_text())}
    assert texts == {"Okonkwo", "Kowalczyk"}, "later runs add to the file"


def test_scrub_when_write_fails_then_map_and_learned_file_already_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, source)
    names, fakes, learned = tmp_path / "n.json", tmp_path / "fakes.json", tmp_path / "l.json"
    names.write_text(ENTITIES.model_dump_json())
    scrubber = load_scrubber(names, "0123456789abcdef0123", salt="t", ner_model=None)

    def broken(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(offline, "_write", broken)
    with pytest.raises(OSError, match="disk full"):
        scrub_path(source, tmp_path / "out", scrubber, map_path=fakes, learned_path=learned)
    assert fakes.exists() and learned.exists(), (
        "fakes must be in the map before a bundle holds them"
    )


def test_save_map_when_two_runs_save_then_entries_merge_atomically(tmp_path: Path) -> None:
    fakes = tmp_path / "fakes.json"
    one, two = _scrubber(), _scrubber()
    one.text("Alice Smith")
    two.text("Priya Raman")
    save_map(one, fakes)
    save_map(two, fakes)
    rows = json.loads(fakes.read_text())["entries"]
    assert len(rows) == len(one.fakes.added) + len(two.fakes.added), "no entry is lost"
    assert fakes.stat().st_mode & 0o777 == 0o600
    assert {p.name for p in tmp_path.iterdir()} <= {"fakes.json", "fakes.json.lock"}, "no temp left"


def test_value_when_ids_look_secret_then_redacted_ids_differ_and_repeat() -> None:
    scrubber = _scrubber()
    one, two = ("sk-ant-" + c * 30 for c in "ab")
    ids = [scrubber.value({"schema": "daimon.eval.case/1", "id": i})["id"] for i in (one, two, one)]
    assert ids[0] != ids[1], "two secret ids must not share a fake"
    assert ids[0] == ids[2] and ids[0].startswith("REDACTED-"), ids


def test_sweep_when_free_value_looks_like_a_case_then_seen_as_value_scrubs_it() -> None:
    scrubber = _scrubber()
    doc = {"ext": {"schema": "daimon.eval.case/1", "surface": "Alice Smith"}}
    assert "Alice" not in json.dumps(scrubber.value(doc)), "free JSON is scrubbed as text"
    assert scrubber.residue_of(doc) == {"person": 1}, "the sweep agrees with the scrub"


@pytest.mark.parametrize(
    ("text", "real"),
    [
        ("shop.personal-site.com-backup", "personal-site"),
        ("a x.personal-site.io- b", "personal-site"),
        ("id 1234567890123456789abc", "1234567890123456789"),
        ("id x1234567890123456789", "1234567890123456789"),
        ("at %10.1.2.3% now", "10.1.2.3"),
        ("at %10.0.0.1%", "10.0.0.1"),
    ],
)
def test_text_when_host_id_or_ip_is_glued_to_other_characters_then_replaced(
    text: str, real: str
) -> None:
    scrubber = _scrubber()
    out = scrubber.text(text)
    assert real not in out, out
    assert scrubber.residue(out) == {}, "the sweep is clean too"
