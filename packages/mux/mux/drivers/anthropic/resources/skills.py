"""Skills and version history, preserving direct multipart upload requests."""

from collections.abc import AsyncIterator
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillCreateResponse, SkillListResponse, SkillRetrieveResponse
from anthropic.types.beta.skill_list_params import SkillListParams
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from anthropic.types.beta.skills.version_list_params import VersionListParams

from mux.contracts.ids import Page, PageRequest, Scope, SkillRef
from mux.contracts.receipts import DeletionReceipt
from mux.contracts.resources import Skill, SkillUpload, SkillVersion
from mux.drivers.anthropic.resources._errors import provider_call
from mux.errors import UnsupportedCapability


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
    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client

    async def create(self, scope: Scope, bundle: SkillUpload, *, key: str) -> Skill:
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
        item = await provider_call(
            self._client.beta.skills.versions.create(skill_id, files=bundle_files(bundle))
        )
        return version_record(item)

    async def retrieve(self, scope: Scope, skill_id: str) -> Skill:
        return skill_record(await provider_call(self._client.beta.skills.retrieve(skill_id)))

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[Skill]:
        kwargs: dict[str, object] = {}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            raise UnsupportedCapability(("skill_list_order",), "anthropic.managed_agents")
        result = await provider_call(self._client.beta.skills.list(**cast(SkillListParams, kwargs)))
        return Page(
            data=tuple(skill_record(item) for item in result.data),
            next_cursor=result.next_page,
            has_more=bool(result.next_page),
        )

    async def delete(self, scope: Scope, skill_id: str, *, key: str) -> DeletionReceipt:
        await provider_call(self._client.beta.skills.delete(skill_id))
        return DeletionReceipt(operation_id=key)


class AnthropicSkillVersions:
    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client

    async def versions(
        self, scope: Scope, skill_id: str, *, page: PageRequest
    ) -> Page[SkillVersion]:
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
            data=tuple(version_record(item) for item in result.data),
            next_cursor=result.next_page,
            has_more=bool(result.next_page),
        )

    async def download(self, scope: Scope, ref: SkillRef) -> AsyncIterator[bytes]:
        if ref.version is None:
            raise ValueError("a version must be pinned before downloading")
        content = await provider_call(
            self._client.beta.skills.versions.download(ref.version, skill_id=ref.id)
        )
        try:
            yield await provider_call(content.read())
        finally:
            await provider_call(content.close())

    async def delete_version(self, scope: Scope, ref: SkillRef, *, key: str) -> DeletionReceipt:
        if ref.version is None:
            raise ValueError("a version must be pinned before deleting")
        await provider_call(self._client.beta.skills.versions.delete(ref.version, skill_id=ref.id))
        return DeletionReceipt(operation_id=key)
