"""Skill preservation decisions use catalog facts, independent of provider IDs."""

from daimon.core.skills.ownership import AttachedSkillOwnership, partition_copy_skills


def test_a_fork_keeps_shared_pins_copies_owned_pins_and_drops_foreign_pins() -> None:
    result = partition_copy_skills(
        (
            AttachedSkillOwnership("library", custom=True, title="shared-notes", version="3"),
            AttachedSkillOwnership("owned", custom=True, title="source/notes", version="2"),
            AttachedSkillOwnership("foreign", custom=True, title="other/tips", version="4"),
            AttachedSkillOwnership("provider-catalog", custom=False, version="1"),
        ),
        source_name="source",
        agent_names=("source", "other"),
    )
    assert result.kept_positions == (0, 3)
    assert [(s.id, s.title, s.version) for s in result.own] == [("owned", "source/notes", "2")]
    assert result.dropped == ("other/tips",)


def test_a_catalog_pin_does_not_inherit_a_same_id_custom_upload_owner() -> None:
    result = partition_copy_skills(
        (AttachedSkillOwnership("collision", custom=False, upload_owner="other"),),
        source_name="source",
        agent_names=("source", "other"),
    )
    assert result.kept_positions == (0,)
    assert not result.own and not result.dropped


def test_stored_foreign_ownership_overrides_an_ambiguous_display_title() -> None:
    result = partition_copy_skills(
        (
            AttachedSkillOwnership(
                "foreign", custom=True, title="source/notes", upload_owner="other"
            ),
        ),
        source_name="source",
        agent_names=("source", "other"),
    )
    assert result.dropped == ("source/notes",)
    assert not result.kept_positions and not result.own


def test_latest_and_missing_versions_resolve_without_changing_an_explicit_pin() -> None:
    result = partition_copy_skills(
        tuple(
            AttachedSkillOwnership(
                str(i), custom=True, title="source/notes", version=pin, latest_version=latest
            )
            for i, (pin, latest) in enumerate(
                (("2", "5"), ("latest", "5"), (None, "5"), (None, None))
            )
        ),
        source_name="source",
        agent_names=("source",),
    )
    assert [s.version for s in result.own] == ["2", "5", "5", None]


def test_duplicate_shared_pins_keep_their_original_positions() -> None:
    pin = AttachedSkillOwnership("same", custom=True, title="shared")
    result = partition_copy_skills((pin, pin), source_name="source", agent_names=("source",))
    assert result.kept_positions == (0, 1)


def test_foreign_upload_with_no_visible_title_is_omitted_by_id() -> None:
    result = partition_copy_skills(
        (AttachedSkillOwnership("opaque", custom=True, upload_owner="other"),),
        source_name="source",
        agent_names=("source", "other"),
    )
    assert result.dropped == ("opaque",)
