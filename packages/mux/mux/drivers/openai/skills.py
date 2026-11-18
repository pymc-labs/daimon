"""Skills bundles travel inline in the one native multipart request."""

from __future__ import annotations

import asyncio
import io
import json
import re
import stat
import struct
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import cast

import yaml

from mux.contracts.errors import ProviderError
from mux.contracts.ids import Page, PageRequest, Scope, SkillRef
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Skill, SkillUpload, SkillUploadFile, SkillVersion
from mux.drivers.openai._common import Context, owned, page_of, query, text, timestamp
from mux.drivers.openai.transport import Object, segment
from mux.state.operations import request_digest

_POST_PUBLISH_READ_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)
"""Bounded ~15.5s backoff applied to the version-metadata and content reads
inside skill-pin resolution (`skill_bindings.resolve_pins`), to content
verification, and to the read-back that `create` and `publish_version` wait
for; never to `retrieve`/`list`/`delete`.

Root-caused live against the QA OpenAI key (neutral-core F2-O probe, then
confirmed independently against the same key): a freshly published version's
metadata GET (`/skills/{id}/versions/{version}`) and content GET
(`.../content`) both 404 immediately after `POST .../versions` (or the
create call's own first version) returns 200, then read 200 within
single-digit seconds — provider-side read-after-write lag, not a wrong or
deleted skill, and not a path bug.

Caller tradeoff, by design: `resolve_pins` has no way to tell "this pin was
just published" from "this pin has referenced a long-deleted skill for
weeks" — both go through the same GET. A 404 on the latter now takes the
full ~15.5s to surface instead of failing immediately, on every agent/session
creation that touches it, not just once. That bounded slowdown on a genuinely
stale reference is the accepted cost of not silently swallowing the real,
common case this exists for. A 404 that survives the whole backoff is final
either way.
"""


async def retry_not_found[T](call: Callable[[], Awaitable[T]]) -> T:
    """Retry `call` on a `not_found` ProviderError with the post-publish backoff.

    Any other exception, including a `not_found` that survives every delay,
    propagates unchanged.
    """
    for delay in _POST_PUBLISH_READ_RETRY_DELAYS_S:
        try:
            return await call()
        except ProviderError as error:
            if error.category != "not_found":
                raise
            await asyncio.sleep(delay)
    return await call()


_FRONTMATTER = re.compile(r"\A---\r?\n(.*?\r?\n)---[ \t]*(?=\r?\n|\Z)", re.DOTALL)
_KEY = re.compile(r"(name|description)[ \t]*:")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def plain_manifest(content: bytes) -> bytes:
    """SKILL.md with a multi-line or block `name`/`description` as one quoted line,
    and a `name` OpenAI refuses as its lowercase hyphen slug.

    OpenAI stores a block scalar (`description: >`) with its line breaks at upload,
    then refuses every session attaching it: "archive metadata does not match its
    Skills API version". A one-line value, quoted or commented too, keeps its bytes,
    so the digests of skills already pinned do not change. Any rewrite that does not
    load back as the same values keeps the original bytes.
    """
    try:
        match = _FRONTMATTER.match(content.decode())
        loaded: object = yaml.safe_load(match[1]) if match else None
    except (UnicodeDecodeError, yaml.YAMLError):
        return content
    if match is None or not isinstance(loaded, dict):
        return content
    values = cast(dict[object, object], loaded)
    expected = dict(values)
    # "\n" only: `str.splitlines` also breaks at U+2028, U+2029 and NEL inside a value.
    lines, out, index = match[1].split("\n"), list[str](), 0
    while index < len(lines):
        line, index = lines[index], index + 1
        key = _KEY.match(line)
        value = values.get(key[1]) if key else None
        if key is None or not isinstance(value, str):
            out.append(line)
            continue
        # A value's own lines: indented ones, and blank ones before another indented.
        end = index
        while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
            end += 1
        while end > index and not lines[end - 1].strip():
            end -= 1
        if key[1] == "name" and _NAME.fullmatch(value) is None:
            # OpenAI refuses any other name; its slug is written unquoted.
            plain = written = "-".join(re.findall(r"[a-z0-9]+", value.lower()))
        elif end > index or "\n" in value:
            plain = " ".join(value.split())
            written = json.dumps(plain, ensure_ascii=False)
        else:
            plain = written = ""
        if not plain:  # a one-line value, or a name with no slug, keeps its line
            out.append(line)
            continue
        out.append(f"{key[1]}: {written}")
        expected[key[1]] = plain
        index = end
    try:
        if out == lines or yaml.safe_load("\n".join(out)) != expected:
            return content
    except yaml.YAMLError:
        return content
    return (match.string[: match.start(1)] + "\n".join(out) + match.string[match.end(1) :]).encode()


def plain_bundle(bundle: SkillUpload) -> SkillUpload:
    return bundle.model_copy(
        update={
            "files": tuple(
                f.model_copy(update={"content": plain_manifest(f.content)})
                if f.path == "SKILL.md"
                else f
                for f in bundle.files
            )
        }
    )


def bundle_digest(bundle: SkillUpload) -> str:
    """Pin file paths and the uploaded bytes independently of ZIP ordering and timestamps."""
    plain = plain_bundle(bundle)
    canonical = plain.model_copy(update={"files": tuple(sorted(plain.files, key=lambda f: f.path))})
    return request_digest(canonical.model_dump(mode="json"))


async def _download_content(context: Context, skill_id: str, version: str) -> bytearray:
    data = bytearray()
    source = await context.transport.download(
        f"/skills/{segment(skill_id)}/versions/{segment(version)}/content"
    )
    try:
        async for part in source:
            data.extend(part)
            if len(data) > 50 * 1024 * 1024:
                raise ValueError("skill archive exceeds native size limit")
    finally:
        closer = getattr(source, "aclose", None)
        if closer is not None:
            await closer()
    return data


async def verify_content(context: Context, skill_id: str, version: str, digest: str) -> None:
    """Read the selected native version's complete bounded archive, never latest."""
    data = await retry_not_found(lambda: _download_content(context, skill_id, version))
    try:
        bundle = _downloaded_bundle(data)
    except (zipfile.BadZipFile, NotImplementedError, zlib.error, EOFError) as error:
        raise ValueError("invalid native skill archive") from error
    if bundle_digest(bundle) != digest:
        raise ValueError("native skill content differs from pinned source bytes")


def _downloaded_bundle(data: bytearray) -> SkillUpload:
    # Refuse an oversized central directory before ZipFile allocates its entries.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 22 - 65535))
    if end < 0 or len(data) - end < 22 or struct.unpack_from("<H", data, end + 10)[0] > 1000:
        raise ValueError("invalid or oversized skill archive directory")
    files: list[SkillUploadFile] = []
    budget = 28 * 1024 * 1024
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000:
            raise ValueError("skill archive has too many entries")
        members = [entry for entry in entries if not entry.is_dir()]
        if len(members) > 500:
            raise ValueError("skill archive has too many files")
        # Native exports may wrap the bundle in one directory. Every file is
        # retained; only this common wrapper is removed.
        names = [entry.filename for entry in members]
        for name in names:
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or str(path) != name
                or "\\" in name
                or "\x00" in name
                or any(part in (".", "..") for part in name.split("/"))
            ):
                raise ValueError("invalid skill archive path")
        wrapper = (
            names[0].split("/", 1)[0] + "/"
            if names
            and "SKILL.md" not in names
            and all("/" in name for name in names)
            and len({name.split("/", 1)[0] for name in names}) == 1
            else ""
        )
        for entry in members:
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                raise ValueError("skill archive contains links or encrypted files")
            with archive.open(entry) as member:
                body = member.read(min(budget, 25 * 1024 * 1024) + 1)
            if len(body) > budget or len(body) > 25 * 1024 * 1024:
                raise ValueError("skill archive exceeds uncompressed size limit")
            budget -= len(body)
            files.append(SkillUploadFile(path=entry.filename.removeprefix(wrapper), content=body))
    bundle = SkillUpload(files=tuple(files))
    bundle_zip(bundle)  # Reuse upload path, uniqueness and root-manifest validation.
    return bundle


def bundle_zip(bundle: SkillUpload) -> bytes:
    if not bundle.files:
        raise ValueError("empty skill bundle")
    paths: set[str] = set()
    manifests: list[str] = []
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in plain_bundle(bundle).files:
            path = PurePosixPath(file.path)
            if (
                path.is_absolute()
                or any(part in (".", "..") for part in file.path.split("/"))
                or "\\" in file.path
                or "\x00" in file.path
                or str(path) != file.path
            ):
                raise ValueError("invalid bundle path")
            if file.path in paths:
                raise ValueError("duplicate bundle path")
            paths.add(file.path)
            if path.name.casefold() == "skill.md":
                manifests.append(file.path)
            info = zipfile.ZipInfo("bundle/" + file.path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, file.content)
    if manifests != ["SKILL.md"]:
        raise ValueError("one root SKILL.md is required")
    return result.getvalue()


class OpenAISkills:
    def __init__(self, context: Context) -> None:
        self._c = context
        self._resource = context.transport

    def _decode(self, raw: Object) -> Skill:
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("invalid description")
        return Skill(
            id=text(raw["id"]),
            display_title=text(raw["name"]),
            description=description,
            latest_version=SkillRef(id=text(raw["id"]), version=text(raw["latest_version"])),
            created_at=timestamp(raw["created_at"]),
        )

    async def _readable(self, skill_id: str, version: str) -> None:
        # A new version 404s for a few seconds; a session attaching it then fails.
        await retry_not_found(
            lambda: self._c.call("GET", f"/skills/{segment(skill_id)}/versions/{segment(version)}")
        )

    @owned
    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
        self._c.authorize(scope, "skill")
        if bundle.display_title is not None:
            raise self._c.unsupported("skill_display_title")
        body = bundle_zip(bundle)
        raw = await self._resource.multipart(
            "/skills", files=(("files", "bundle.zip", body, "application/zip"),), fields={}, key=key
        )
        skill = self._decode(raw)
        await self._readable(skill.id, text(raw["latest_version"]))
        return skill

    @owned
    async def publish_version(
        self, scope: Scope, skill_id: str, bundle: SkillUpload, *, key: str
    ) -> SkillVersion:
        self._c.authorize(scope, "skill", skill_id)
        if bundle.display_title is not None:
            raise self._c.unsupported("skill_display_title")
        body = bundle_zip(bundle)
        raw = await self._resource.multipart(
            f"/skills/{segment(skill_id)}/versions",
            files=(("files", "bundle.zip", body, "application/zip"),),
            fields={},
            key=key,
        )
        if raw.get("skill_id") != skill_id:
            raise ValueError("wrong published skill identity")
        version = text(raw["version"])
        description = raw.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("invalid description")
        await self._readable(skill_id, version)
        return SkillVersion(
            ref=SkillRef(id=skill_id, version=version),
            version=version,
            name=text(raw["name"]),
            description=description,
            created_at=timestamp(raw["created_at"]),
        )

    @owned
    async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
        self._c.authorize(scope, "skill", skill_id)
        raw = await self._c.call("GET", "/skills/" + segment(skill_id))
        if raw.get("id") != skill_id:
            raise ValueError("wrong skill identity")
        return self._decode(raw)

    @owned
    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Skill]:
        self._c.authorize(scope, "skill")
        if not scope.is_platform:
            raise self._c.unsupported("tenant_skill_listing")
        return page_of(await self._c.call("GET", "/skills", params=query(page)), self._decode)

    @owned
    async def delete(self, scope: Scope, skill_id: str, *, key: str) -> DeletionReceipt:
        self._c.authorize(scope, "skill", skill_id)
        await self._c.call("DELETE", "/skills/" + segment(skill_id), key=key)
        return DeletionReceipt(operation_id=key, deleted=(self._c.ref(scope, "skill", skill_id),))
