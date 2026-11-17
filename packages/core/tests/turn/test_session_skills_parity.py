"""Every profile that installs a skill bundle attaches an agent's custom and channel skills."""

from __future__ import annotations

from daimon.core.turn.session_skills import attaches_session_skills
from mux.profiles import PROFILES

#: Profiles that take a bundle but carry no template's skills per session, and why.
OTHER_WAY = {
    "anthropic.managed_agents": "the source provider: its skills ride on the agent itself",
    "selfhosted.loop": "its skills support is unknown until the server is probed",
}


def test_every_profile_with_a_skills_bundle_attaches_custom_and_channel_skills() -> None:
    for profile_id, profile in PROFILES.items():
        if profile.support_for("skills_bundle") == "unsupported":
            assert not attaches_session_skills(profile), profile_id
            continue
        assert attaches_session_skills(profile) or profile_id in OTHER_WAY, profile_id
    for profile_id in OTHER_WAY:
        assert not attaches_session_skills(PROFILES[profile_id]), f"{profile_id} closed its gap"


def test_gemini_keeps_skills_within_its_shared_inline_budget() -> None:
    """Each skill fits alone; the one that would overrun the session's total is a named loss."""
    import uuid

    from daimon.core.stores.domain import SkillSizeRow
    from daimon.core.turn.session_skills import fitting_skills, skill_losses, skill_names
    from mux.contracts.ids import SkillRef

    profile = PROFILES["gemini.inline_reuse"]
    pins = tuple(SkillRef(id=f"s{i}", version="1") for i in range(3))
    sizes = {
        (pin.id, "1"): SkillSizeRow(
            tenant_id=uuid.uuid4(),
            skill_id=pin.id,
            version="1",
            name=f"skill-{pin.id}",
            total_bytes=900 * 1024,
            largest_file_bytes=900 * 1024,
            file_count=1,
            text_only=True,
        )
        for pin in pins
    }
    kept = fitting_skills(profile, pins, sizes)
    assert kept == pins[:2], "two fit in 2 MiB, the third does not"
    assert skill_losses(pins, kept) == ("skill:s2",), "named, never silent"
    assert skill_losses(pins, ()) == tuple(f"skill:{p.id}" for p in pins), (
        "a skill measured or not is the same loss, so a consent holds across captures"
    )
    assert skill_names(sizes) == {p.id: f"skill-{p.id}" for p in pins}, "names for the card"


def test_a_skill_loss_shows_its_name_on_every_surface() -> None:
    """The drop keys a skill by id; the card, its words and the preview show the name."""
    import uuid

    from daimon.core.channel_backend import compatibility_preview, switch_losses
    from daimon.core.confirmation import drop_words
    from daimon.core.stores.domain import BackendAgent, SourceRepresentation, TenantBackendPolicy
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec

    target = AgentSpec(name="helper", model=ModelRef(provider="gemini", id="gemini-x"))
    agent = BackendAgent(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="config",
        representations=(
            SourceRepresentation(
                source_pin="0" * 64, target=target, skill_names={"skill_01a": "pdf-tools"}
            ),
        ),
    )
    drops = ("skill:skill_01a", "skill:skill_01b")
    assert switch_losses(drops, agent)[0] == ("pdf-tools", "skill_01b"), "named, else its id"
    policy = TenantBackendPolicy(
        tenant_id=agent.tenant_id, enabled_backends=("gemini",), revision="r"
    )
    preview = compatibility_preview(agent, "gemini", policy, declared_drops=drops)
    assert preview.unsupported_skills == ("pdf-tools", "skill_01b"), "the card shows the name"
    assert drop_words(drops, preview.skill_names) == (
        "its pdf-tools skill and its skill_01b skill"
    ), "the card's words show the name"


def _zip(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return packed.getvalue()


def _mux_spend(files: dict[str, bytes]) -> int | None:
    """What mux installs of a bundle from the inline budget, or None if it refuses it."""
    from mux.contracts.resources import SkillUpload, SkillUploadFile
    from mux.drivers.gemini.bundles import fits_inline, validate_bundle
    from mux.errors import UnsupportedCapability

    bundle = SkillUpload(files=tuple(SkillUploadFile(path=p, content=b) for p, b in files.items()))
    try:
        validate_bundle(bundle, upload=True)
    except (UnsupportedCapability, ValueError):
        return None
    # An inline bundle mounts every file; an uploaded one, only its SKILL.md.
    return sum(map(len, files.values())) if fits_inline(bundle) else len(files["SKILL.md"])


def test_the_skills_a_gemini_capture_keeps_are_the_ones_its_session_installs() -> None:
    """Capture reads a skill's measured size; mux reads its bytes. Both keep the same
    skills, and each spends the same inline bytes: a kept large skill really uploads."""
    import uuid
    from pathlib import Path

    from daimon.core.turn.session_skills import fitting_skills, measure_archive, skill_spend
    from mux.contracts.ids import SkillRef
    from mux.drivers.gemini.bundles import MAX_BUNDLE_BYTES

    mib, head = 1024 * 1024, b"---\nname: s\ndescription: Fixture.\n---\n"
    style = Path(__file__).parents[4] / "defaults/skills/pymc-artifact-style"
    shapes = {
        "text": {"SKILL.md": head, "ref.md": b"a" * 1_000_000},
        "default": {
            p.relative_to(style).as_posix(): p.read_bytes()
            for p in sorted(style.rglob("*"))
            if p.is_file()
        },
        "binary": {"SKILL.md": head + b"b" * 200_000, "f.ttf": b"\xff" * (3 * mib)},
        "big_manifest": {"SKILL.md": head + b"m" * mib, "f.bin": b"\xff" * mib},
        "binary_manifest": {"SKILL.md": head + b"\xff", "f.bin": b"\xff" * (3 * mib)},
        "over_cap": {"SKILL.md": head, "f.bin": b"\xff" * (29 * mib)},
        "many_files": {"SKILL.md": head, **{f"f{i}.bin": b"\xff" for i in range(256)}},
    }
    kept = {"text", "default", "binary"}
    profile = PROFILES["gemini.inline_reuse"]
    tenant = uuid.uuid4()
    sizes = {(sid, "1"): measure_archive(tenant, sid, "1", _zip(f)) for sid, f in shapes.items()}
    for sid, files in shapes.items():
        spend = skill_spend(profile, sizes[(sid, "1")])
        assert spend == _mux_spend(files), sid
        assert (spend is not None) == (sid in kept), sid
    assert sum(map(len, shapes["default"].values())) > 4 * mib, "the default agent's own"
    # Together: the kept set fits mux's shared inline cap, and the next one would not.
    pins = tuple(SkillRef(id=sid, version="1") for sid in shapes)
    session = fitting_skills(profile, pins, sizes)
    assert {p.id for p in session} == kept
    spent = [_mux_spend(shapes[p.id]) or 0 for p in session]
    assert sum(spent) <= MAX_BUNDLE_BYTES
    crowded = (*pins, SkillRef(id="text2", version="1"))
    sizes[("text2", "1")] = sizes[("text", "1")]
    assert "text2" not in {p.id for p in fitting_skills(profile, crowded, sizes)}
    assert sum(spent) + spent[0] > MAX_BUNDLE_BYTES, "mux would refuse it too"
