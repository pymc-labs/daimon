"""Roles, modules and capabilities."""

from __future__ import annotations

import pytest
from daimon.adapters.web.access import (
    Role,
    at_or_above,
    capabilities_for,
    modules_for,
    role_for,
)
from daimon.core.config import WebModuleKey

ALL: tuple[WebModuleKey, ...] = ("chat", "agents", "credits", "admin")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "member"),
        ({"workspace_grant": "owner"}, "owner"),
        ({"workspace_grant": "admin"}, "admin"),
        ({"platform_admin": True}, "admin"),
        ({"group_roles": {"admin"}}, "admin"),
        ({"group_roles": {"member"}}, "member"),
        ({"kind": "visitor"}, "visitor"),
        ({"kind": "visitor", "workspace_grant": "admin"}, "visitor"),
        ({"kind": "visitor", "workspace_grant": "owner"}, "visitor"),
        ({"kind": "visitor", "platform_admin": True}, "visitor"),
        ({"kind": "visitor", "group_roles": {"admin"}}, "visitor"),
        ({"workspace_grant": "owner", "platform_admin": True}, "owner"),
    ],
)
def test_role_is_the_highest_one_held(kwargs: dict[str, object], expected: Role) -> None:
    base: dict[str, object] = {
        "kind": "member",
        "workspace_grant": None,
        "platform_admin": False,
        "group_roles": set(),
    }
    assert role_for(**{**base, **kwargs}) == expected  # pyright: ignore[reportArgumentType]


def test_modules_follow_the_role_and_the_deployment() -> None:
    assert modules_for("owner", ALL) == ["chat", "agents", "credits", "admin"]
    assert modules_for("member", ALL) == ["chat", "agents", "credits"]
    assert modules_for("visitor", ALL) == ["chat"]
    assert modules_for("owner", ("chat", "admin")) == ["chat", "admin"], "off means off"


def test_capabilities_by_role() -> None:
    member = capabilities_for("member", ["chat", "agents", "credits"])
    assert member == {
        "chat.use",
        "agents.view",
        "agents.edit",
        "agents.create",
        "credits.view_own",
    }
    assert capabilities_for("visitor", ["chat"]) == {"chat.use"}
    assert capabilities_for("admin", ALL) >= member | {"people.manage", "credits.manage"}
    assert len(capabilities_for("owner", ALL)) == 9


def test_a_module_that_is_off_takes_its_capabilities_with_it() -> None:
    caps = capabilities_for("owner", ["chat", "admin"])
    assert "agents.edit" not in caps and "credits.manage" not in caps
    assert {"chat.use", "people.manage"} <= caps


@pytest.mark.parametrize(
    ("role", "other", "expected"),
    [
        ("owner", "admin", True),
        ("admin", "admin", True),
        ("admin", "owner", False),
        ("member", "admin", False),
        ("visitor", "visitor", True),
        ("visitor", "member", False),
    ],
)
def test_at_or_above_orders_the_roles(role: Role, other: Role, expected: bool) -> None:
    assert at_or_above(role, other) is expected, f"{role} against {other}"
