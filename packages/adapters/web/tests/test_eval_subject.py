"""Who web callers are to evals: the same key as on MCP, and visitors from other orgs."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.evals import eval_subject
from daimon.core.stores.domain import Role
from daimon.core.turn_hold import RequesterOrg
from daimon.core.web_identity import WebPrincipal


def _caller(person_id: uuid.UUID) -> WebCaller:
    person = SimpleNamespace(id=person_id, tenant_id=uuid.uuid4())
    return cast(WebCaller, SimpleNamespace(person=person, identities=[], role="member"))


def test_eval_subject_when_chat_visitor_from_another_org_then_visitor_keyed_like_mcp() -> None:
    person_id = uuid.uuid4()
    host = WebPrincipal(
        platform="discord", external_id="200", role=Role.USER, org=RequesterOrg.HOST, home_id=None
    )
    member = eval_subject(_caller(person_id), host)
    assert (member.key, member.role) == ("200", "member"), "the platform id owns, as on MCP"
    assert f"web:{person_id}" in member.ids, "suites made under the old web key stay theirs"
    other = WebPrincipal(
        platform="discord", external_id="300", role=Role.USER, org=RequesterOrg.OTHER, home_id=None
    )
    assert eval_subject(_caller(person_id), other).role == "visitor", "another org is a visitor"
