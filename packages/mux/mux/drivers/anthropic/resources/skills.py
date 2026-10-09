"""Skills and version history, preserving direct multipart upload requests."""

from collections.abc import AsyncIterator
from typing import Protocol, cast

from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import SkillCreateResponse, SkillListResponse, SkillRetrieveResponse
from anthropic.types.beta.skill_list_params import SkillListParams
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from anthropic.types.beta.skills.version_list_params import VersionListParams
from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, Scope, SkillRef
from mux.contracts.ports import SkillVersions
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Skill, SkillUpload, SkillVersion
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    visible_skill,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.errors import UnsupportedCapability


class NativeSkillVersions(SkillVersions, Protocol):
    """anthropic.skills_versions@1 workspace-key download by native version ID."""

    def walk_native(self, scope: Scope, skill_id: str) -> AsyncIterator[dict[str, JsonValue]]: ...

    def download_by_id(
        self, scope: Scope, skill_id: str, version_id: str
    ) -> AsyncIterator[bytes]: ...


def skill_record(item: SkillCreateResponse | SkillListResponse | SkillRetrieveResponse) -> Skill:
    return Skill.model_validate(
        {
            "id": item.id,
            "native": item.model_dump(mode="json", exclude_unset=True),
            "display_title": item.display_title,
            "source": item.source,
            "latest_version": {"id": item.id, "version": item.latest_version, "source": item.source}
            if item.latest_version is not None
            else None,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
    )


def version_record(item: VersionCreateResponse | VersionListResponse) -> SkillVersion:
    return SkillVersion.model_validate(
        {
            "ref": {"id": item.skill_id, "version": item.version, "source": "custom"},
            "native": item.model_dump(mode="json", exclude_unset=True),
            "version": item.version,
            "name": item.name,
            "description": item.description,
            "created_at": item.created_at,
        }
    )


def bundle_files(bundle: SkillUpload) -> list[tuple[str, bytes, str | None]]:
    return [(file.path, file.content, file.media_type) for file in bundle.files]


class AnthropicSkills:
    def __init__(
        self, client: AsyncAnthropic, authorization: ResourceAuthorization | None = None
    ) -> None:
        self._client = client
        self._authorization = authorization

    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
        authorize(self._authorization, scope, "skill", None)
        kwargs = (
            {"display_title": bundle.display_title}
            if "display_title" in bundle.model_fields_set
            else {}
        )
        from anthropic.types.beta.skill_create_params import SkillCreateParams

        item = await provider_call(
            self._client.beta.skills.create(
                **cast(SkillCreateParams, {**kwargs, "files": bundle_files(bundle)})
            )
        )
        return skill_record(item)

    async def publish_version(
        self, scope: Scope, skill_id: str, bundle: SkillUpload, *, key: str
    ) -> SkillVersion:
        authorize(self._authorization, scope, "skill", skill_id)
        item = await provider_call(
            self._client.beta.skills.versions.create(skill_id, files=bundle_files(bundle))
        )
        return version_record(item)

    async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
        authorize(self._authorization, scope, "skill", skill_id)
        return skill_record(await provider_call(self._client.beta.skills.retrieve(skill_id)))

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Skill]:
        authorize(self._authorization, scope, "skill", None)
        kwargs: dict[str, object] = {}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            raise UnsupportedCapability(("skill_list_order",), "anthropic.managed_agents")
        result = await provider_call(self._client.beta.skills.list(**cast(SkillListParams, kwargs)))
        return Page(
            data=tuple(
                skill_record(item)
                for item in (result.data or ())
                if visible_skill(self._authorization, scope, item.id, item.source)
            ),
            next_cursor=result.next_page or None,
            has_more=bool(result.next_page),
        )

    async def delete(self, scope: Scope, skill_id: str, *, key: str) -> DeletionReceipt:
        authorize(self._authorization, scope, "skill", skill_id)
        await provider_call(self._client.beta.skills.delete(skill_id))
        return DeletionReceipt(operation_id=key)

    async def pages(self, scope: Scope, *, limit: int) -> AsyncIterator[Page[Skill]]:
        """Use the SDK page iterator, including its empty-page stop rule."""
        authorize(self._authorization, scope, "skill")
        page = await provider_call(self._client.beta.skills.list(limit=limit))
        async for current in provider_iter(page.iter_pages()):
            yield Page(
                data=tuple(
                    skill_record(item)
                    for item in (current.data or ())
                    if visible_skill(self._authorization, scope, item.id, item.source)
                ),
                next_cursor=current.next_page or None,
                has_more=bool(current.next_page),
            )


class AnthropicSkillVersions:
    def __init__(
        self, client: AsyncAnthropic, authorization: ResourceAuthorization | None = None
    ) -> None:
        self._client = client
        self._authorization = authorization

    async def versions(
        self, scope: Scope, skill_id: str, *, page: PageRequest
    ) -> Page[SkillVersion]:
        authorize(self._authorization, scope, "skill", skill_id)
        kwargs: dict[str, object] = {}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            raise UnsupportedCapability(("skill_versions_order",), "anthropic.managed_agents")
        result = await provider_call(
            self._client.beta.skills.versions.list(skill_id, **cast(VersionListParams, kwargs))
        )
        return Page(
            data=tuple(version_record(item) for item in (result.data or ())),
            next_cursor=result.next_page or None,
            has_more=bool(result.next_page),
        )

    async def download(self, scope: Scope, ref: SkillRef) -> AsyncIterator[bytes]:
        authorize(self._authorization, scope, "skill", ref.id)
        if ref.version is None:
            raise ValueError("a version must be pinned before downloading")
        content = await provider_call(
            self._client.beta.skills.versions.download(ref.version, skill_id=ref.id)
        )
        try:
            yield await provider_call(content.read())
        finally:
            await provider_call(content.close())

    async def walk_native(self, scope: Scope, skill_id: str) -> AsyncIterator[dict[str, JsonValue]]:
        """Preserve main's lazy list and partial native version rows."""
        authorize(self._authorization, scope, "skill", skill_id)
        async for item in provider_iter(self._client.beta.skills.versions.list(skill_id=skill_id)):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json", exclude_unset=True))

    async def download_by_id(
        self, scope: Scope, skill_id: str, version_id: str
    ) -> AsyncIterator[bytes]:
        authorize(self._authorization, scope, "skill", skill_id)
        content = await provider_call(
            self._client.beta.skills.versions.download(
                version_id, skill_id=skill_id, extra_headers={"anthropic-beta": omit}
            )
        )
        try:
            yield await provider_call(content.read())
        finally:
            await provider_call(content.close())

    async def delete_version(self, scope: Scope, ref: SkillRef, *, key: str) -> DeletionReceipt:
        authorize(self._authorization, scope, "skill", ref.id)
        if ref.version is None:
            raise ValueError("a version must be pinned before deleting")
        await provider_call(self._client.beta.skills.versions.delete(ref.version, skill_id=ref.id))
        return DeletionReceipt(operation_id=key)

    async def walk(
        self, scope: Scope, skill_id: str, *, limit: int | None = None
    ) -> AsyncIterator[SkillVersion]:
        """Keep SDK iteration while callers delete versions during the walk."""
        authorize(self._authorization, scope, "skill", skill_id)
        kwargs: VersionListParams = {"limit": limit} if limit is not None else {}
        async for item in provider_iter(self._client.beta.skills.versions.list(skill_id, **kwargs)):
            yield version_record(item)
