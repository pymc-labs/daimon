"""Scenario: a posted control card reaches the same states, wearing the same
copy, on Discord, Slack and Teams.

A posted control is the one durable record of a request for a private value:
the agent drops a card in the channel, exactly one person can open its form,
and the card is edited in place to say what happened. Discord draws it as a
components-v2 `LayoutView` and Slack as Block Kit; the words and the state
machine behind them come from `daimon.core.posted_controls` and must not
diverge.

Every scenario below runs through both `PlatformDriver`s, and every one of
them crosses the same two process boundaries production does: the card is
minted and posted by the MCP server's own tool implementation, then clicked
and submitted in the bot process. Nothing here builds a `PostedCard` and
asserts on it -- the cards these tests read are the payloads the platform
APIs were actually sent, parsed back by `drivers/cards.py`.

The expected copy is always an independent call into the core renderer with
the same fields, never a string spelled out here: a test that repeats the
copy only proves the copy was repeated.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from daimon.core.continuity.messages import ConfigurationChange, render_change_confirmation
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.posted_controls import WRONG_REQUESTER_MESSAGE, build_posted_card, expired_message
from daimon.core.stores.agent_files import list_agent_files, put_agent_file
from daimon.core.stores.agent_mcp_credentials import list_credentials
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import Platform
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.cards import CapturedCard
from .drivers.protocol import PlatformDriver, parity_account_id

_AGENT_NAME = "research-bot"
_MA_AGENT_ID = "ag_parity_card"
_RESPONDER_NAME = "Daimon"
_KEY = "TOGGL_TOKEN"
_SECRET = "tok-do-not-leak"
_MCP_SERVER = "linear"
_MCP_SERVER_URL = "https://mcp.linear.app/sse"

#: Any timezone-aware expiry: `build_posted_card` renders it into the footer
#: only, and no assertion below reads the footer.
_SOME_EXPIRY = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)


@dataclass(frozen=True)
class _Install:
    """The ids one scenario's install is addressed by, in the platform's own
    vocabulary: guild/thread/user snowflakes on Discord, team/channel/user
    ids on Slack."""

    tenant_id: uuid.UUID
    workspace_id: str
    channel_id: str
    user_id: str
    other_user_id: str


def _ids(driver: PlatformDriver) -> tuple[str, str, str, str]:
    if driver.param_id == "discord":
        return ("800000777", "333000000000000777", "100000000000000042", "100000000000000099")
    if driver.param_id == "teams":
        tenant, requester, interloper = (str(uuid.UUID(int=n)) for n in (0x777, 0x42, 0x99))
        return (tenant, "19:parity-card@thread.tacv2", requester, interloper)
    return ("T_PARITY", "C_PARITY", "U_REQUESTER", "U_INTERLOPER")


async def _install(
    driver: PlatformDriver, session_factory: async_sessionmaker[AsyncSession]
) -> _Install:
    workspace_id, channel_id, user_id, other_user_id = _ids(driver)
    async with session_factory.begin() as session:
        tenant = await make_tenant(
            session,
            platform=cast(Platform, driver.param_id),
            workspace_id=workspace_id,
        )
        # `turn_origins.account_id` is the one account id in this flow with an
        # FK, so the account the drivers act as has to exist before the mint
        # opens an origin -- exactly as it would in a real install.
        await make_account(session, tenant=tenant, id=parity_account_id(tenant.id, user_id))
    return _Install(
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        other_user_id=other_user_id,
    )


def _agent_router(install: _Install) -> MARouter:
    """A router serving the one agent every card in this module is for."""
    agent = ma_agent(id=_MA_AGENT_ID, name=_AGENT_NAME, tenant_id=install.tenant_id)
    router = MARouter()
    router.add_agent_list(agent)
    router.add_agent(agent)
    return router


def _agent_uuid(install: _Install) -> uuid.UUID:
    return derive_agent_uuid(tenant_id=install.tenant_id, ma_agent_id=_MA_AGENT_ID)


def _last(driver: PlatformDriver) -> CapturedCard:
    cards = driver.captured_cards()
    assert cards, "the scenario captured no card at all"
    return cards[-1]


async def _age_out(
    session_factory: async_sessionmaker[AsyncSession], *, token: str, now: datetime
) -> None:
    """Move one request's expiry into the past.

    Nothing in production ever moves an expiry, so the store exposes no
    mutator for it and this is a single statement rather than a store call.
    The alternative -- freezing the clock across a mint, a click and an edit
    that each read `datetime.now` -- would fake the thing under test.
    """
    async with session_factory.begin() as session:
        await session.execute(
            text("UPDATE credential_requests SET expires_at = :expired WHERE token = :token"),
            {"expired": now - timedelta(minutes=5), "token": token},
        )


async def test_posted_card_requested_copy_matches_core_renderer(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The card a real mint posts is the core renderer's own card, verbatim."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)

    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env",
        target=_KEY,
        agent_name=_AGENT_NAME,
    )

    expected = build_posted_card(
        kind="env",
        state="requested",
        agent_name=_AGENT_NAME,
        responder_name=_RESPONDER_NAME,
        target=_KEY,
        requester_platform_user_id=install.user_id,
        expires_at=_SOME_EXPIRY,
        token=token,
    )
    posted = _last(driver)
    assert posted.state == "requested", f"a freshly posted card is requested, got {posted.state!r}"
    assert posted.headline == expected.headline, (
        f"the posted headline must be the renderer's own, got {posted.headline!r}"
    )
    assert posted.facts == expected.facts, (
        f"the posted facts must be the renderer's own, got {posted.facts!r}"
    )
    assert posted.button_labels == tuple(b.label for b in expected.buttons), (
        f"the button must be the renderer's own, got {posted.button_labels!r}"
    )


async def test_expired_click_refuses_and_flips_card_to_expired(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A late click is the only thing that ever notices an expiry, so it both
    answers the clicker and retires the card still advertising the form."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env",
        target=_KEY,
        agent_name=_AGENT_NAME,
    )
    await _age_out(db_session_factory, token=token, now=datetime.now(UTC))

    refusal = await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )

    assert refusal == expired_message(
        kind="env", agent_name=_AGENT_NAME, responder_name=_RESPONDER_NAME, target=_KEY
    ), f"the refusal must say what the expired card now says, got {refusal!r}"
    assert driver.captured_card_states() == ["requested", "expired"], (
        f"the card must retire itself on the late click, got {driver.captured_card_states()}"
    )


async def test_wrong_requester_click_refuses_and_leaves_card_requested(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Anyone else's click is refused privately and changes nothing: editing
    the card would let a bystander retire a form that is still good."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env",
        target=_KEY,
        agent_name=_AGENT_NAME,
    )

    refusal = await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.other_user_id,
        token=token,
    )

    assert refusal == WRONG_REQUESTER_MESSAGE, (
        f"the refusal must be the shared copy, character for character, got {refusal!r}"
    )
    assert driver.captured_card_states() == ["requested"], (
        f"a bystander's click must not touch the card, got {driver.captured_card_states()}"
    )


async def test_applied_edit_sequence_and_copy(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A saved key walks the card through received to applied, and the applied
    headline is the configuration-change renderer's own first line."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env",
        target=_KEY,
        agent_name=_AGENT_NAME,
    )
    await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )

    await driver.submit_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
        value=_SECRET,
    )

    assert driver.captured_card_states() == ["requested", "received", "applied"], (
        f"the card must show the submission before its outcome, got {driver.captured_card_states()}"
    )
    change = ConfigurationChange(
        target_name=_AGENT_NAME, kind="key", detail=_KEY, availability="saved"
    )
    applied = _last(driver)
    assert applied.headline == "✅ " + render_change_confirmation(change).split("\n")[0], (
        f"the applied headline must be the renderer's own line, got {applied.headline!r}"
    )
    assert _SECRET not in "\n".join((applied.headline, *applied.facts)), (
        "no submitted value may ever reach the posted card"
    )
    async with db_session_factory() as session:
        stored = await list_agent_files(
            session, tenant_id=install.tenant_id, agent_id=_agent_uuid(install)
        )
    assert [(row.key, row.content) for row in stored] == [(_KEY, _SECRET)], (
        "sanity: the key the card was posted for is what got written"
    )
    assert stored[0].last_set_by_account_id is not None, (
        "the write must record which account submitted it"
    )


def _mcp_routes(router: MARouter) -> None:
    """Everything the MCP submission touches beyond the agent itself.

    The per-agent vault is served empty so the bootstrap branch runs (the
    vault's display name is derived from the account id the driver acts as,
    which a scenario deliberately does not know), and the agent update --
    the attach the submission owes after the token is stored -- answers 500.
    """

    def _vault(_request: httpx.Request, _match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "vlt_parity",
                "type": "vault",
                "display_name": "daimon-mcp:parity",
                "metadata": None,
                "archived_at": None,
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    def _credential(_request: httpx.Request, _match: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "vcrd_parity",
                "type": "credential",
                "vault_id": "vlt_parity",
                "auth": {"type": "static_bearer", "mcp_server_url": _MCP_SERVER_URL},
            },
        )

    def _empty_list(_request: httpx.Request, _match: Any) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "next_page": None})

    def _attach_fails(_request: httpx.Request, _match: Any) -> httpx.Response:
        return httpx.Response(
            500, json={"type": "error", "error": {"type": "api_error", "message": "upstream"}}
        )

    router.add("GET", r"/v1/vaults", _empty_list)
    router.add("POST", r"/v1/vaults", _vault)
    router.add("GET", r"/v1/vaults/[^/]+/credentials", _empty_list)
    router.add("POST", r"/v1/vaults/[^/]+/credentials", _credential)
    router.add("POST", rf"/v1/agents/{_MA_AGENT_ID}", _attach_fails)


async def test_mcp_attach_failure_publishes_no_token_and_allows_retry(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The attach comes before the agent-wide token is published, so a failed
    attach stores nothing and the card offers the same form for retry on all
    three platforms."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    _mcp_routes(router)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="mcp",
        target=_MCP_SERVER,
        agent_name=_AGENT_NAME,
        mcp_server_url=_MCP_SERVER_URL,
    )
    await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )

    await driver.submit_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
        value=_SECRET,
    )

    retry = _last(driver)
    assert retry.state == "requested", (
        f"a failed attach must offer the same form again, got {retry.state!r}"
    )
    assert retry.button_labels
    assert "Try again" in "\n".join((retry.headline, *retry.facts))
    assert _SECRET not in "\n".join((retry.headline, *retry.facts)), (
        "no submitted token may ever reach the posted card"
    )
    async with db_session_factory() as session:
        credentials = await list_credentials(
            session, tenant_id=install.tenant_id, agent_id=_agent_uuid(install)
        )
        request = await peek_credential_request(session, token=token)
    assert request is not None and request.used_at is None
    assert request.outcome == "write_failed"
    assert credentials == (), "no agent-wide token is published without an attach"


async def test_replacement_precondition_failure_renders_superseded(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A replacement writes only while the value it promised to overwrite is
    still the one on the card. Someone else's write in between wins."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    agent_id = _agent_uuid(install)
    async with db_session_factory.begin() as session:
        await put_agent_file(
            session,
            tenant_id=install.tenant_id,
            agent_id=agent_id,
            key=_KEY,
            content="first-value",
            set_by_account_id=None,
        )

    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env",
        target=_KEY,
        agent_name=_AGENT_NAME,
    )

    # A separate transaction, so Postgres stamps a later `now()` on the row
    # than the one the mint froze onto the request.
    async with db_session_factory.begin() as session:
        await put_agent_file(
            session,
            tenant_id=install.tenant_id,
            agent_id=agent_id,
            key=_KEY,
            content="second-value",
            set_by_account_id=None,
        )

    await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )
    await driver.submit_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
        value="third-value",
    )

    assert _last(driver).state == "superseded", (
        f"a stale replacement must say so, got {driver.captured_card_states()}"
    )
    async with db_session_factory() as session:
        stored = await list_agent_files(session, tenant_id=install.tenant_id, agent_id=agent_id)
    assert [(row.key, row.content) for row in stored] == [(_KEY, "second-value")], (
        "the value in use must survive a replacement that lost the race"
    )


# A Teams dialog takes no `.env` upload (test_teams_deliberate_gaps.py).
@pytest.mark.parametrize("driver", ["discord", "slack"], indirect=True)
async def test_env_file_parse_error_does_not_consume_the_request(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A typo in the uploaded file costs the one click the card is good for
    nothing: the request stays live and the card stays in requested."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env_file",
        target=".env",
        agent_name=_AGENT_NAME,
    )
    await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )

    await driver.submit_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
        file_bytes=b"NOT A KEY LINE\nALPHA=fine\n",
    )

    assert driver.captured_card_states() == ["requested"], (
        f"an unreadable upload must leave the card clickable, got {driver.captured_card_states()}"
    )
    async with db_session_factory() as session:
        row = await peek_credential_request(session, token=token)
        stored = await list_agent_files(
            session, tenant_id=install.tenant_id, agent_id=_agent_uuid(install)
        )
    assert row is not None and row.used_at is None, (
        "the request must still be spendable after a file that could not be read"
    )
    assert stored == [], "a rejected file is rejected whole, not line by line"


# A Teams dialog takes no `.env` upload (test_teams_deliberate_gaps.py).
@pytest.mark.parametrize("driver", ["discord", "slack"], indirect=True)
async def test_env_file_valid_upload_applies_all_keys(
    driver: PlatformDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A file that parses whole is stored whole, and the card is the receipt."""
    install = await _install(driver, db_session_factory)
    router = _agent_router(install)
    token = await driver.post_credential_card(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        kind="env_file",
        target=".env",
        agent_name=_AGENT_NAME,
    )
    await driver.click_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
    )

    await driver.submit_private_input(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=install.tenant_id,
        workspace_id=install.workspace_id,
        channel_id=install.channel_id,
        user_id=install.user_id,
        token=token,
        file_bytes=b"ALPHA=alpha-private\nBETA=beta-private\n",
    )

    states = driver.captured_card_states()
    assert states[0] == "requested" and states[-1] == "applied", (
        f"a whole-file import ends applied, got {states}"
    )
    applied = _last(driver)
    assert "alpha-private" not in "\n".join((applied.headline, *applied.facts)), (
        "no uploaded value may ever reach the posted card"
    )
    async with db_session_factory() as session:
        stored = await list_agent_files(
            session, tenant_id=install.tenant_id, agent_id=_agent_uuid(install)
        )
    assert [(row.key, row.content) for row in stored] == [
        ("ALPHA", "alpha-private"),
        ("BETA", "beta-private"),
    ], "every key the file declared must be stored, by the documented grammar"
