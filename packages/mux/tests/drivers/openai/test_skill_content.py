"""Native version bytes, rather than metadata claims, prove retained skill pins."""

import io
import stat
import zipfile

import httpx
import pytest
from mux.contracts.ids import Scope, SkillRef
from mux.contracts.resources import SkillUpload, SkillUploadFile
from mux.drivers.openai._common import Context
from mux.drivers.openai.skill_bindings import resolve_pins
from mux.drivers.openai.skills import bundle_digest
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)
BUNDLE = SkillUpload(
    files=(
        SkillUploadFile(path="SKILL.md", content=b"retained"),
        SkillUploadFile(path="binary", content=b"\xff\x00"),
    )
)


def packed(fault):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in reversed(BUNDLE.files):
            name = "wrapper/" + file.path
            if fault == "traversal":
                name = "../" + file.path
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 16, 0, 0, 0))
            if fault == "symlink":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"changed" if fault == "bytes" else file.content)
        if fault == "extra":
            archive.writestr("wrapper/additional", b"unclaimed")
    return b"not a zip" if fault == "malformed" else output.getvalue()


@pytest.mark.parametrize("fault", [None, "traversal", "symlink", "bytes", "extra", "malformed"])
async def test_native_archive_pin_verifies_all_files_and_closes_owned_stream(fault):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=packed(fault))
        assert request.url.path == "/v1/skills/native/versions/7"
        return httpx.Response(200, json={"skill_id": "native", "version": "7"})

    async with build_async_openai(transport=httpx.MockTransport(handle)) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda actual, *_: actual == SCOPE,
        )
        pins = (SkillRef(id="native", version="7", digest=bundle_digest(BUNDLE)),)
        if fault is None:
            assert await resolve_pins(pins, SCOPE, context) == pins
        else:
            with pytest.raises(ValueError):
                await resolve_pins(pins, SCOPE, context)
    assert [call.url.path for call in calls] == [
        "/v1/skills/native/versions/7",
        "/v1/skills/native/versions/7/content",
    ]


def test_plain_manifest_when_description_is_a_block_scalar_then_uploads_one_quoted_line():
    """OpenAI rejects sessions attaching a `description: >` skill; plain ones stay byte for byte."""
    from mux.drivers.openai.skills import bundle_zip, plain_manifest

    folded = (
        b"---\nname: lint\ndescription: >\n  Lint data.\n  Use it: often.\nlicense: MIT\n---\nbody"
    )
    zipped = bundle_zip(SkillUpload(files=(SkillUploadFile(path="SKILL.md", content=folded),)))
    uploaded = zipfile.ZipFile(io.BytesIO(zipped)).read("bundle/SKILL.md")
    assert uploaded == (
        b'---\nname: lint\ndescription: "Lint data. Use it: often."\nlicense: MIT\n---\nbody'
    ), "the folded value becomes one quoted line; other keys and the body are kept"
    assert plain_manifest(uploaded) == uploaded, "a rewritten manifest is stable"
    plain = b"---\nname: lint\ndescription: Lint data.\n---\nbody"
    assert plain_manifest(plain) == plain, "a plain manifest keeps its bytes and pinned digest"
    named = b"---\nname: Marimo__Notebooks_\ndescription: d\n---\n"
    assert plain_manifest(named) == b"---\nname: marimo-notebooks\ndescription: d\n---\n", (
        "a name OpenAI refuses becomes its unquoted slug"
    )
