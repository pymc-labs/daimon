"""Skills bundles travel inline in the one native multipart request."""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from mux.contracts.ids import Page, PageRequest, Scope, SkillRef
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Skill, SkillUpload, SkillVersion
from mux.drivers.openai._common import Context, owned, page_of, query, text, timestamp
from mux.drivers.openai.transport import Object, segment


def bundle_zip(bundle: SkillUpload) -> bytes:
    if not bundle.files:
        raise ValueError("empty skill bundle")
    paths: set[str] = set()
    manifests: list[str] = []
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in bundle.files:
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

    @owned
    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
        self._c.authorize(scope, "skill")
        if bundle.display_title is not None:
            raise self._c.unsupported("skill_display_title")
        body = bundle_zip(bundle)
        raw = await self._resource.multipart(
            "/skills", files=(("files", "bundle.zip", body, "application/zip"),), fields={}, key=key
        )
        return self._decode(raw)

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
