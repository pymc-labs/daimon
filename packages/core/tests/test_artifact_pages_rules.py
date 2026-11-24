"""The pure artifact rules every path (web pages, management API, MCP tools) calls."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from daimon.core.artifact_pages import access, content, documents, events
from daimon.core.artifact_pages.access import PageChange, PageSettings, Viewer
from daimon.core.artifact_pages.model import ArtifactError
from daimon.core.artifact_pages.tickets import Ticket, fernet_of, open_ticket, seal
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.page_events import MAX_EVENT_BYTES
from daimon.core.stores.domain import ArtifactRow, ArtifactUsage, CollectionRule
from pydantic import JsonValue

NOW = datetime(2026, 11, 1, tzinfo=UTC)
OWNER, EDITOR, LISTED, MEMBER = (uuid.UUID(int=i) for i in range(1, 5))
LIMITS = ArtifactPagesSettings()


def _page(**values: object) -> ArtifactRow:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "public_id": "abcdefgh23",
        "slug": "runsheet",
        "title": "Runsheet",
        "agent_id": "ag_1",
        "owner_account_id": OWNER,
        "access": "workspace",
        "people": (LISTED,),
        "editors": (EDITOR,),
        "editors_can_share": True,
        "data": "off",
        "events": False,
        "ask": False,
        "data_scale": 1,
        "link_secret": None,
        "expires_at": None,
        "current_version": 1,
        "size_bytes": 10,
        "data_revision": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ArtifactRow.model_validate(base | values)


_PUBLIC = {"access": "public", "link_secret": "s3cret", "expires_at": NOW + timedelta(days=1)}


@pytest.mark.parametrize(
    ("page", "viewer", "role"),
    [
        ({}, Viewer(account_id=OWNER, kind="member"), "owner"),
        ({}, Viewer(account_id=MEMBER, kind="member", is_admin=True), "admin"),
        ({}, Viewer(account_id=EDITOR, kind="member"), "editor"),
        ({}, Viewer(account_id=MEMBER, kind="member"), "viewer"),
        ({}, Viewer(account_id=MEMBER, kind="visitor"), "none"),
        ({}, Viewer(account_id=LISTED, kind="visitor"), "viewer"),
        ({}, Viewer(), "none"),
        ({"access": "people"}, Viewer(account_id=MEMBER, kind="member"), "none"),
        ({"access": "people"}, Viewer(account_id=LISTED, kind="member"), "viewer"),
        (_PUBLIC, Viewer(link="s3cret"), "viewer"),
        (_PUBLIC, Viewer(link="wrong"), "none"),
        (_PUBLIC, Viewer(account_id=MEMBER, kind="member"), "none"),
        (_PUBLIC, Viewer(account_id=EDITOR, kind="member"), "editor"),
        # A visitor's stored manager role reads as a visitor's.
        ({"access": "people"}, Viewer(account_id=OWNER, kind="visitor"), "viewer"),
        ({"access": "people"}, Viewer(account_id=EDITOR, kind="visitor"), "viewer"),
        (_PUBLIC, Viewer(account_id=EDITOR, kind="visitor"), "none"),
        ({"expires_at": NOW}, Viewer(account_id=OWNER, kind="member"), "none"),
    ],
)
def test_admit_gives_each_viewer_their_role(
    page: dict[str, object], viewer: Viewer, role: access.Role
) -> None:
    assert access.admit(_page(**page), viewer, NOW) == role, f"{page} {viewer}"


@pytest.mark.parametrize(
    ("role", "can_share", "can_delete"),
    [("owner", True, True), ("admin", True, True), ("editor", False, False)],
)
def test_editor_sharing_follows_the_owners_switch(
    role: access.Role, can_share: bool, can_delete: bool
) -> None:
    page = _page(editors_can_share=False)
    assert access.can_share(page, role) is can_share, role
    assert access.can_delete(role) is can_delete, role
    assert access.can_share(_page(), "editor"), "editors share by default"


def test_tools_reach_only_workspace_pages_unless_managing() -> None:
    assert access.can_use_with_tools(_page(), "none"), "workspace page: anyone in it"
    assert not access.can_use_with_tools(_page(access="people"), "viewer"), "listed is not enough"
    assert access.can_use_with_tools(_page(access="people"), "editor"), "editors may"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"data": "write"}, "cannot write"),
        ({"data": "read", "events": True}, "not both"),
        ({"ask": True}, "cannot ask"),
    ],
)
def test_public_pages_refuse_what_anonymous_viewers_must_not_do(
    settings: dict[str, object], message: str
) -> None:
    public = {"access": "public", "expires_at": NOW + timedelta(days=1)}
    with pytest.raises(ArtifactError, match=message):
        access.check_settings(PageSettings.model_validate(public | settings))


def test_an_editor_without_sharing_may_change_capabilities_but_not_who_sees_it() -> None:
    current = PageSettings(editors_can_share=False)
    data_on = access.apply_change(current, PageChange(data="read"), owner_account_id=OWNER)
    access.check_change(current, data_on, role="editor", now=NOW)
    public = access.apply_change(current, PageChange(access="people"), owner_account_id=OWNER)
    with pytest.raises(ArtifactError, match="Ask the owner") as refused:
        access.check_change(current, public, role="editor", now=NOW)
    assert refused.value.code == "forbidden", "a role refusal is forbidden, not invalid"


def test_a_public_page_may_never_expire() -> None:
    access.check_settings(PageSettings(access="public", expires_at=None))


def test_an_editor_without_sharing_may_leave_but_not_remove_others() -> None:
    current = PageSettings(editors=(EDITOR, MEMBER), editors_can_share=False)
    left = access.apply_change(current, PageChange(editors=(MEMBER,)), owner_account_id=OWNER)
    access.check_change(current, left, role="editor", now=NOW, actor=EDITOR)
    others = access.apply_change(current, PageChange(editors=(EDITOR,)), owner_account_id=OWNER)
    with pytest.raises(ArtifactError, match="Ask the owner"):
        access.check_change(current, others, role="editor", now=NOW, actor=EDITOR)


@pytest.mark.parametrize(
    "change",
    [
        {"data": "read"},
        {"events": True},
        {"data_scale": 2},
        {"collection_rules": {"votes": CollectionRule(read="own")}},
    ],
)
def test_data_and_events_are_sharing_while_outsiders_see_the_page(
    change: dict[str, object],
) -> None:
    current = PageSettings(editors_can_share=False)
    new = access.apply_change(current, PageChange.model_validate(change), owner_account_id=OWNER)
    access.check_change(current, new, role="editor", now=NOW, outside=False)
    with pytest.raises(ArtifactError, match="Ask the owner"):
        access.check_change(current, new, role="editor", now=NOW, outside=True)
    access.check_change(current, new, role="owner", now=NOW, outside=True)


def test_only_the_owner_or_an_admin_turns_editor_sharing_off() -> None:
    current = PageSettings()
    off = access.apply_change(current, PageChange(editors_can_share=False), owner_account_id=OWNER)
    with pytest.raises(ArtifactError, match="Ask the owner"):
        access.check_change(current, off, role="editor", now=NOW)
    access.check_change(current, off, role="admin", now=NOW)


def test_collection_rules_bind_viewers_to_their_own_documents() -> None:
    own = CollectionRule(read="own", write="own")
    assert documents.may_read(own, MEMBER, MEMBER), "their own"
    assert not documents.may_read(own, MEMBER, LISTED), "someone else's"
    assert not documents.may_read(own, None, None), "anonymous viewers own nothing"
    documents.check_write(own, exists=False, created_by=None, viewer=MEMBER)
    documents.check_write(own, exists=True, created_by=MEMBER, viewer=MEMBER)
    for exists, created_by, viewer in [(True, MEMBER, LISTED), (False, None, None)]:
        with pytest.raises(ArtifactError) as refused:
            documents.check_write(own, exists=exists, created_by=created_by, viewer=viewer)
        assert refused.value.code == "not_allowed", (exists, viewer)
    with pytest.raises(ArtifactError, match="read-only"):
        documents.check_write(
            CollectionRule(write="none"), exists=False, created_by=None, viewer=MEMBER
        )
    with pytest.raises(ArtifactError):
        access.check_settings(PageSettings(collection_rules={"_events": own}))


def test_apply_change_drops_the_owner_from_editors_and_keeps_unset_fields() -> None:
    current = PageSettings(data="read", expires_at=NOW)
    new = access.apply_change(
        current, PageChange(editors=(OWNER, EDITOR, EDITOR)), owner_account_id=OWNER
    )
    assert new.editors == (EDITOR,), "owner dropped, duplicates merged"
    assert (new.data, new.expires_at) == ("read", NOW), "fields not set stay"
    never = access.apply_change(current, PageChange(expires_at=None), owner_account_id=OWNER)
    assert never.expires_at is None, "an explicit null means never"


@pytest.mark.parametrize(
    ("access_mode", "days", "current", "expected"),
    [
        ("public", None, None, NOW + timedelta(days=30)),
        ("public", None, NOW + timedelta(days=2), NOW + timedelta(days=2)),
        ("workspace", None, None, None),
        ("workspace", 0, NOW, None),
        ("workspace", 7, None, NOW + timedelta(days=7)),
    ],
)
def test_resolve_expiry(
    access_mode: access.ArtifactAccess,
    days: int | None,
    current: datetime | None,
    expected: datetime | None,
) -> None:
    assert access.resolve_expiry(access_mode, days, current=current, now=NOW) == expected


def test_expiry_beyond_ten_years_is_refused() -> None:
    with pytest.raises(ArtifactError):
        access.resolve_expiry("workspace", 3651, current=None, now=NOW)


def test_document_writes_follow_firestore_and_reserve_daimons_collections() -> None:
    merged = documents.apply_write(documents.DocumentWrite(mode="merge", data={"b": 2}), {"a": 1})
    assert merged == {"a": 1, "b": 2}, "merge keeps other fields"
    with pytest.raises(ArtifactError) as missing:
        documents.apply_write(documents.DocumentWrite(mode="update", data={"b": 2}), None)
    assert missing.value.code == "not_found", "update needs a document"
    with pytest.raises(ArtifactError):
        documents.check_page_collection("_events")
    with pytest.raises(ArtifactError):
        documents.check_writable_collection("_events")
    with pytest.raises(ArtifactError):
        documents.measure({"x": "a" * LIMITS.document_bytes}, LIMITS.document_bytes)


@pytest.mark.parametrize("scale", [1, 3])
def test_capacity_scales_with_data_scale(scale: int) -> None:
    full = ArtifactUsage(documents=LIMITS.documents * scale, bytes=0)
    check = documents.check_capacity
    check(full, new_size=10, replaced_size=5, scale=scale, limits=LIMITS)
    with pytest.raises(ArtifactError, match="at most"):
        check(full, new_size=10, replaced_size=None, scale=scale, limits=LIMITS)
    heavy = ArtifactUsage(documents=1, bytes=LIMITS.data_bytes * scale)
    with pytest.raises(ArtifactError, match="limit"):
        check(heavy, new_size=10, replaced_size=None, scale=scale, limits=LIMITS)


def test_build_event_carries_server_facts_and_refuses_oversize() -> None:
    event_id = uuid.uuid4()
    built = events.build_event(
        slug="runsheet",
        name="vote",
        data={"choice": "a"},
        sender="visitor",
        by="Ines",
        visible_outside_workspace=True,
        event_id=event_id,
        now=NOW,
        max_bytes=LIMITS.event_bytes,
    )
    body = json.loads(built.body)
    assert body["id"] == str(event_id) and body["sender"] == "visitor", body
    assert body["page_visible_outside_workspace"] is True, "the turn knows outsiders read it"
    with pytest.raises(ArtifactError) as large:
        events.build_event(
            slug="runsheet",
            name="vote",
            data="x" * LIMITS.event_bytes,
            sender="member",
            by=None,
            visible_outside_workspace=False,
            event_id=event_id,
            now=NOW,
            max_bytes=LIMITS.event_bytes,
        )
    assert large.value.code == "too_large", "events stay within their limit"


def test_a_watch_filter_matches_every_key_to_one_of_its_values() -> None:
    doc: dict[str, JsonValue] = {"event": "vote", "sender": "member", "outside": False, "n": 1}
    where = events.check_where({"event": ["vote", "undo"], "sender": "member", "outside": False})
    assert events.event_matches(where, doc)
    assert events.event_matches(None, doc), "no filter wakes on everything"
    for other in ({"event": "poke"}, {"outside": 0}, {"n": True}, {"missing": "x"}):
        assert not events.event_matches(other, doc), other
    assert (
        events.describe_where(where) == "event is vote or undo; sender is member; outside is false"
    )
    assert events.describe_where(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {f"k{i}": 1 for i in range(9)},
        {"a": []},
        {"a": list(range(21))},
        {"a": {"b": 1}},
        {"": 1},
    ],
)
def test_a_watch_filter_is_bounded_and_scalar(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="where"):
        events.check_where(bad)


def test_a_large_event_reaches_watches_as_a_bounded_preview() -> None:
    data = {"note": "é" * 20_000}
    built = events.build_event(
        slug="runsheet",
        name="vote",
        data=data,
        sender="member",
        by="Ines",
        visible_outside_workspace=False,
        event_id=uuid.uuid4(),
        now=NOW,
        max_bytes=LIMITS.event_bytes,
    )
    assert built.document["data"] == data, "the saved event is whole"
    body = json.loads(built.body)
    assert len(built.body) <= MAX_EVENT_BYTES, "the signed copy fits a watch's bound"
    assert body["data_truncated"] is True and body["data"] is None, body.keys()
    assert json.dumps(data, ensure_ascii=False).startswith(body["data_preview"]), "a prefix"


def test_policies_keep_page_code_sandboxed_and_offline() -> None:
    policy = content.content_policy(ancestor="https://daimon.example")
    assert "connect-src 'none'" in policy and "allow-same-origin" not in policy, policy
    shell = content.shell_policy("n0nce", "https://daimon.example/a/abcdefgh23/_content")
    assert "frame-src https://daimon.example/a/abcdefgh23/_content;" in shell, shell
    assert "frame-ancestors 'self'" in shell and "script-src 'nonce-n0nce'" in shell, shell


@pytest.mark.parametrize(
    "html", ["<html><head><title>x</title></head></html>", "<!doctype html><p>x", "<p>x"]
)
def test_runtime_runs_first_and_boot_values_cannot_close_the_script(html: str) -> None:
    served = content.inject_runtime(html, "/*rt*/", {"title": "</script><script>alert(1)"})
    assert "</script><script>alert(1)" not in served, "a boot value closed the script tag"
    assert served.index("/*rt*/") < served.index("<p>x" if "<p>" in html else "<title>"), served


def test_a_ticket_opens_only_for_its_purpose_within_its_ttl() -> None:
    fernet = fernet_of((Fernet.generate_key().decode(),))
    ticket = Ticket(purpose="content", artifact_id=uuid.uuid4(), version=2)
    token = seal(fernet, ticket, now=NOW)
    ttl = timedelta(hours=12)
    assert open_ticket(fernet, token, purpose="content", ttl=ttl, now=NOW) == ticket
    assert open_ticket(fernet, token, purpose="source", ttl=ttl, now=NOW) is None, "purpose"
    late = NOW + ttl + timedelta(seconds=1)
    assert open_ticket(fernet, token, purpose="content", ttl=ttl, now=late) is None, "expired"
