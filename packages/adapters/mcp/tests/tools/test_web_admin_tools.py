"""Web admin tools: the admin gate, the core call, and that no link or token reaches a result."""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.mcp.tools import _web_admin as shared
from daimon.adapters.mcp.tools import allowances as allowance_tool
from daimon.adapters.mcp.tools import audit_log as audit_tool
from daimon.adapters.mcp.tools import web_invites as invite_tool
from daimon.adapters.mcp.tools import web_people as people_tool
from daimon.adapters.mcp.tools import web_workspace_settings as settings_tool
from daimon.core.config import WebSettings
from daimon.core.stores.domain import SpendAllowanceRow, WebInviteRow
from daimon.core.web_admin import WebAdminError
from fastmcp.exceptions import ToolError

from .parity_fakes import fake_runtime, make_auth

TOKEN = "s3cret-token"
LINK = f"https://web.test/invite/{TOKEN}"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def runtime(monkeypatch):
    async def no_grant(*_a: Any, **_k: Any):
        return None

    monkeypatch.setattr(shared.web_roles, "get_workspace_role", no_grant)

    async def agents(*_a: Any, **_k: Any):
        return {"a1": "Ada"}

    for module in (invite_tool, settings_tool, allowance_tool):
        monkeypatch.setattr(module, "agent_names", agents)
    rt = fake_runtime()
    rt.settings = SimpleNamespace(web=WebSettings(public_url="https://web.test"))  # type: ignore[misc,call-arg]
    return rt


def _invite(**over: Any) -> WebInviteRow:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        kind="invite",
        email="v@x.test",
        role="visitor",
        credit_usd=Decimal(0),
        allow_files=False,
        person_id=None,
        created_by_account_id=None,
        created_at=NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        accepted_at=None,
        accepted_by_person_id=None,
        revoked_at=None,
        agent_ids=("a1",),
    )
    return WebInviteRow(**{**base, **over})


def _allowance(**over: Any) -> SpendAllowanceRow:
    base = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        subject_kind="group",
        subject_id="everyone",
        agent_id=None,
        amount_usd=Decimal(5),
        window="month",
        starts_at=NOW,
        expires_at=None,
        created_by_account_id=None,
        created_at=NOW,
        is_cap=False,
    )
    return SpendAllowanceRow(**{**base, **over})


async def test_every_admin_tool_when_member_then_refused(runtime):
    member = make_auth(admin=False)
    calls = [
        invite_tool._list_impl(runtime, member),
        invite_tool._create_impl(
            runtime,
            member,
            email="a@b.test",
            role="member",
            agent_ids=[],
            credit_usd=Decimal(0),
            days=7,
            allow_files=False,
        ),
        invite_tool._files_impl(runtime, member, str(uuid.uuid4()), True),
        invite_tool._revoke_impl(runtime, member, str(uuid.uuid4())),
        invite_tool._issue_link_impl(runtime, member, str(uuid.uuid4())),
        invite_tool._revoke_link_impl(runtime, member, str(uuid.uuid4())),
        people_tool._people_impl(runtime, member, None, 10),
        people_tool._person_impl(runtime, member, str(uuid.uuid4())),
        people_tool._groups_impl(runtime, member),
        people_tool._update_impl(runtime, member, str(uuid.uuid4()), "admin", None),
        people_tool._access_list_impl(runtime, member),
        people_tool._access_set_impl(runtime, member, "a1", True, None, None),
        allowance_tool._list_impl(runtime, member),
        allowance_tool._set_impl(runtime, member, str(uuid.uuid4()), Decimal(1)),
        allowance_tool._delete_impl(runtime, member, str(uuid.uuid4())),
        audit_tool._kinds_impl(runtime, member),
    ]
    for call in calls:
        with pytest.raises(ToolError):
            await call
    with pytest.raises(ToolError):
        await settings_tool._change_impl(
            runtime,
            member,
            modules=None,
            default_agent=None,
            everyone_invokes=True,
            invoker_people=[],
            dm_enabled=None,
            dm_memory_read_only=None,
        )


async def test_create_invite_when_admin_then_link_goes_by_dm_not_in_result(runtime, monkeypatch):
    sent: list[str] = []
    invite = _invite()

    async def fake_create(_db, _admin, _now, **_kw):
        return invite, TOKEN

    async def fake_send(_rt, _auth, text):
        sent.append(text)

    monkeypatch.setattr(invite_tool.web_admin, "create_invite", fake_create)
    monkeypatch.setattr(invite_tool, "send_privately", fake_send)
    out = await invite_tool._create_impl(
        runtime,
        make_auth(),
        email="v@x.test",
        role="visitor",
        agent_ids=["a1"],
        credit_usd=Decimal(0),
        days=7,
        allow_files=False,
    )
    assert out.delivered == "direct_message", "the result names where the link went"
    assert sent == [f"Invite link for v@x.test: {LINK}"], "the admin gets the link by DM"
    assert TOKEN not in repr(out), "the token must never be in the result"
    assert out.web_url is not None and out.web_url.endswith(f"/admin/invites?invite={invite.id}")


async def test_create_invite_when_dm_fails_then_revokes_and_refuses(runtime, monkeypatch):
    revoked: list[uuid.UUID] = []
    invite = _invite()

    async def fake_create(_db, _admin, _now, **_kw):
        return invite, TOKEN

    async def fake_revoke(_db, _admin, _now, invite_id):
        revoked.append(invite_id)

    async def failing(*_a: Any):
        raise ToolError("no dm")

    monkeypatch.setattr(invite_tool.web_admin, "create_invite", fake_create)
    monkeypatch.setattr(invite_tool.web_admin, "revoke_invite", fake_revoke)
    monkeypatch.setattr(invite_tool, "send_privately", failing)
    with pytest.raises(ToolError) as err:
        await invite_tool._create_impl(
            runtime,
            make_auth(),
            email="v@x.test",
            role="visitor",
            agent_ids=["a1"],
            credit_usd=Decimal(0),
            days=7,
            allow_files=False,
        )
    assert revoked == [invite.id], "an undelivered invite must not stay live"
    assert TOKEN not in str(err.value), "the error must not carry the token"


async def test_issue_visitor_link_when_core_refuses_then_tool_error(runtime, monkeypatch):
    async def refuse(*_a: Any, **_k: Any):
        raise WebAdminError("You can't sign in as someone at your own role.", status=403)

    monkeypatch.setattr(invite_tool.web_admin, "issue_sign_in_link", refuse)
    with pytest.raises(ToolError, match="own role"):
        await invite_tool._issue_link_impl(runtime, make_auth(), str(uuid.uuid4()))


async def test_issue_visitor_link_when_admin_then_dm_only(runtime, monkeypatch):
    sent: list[str] = []

    async def fake_issue(*_a: Any, **_k: Any):
        return SimpleNamespace(), TOKEN

    async def fake_send(_rt, _auth, text):
        sent.append(text)

    monkeypatch.setattr(invite_tool.web_admin, "issue_sign_in_link", fake_issue)
    monkeypatch.setattr(invite_tool, "send_privately", fake_send)
    out = await invite_tool._issue_link_impl(runtime, make_auth(), str(uuid.uuid4()))
    assert out.delivered == "direct_message", "the result names where the link went"
    assert sent == [f"Sign-in link: {LINK}"], "the link goes to the admin's DMs"
    assert TOKEN not in repr(dataclasses.asdict(out)), "the token must never be in the result"


async def test_update_person_when_admin_then_passes_role_to_core(runtime, monkeypatch):
    seen: dict[str, Any] = {}
    person = SimpleNamespace(
        id=uuid.uuid4(), display_name="P", email=None, status="active", last_seen_at=NOW
    )

    async def fake_change(_db, admin, _now, person_id, **kw):
        seen.update(kw, role_of_admin=admin.role)
        return person

    async def no_identities(*_a: Any, **_k: Any):
        return []

    async def role(*_a: Any, **_k: Any):
        return "admin"

    monkeypatch.setattr(people_tool.web_admin, "change_person", fake_change)
    monkeypatch.setattr(people_tool.identities_store, "list_for_person", no_identities)
    monkeypatch.setattr(people_tool, "role_of", role)
    out = await people_tool._update_impl(runtime, make_auth(), str(uuid.uuid4()), "admin", None)
    assert seen == {"role": "admin", "status": None, "role_of_admin": "admin"}, (
        "core gets the role asked for and the caller's web role"
    )
    assert out.role == "admin", "the result shows the person's role after the change"
    assert out.web_url is not None and f"?person={person.id}" in out.web_url, "their page"


async def test_update_person_when_bad_id_then_tool_error(runtime):
    with pytest.raises(ToolError):
        await people_tool._update_impl(runtime, make_auth(), "nope", "admin", None)


async def test_change_settings_when_admin_then_reports_changes(runtime, monkeypatch):
    async def fake_change(_factory, _admin, _now, **kw):
        assert kw["everyone_invokes"] is True, "the flag reaches core"
        return [("Who can use the agents", "Everyone")]

    monkeypatch.setattr(settings_tool.web_admin, "change_settings", fake_change)
    out = await settings_tool._change_impl(
        runtime,
        make_auth(),
        modules=None,
        default_agent=None,
        everyone_invokes=True,
        invoker_people=[],
        dm_enabled=None,
        dm_memory_read_only=None,
    )
    assert [(c.setting, c.value) for c in out.changes] == [("Who can use the agents", "Everyone")]


async def test_create_allowance_when_agent_not_in_workspace_then_refused(runtime):
    with pytest.raises(ToolError, match="agent"):
        await allowance_tool._create_impl(
            runtime,
            make_auth(),
            subject_kind="group",
            subject="everyone",
            amount_usd=Decimal(5),
            window="month",
            agent_id="other",
            expires_at=None,
            cap=False,
        )


async def test_create_allowance_when_amount_not_positive_then_refused(runtime):
    with pytest.raises(ToolError, match="amount_usd"):
        await allowance_tool._create_impl(
            runtime,
            make_auth(),
            subject_kind="group",
            subject="everyone",
            amount_usd=Decimal(0),
            window="month",
            agent_id=None,
            expires_at=None,
            cap=False,
        )


async def test_create_allowance_when_admin_then_core_row_is_returned(runtime, monkeypatch):
    row = _allowance()

    async def fake_create(_db, _admin, _now, **kw):
        assert kw["subject_kind"] == "group", "the subject kind reaches core"
        return row

    async def fake_labels(*_a: Any, **_k: Any):
        return {}

    monkeypatch.setattr(allowance_tool.web_allowances, "create_allowance", fake_create)
    monkeypatch.setattr(allowance_tool.credit_views, "label_people", fake_labels)
    out = await allowance_tool._create_impl(
        runtime,
        make_auth(),
        subject_kind="group",
        subject="everyone",
        amount_usd=Decimal(5),
        window="month",
        agent_id=None,
        expires_at=None,
        cap=False,
    )
    assert out.allowance.subject_name == "Everyone", "the group reads as Everyone"
    assert out.allowance.amount_usd == 5.0, "the amount is the one stored"
    assert out.web_url is not None and out.web_url.endswith(f"?allowance={row.id}"), "its page"


async def test_delete_allowance_when_gone_then_tool_error(runtime, monkeypatch):
    async def gone(*_a: Any, **_k: Any):
        raise WebAdminError("This allowance no longer exists.", status=404)

    monkeypatch.setattr(allowance_tool.web_allowances, "delete_allowance", gone)
    with pytest.raises(ToolError, match="no longer exists"):
        await allowance_tool._delete_impl(runtime, make_auth(), str(uuid.uuid4()))


async def test_update_person_when_no_web_app_then_no_link(runtime, monkeypatch):
    runtime.settings = SimpleNamespace(web=None)
    person = SimpleNamespace(
        id=uuid.uuid4(), display_name="P", email=None, status="active", last_seen_at=NOW
    )

    async def fake_change(*_a: Any, **_k: Any):
        return person

    async def no_identities(*_a: Any, **_k: Any):
        return []

    async def role(*_a: Any, **_k: Any):
        return "admin"

    monkeypatch.setattr(people_tool.web_admin, "change_person", fake_change)
    monkeypatch.setattr(people_tool.identities_store, "list_for_person", no_identities)
    monkeypatch.setattr(people_tool, "role_of", role)
    out = await people_tool._update_impl(runtime, make_auth(), str(uuid.uuid4()), "admin", None)
    assert out.web_url is None, "no web app, no link"


async def test_audit_log_when_bad_date_then_tool_error(runtime):
    with pytest.raises(ToolError, match="first"):
        await audit_tool._log_impl(
            runtime,
            make_auth(),
            source=None,
            person_id=None,
            kind=None,
            first="yesterday",
            last=None,
            limit=10,
        )
