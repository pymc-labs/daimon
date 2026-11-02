"""Executable record of how the web chat differs from a platform DM.

A web chat reaches only the person, like a DM, but it is in no workspace channel and its
sign-in is not a platform's live admin signal. So it gets no admin exemption from agent
rules, and an agent kept to channels never answers there. A visitor (client) may chat,
held to the web place.
"""

from __future__ import annotations

from daimon.core import authz
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize

PINNED = TenantAccessPolicy(agent_channel_pins={"acme": ("C_ACME",)})
ADMIN = Subject(is_admin=True, platform_user_id="U_ADM")


def test_web_has_no_admin_pin_exemption() -> None:
    assert Surface.WEB not in authz._ADMIN_ONLY_SURFACES, (  # pyright: ignore[reportPrivateUsage]
        "a web sign-in is not a platform's live admin signal: no exemption from agent rules"
    )


def test_visitors_may_chat_on_the_web() -> None:
    assert Surface.WEB not in authz._CLIENT_REFUSED_SURFACES, (  # pyright: ignore[reportPrivateUsage]
        "a visitor's chat is held to the web place, not refused"
    )


def test_pinned_agent_answers_an_admin_dm_but_not_an_admin_web_chat() -> None:
    def run(surface: Surface, place: Place) -> bool:
        return bool(
            authorize(
                PINNED,
                subject=ADMIN,
                action=Action.RUN_AGENT,
                surface=surface,
                place=place,
                agent=AgentRef.of("acme"),
            )
        )

    assert run(Surface.DM, Place()), "a DM keeps the admin exemption"
    assert not run(Surface.WEB, Place(channel_id="web:t1", parent_channel_id="web")), (
        "the web chat refuses a channel-bound agent for everyone"
    )
