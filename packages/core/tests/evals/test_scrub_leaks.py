"""Leaks found in review: each probe must come out with nothing real left, or be marked partial."""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daimon.core.evals.bundle import BundleHeader, DirSource, build_files, write_dir
from daimon.core.evals.format import BlobEntry, SourceInfo, case_hash, sha256_hex
from daimon.core.evals.from_thread import ThreadRecord, build_cases
from daimon.core.evals.scrub.detect import Detector, Hit, Ner
from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.entities import KnownEntities, load_entities, terms_of
from daimon.core.evals.scrub.fakes import adapt
from daimon.core.evals.scrub.files import rebuild
from daimon.core.evals.scrub.offline import leftovers_of, load_scrubber, save_map, scrub_path
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
    scrubber.detector = Detector([], _Names(f"{fake} Team", f"{fake} Johnson"))
    assert scrubber.residue(f"{fake} Team") == {}
    assert scrubber.residue(f"{fake} Johnson") == {"person": 1}


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
    scrubber = _scrubber(KnownEntities(phrases=["version", "Kestrel Ops"], projects=["relay"]))
    scrubber.learn({"text": "write to helperbot@corp.example", "project": "weight"})
    case = {
        "schema": "daimon.eval.case/1",
        "world": {"mcp_servers": [{"name": "tools", "kind": "relay"}]},
        "turn": {"messages": [{"kind": "message", "text": "helperbot owns Kestrel Ops"}]},
        "ext": {"version": 1, "weight": 2.0, "Kestrel Ops": "a"},
    }
    out = scrubber.value(case)
    assert out["world"] == case["world"], "enums keep their values"
    assert list(out["ext"])[:2] == ["version", "weight"], "keys keep words names share"
    assert "Kestrel Ops" not in json.dumps(out) and "helperbot" not in json.dumps(out)
    suite = scrubber.value({"schema": "daimon.eval.suite/1", "cases": [{"split": "test"}]})
    assert suite["cases"] == [{"split": "test"}]


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("id d1e3a32d1766 and Jane Roe", "d1e3a32d1766"),  # no digit term from "(+3)"
        ("PrimRose garden", "PrimRose garden"),  # one-word terms: whole words only
        ("see https://github.com/x and notes.md", "https://github.com/x and notes.md"),
    ],
)
def test_text_when_word_inside_another_or_url_scheme_then_kept(text: str, kept: str) -> None:
    entities = KnownEntities.model_validate({"people": ["Jane Roe (+3)", "Rose Dunn"]})
    scrubber = _scrubber(entities.model_copy(update={"phrases": ["https", "test"]}))
    out = scrubber.text(text)
    assert kept in out and "Roe" not in out, out


@pytest.mark.parametrize(
    "real",
    [
        "100.90.141.55",
        "288838c5-de50-80c4-b6fb-f19866be187c",
        "288838c5de5080c4b6fbf19866be187c",
        "files.personal-site.co.uk",
    ],
)
def test_text_when_tailnet_ip_hex_id_or_private_host_then_faked_same_shape(real: str) -> None:
    out = _scrubber().text(f"see {real}/x and docs.google.com")
    fake = out.split()[1].removesuffix("/x")
    assert real not in out and "docs.google.com" in out, out
    if "-" in real and real[0].isdigit() or len(real) == 32:
        assert re.fullmatch(re.sub(r"[0-9a-f]", "[0-9a-f]", real), fake), fake


def test_bundle_when_output_file_rebuilt_and_case_id_faked_then_valid(tmp_path: Path) -> None:
    loaded = validate_bundle(DirSource(EXAMPLE))
    assert loaded.manifest is not None
    data = b"<p>Acme Corp report</p>"
    blob = BlobEntry(
        sha256=sha256_hex(data), size=len(data), media_type="text/html", scrub="unscrubbed"
    )
    case = {**loaded.suites[0].cases[0], "id": "288838c5-de50-80c4-b6fb-f19866be187c"}
    case["hash"] = case_hash(case)
    pin = {"id": case["id"], "revision": 1, "hash": case["hash"]}
    suite = LoadedSuite({**loaded.suites[0].document, "cases": [pin]}, [case])
    ref = blob.model_dump(include={"sha256", "size", "media_type"})
    output = {"id": "o1", "name": "a.html", "media_type": "text/html", "blob": ref}
    run = loaded.runs[0]
    items = [{**run.items[0], "case": pin, "outputs": [output]}]
    header = BundleHeader(
        id="b", created_at=datetime.now(UTC), source=SourceInfo(kind="converter"),
        scrub_status="unscrubbed",
    )  # fmt: skip
    source = DirSource(EXAMPLE)
    blobs = {b: source.read(f"blobs/sha256/{b.sha256}") for b in loaded.manifest.blobs}
    files = build_files(header, [suite], [replace(run, items=items)], {**blobs, blob: data})
    write_dir(files, tmp_path / "raw")
    scrub_path(tmp_path / "raw", tmp_path / "out", _scrubber())
    report = validate_bundle(DirSource(tmp_path / "out"))
    assert report.ok, report.problems
    assert (
        "288838c5"
        not in (tmp_path / "out" / "suites").joinpath("support-basics", "cases.jsonl").read_text()
    ), "the case id is faked and its pins follow"
