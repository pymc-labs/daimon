"""A ceiling or a limit narrows below the tier above, like a guardrail: a write that
resolution would override is refused, and every feature write can be listed back."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    ComputeSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    GeminiSettings,
    MicrosoftAccountsSettings,
    NotebookSettings,
    RepoMemorySettings,
    ReportHostSettings,
    Settings,
)
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import (
    FeatureActor,
    change_feature,
    change_refusal,
)
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.channel_changes import list_changes
from daimon.testing.factories import make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


async def _admin(_: object) -> bool:
    return True


def _history_keys() -> list[str]:
    """Every feature set at a channel or a thread. Approvals alone is left out: its writes
    need a live native admin check, which `test_approval_gate` covers."""
    return [
        key
        for key, spec in FEATURES.items()
        if ("channel" in spec.tiers or "thread" in spec.tiers) and not spec.authority.native
    ]


def _integrated_settings() -> Settings:
    """`_settings()` with every integration set up, so those features are offered."""
    base = _settings()
    return base.model_copy(
        update={
            "compute": ComputeSettings(
                enabled=True,
                deployment_id="d",
                api_token=SecretStr("s"),
                org_slug="o",
                session_auth_key=SecretStr("s"),
            ),
            "microsoft_accounts": MicrosoftAccountsSettings(
                client_id="app",
                client_secret=SecretStr("s"),
                tenant_id="11111111-1111-1111-1111-111111111111",
            ),
            "crypto": CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),)),
            "mcp": base.mcp.model_copy(update={"public_url": HttpUrl("https://mcp.example")}),
            "notebook": NotebookSettings(
                host_url=HttpUrl("http://notebook-host:8001"), admin_secret=SecretStr("s")
            ),
            "report_host": ReportHostSettings(
                host_url=HttpUrl("http://report-host:8002"), admin_secret=SecretStr("s")
            ),
            "gemini": GeminiSettings(api_key=SecretStr("s")),
            "repo_memory": RepoMemorySettings(enabled=True),
        }
    )


async def test_a_channel_cannot_turn_on_a_capability_the_workspace_turned_off(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1")
    await change_feature(
        db_session_factory, settings, actor, key="watches", tier="workspace", value="off",
        place=place,
    )  # fmt: skip
    early = await change_refusal(
        db_session_factory, settings, actor, key="watches", tier="channel", place=place,
        value="on",
    )  # fmt: skip
    assert early is not None and early.reason == "looser_than_above", "refused before a card"
    assert "can't turn it on" in str(early) and "Ask" in str(early), "names who can help"
    with pytest.raises(ChannelChangeRefused, match="can't turn it on"):
        await change_feature(
            db_session_factory, settings, actor, key="watches", tier="channel", value="on",
            place=place,
        )  # fmt: skip
    await change_feature(
        db_session_factory, settings, actor, key="watches", tier="channel", value="off",
        place=place,
    )  # fmt: skip
    found = await feature_in(db_session, settings, place, "watches")
    assert (found.value, found.tier) == ("off", "workspace"), "Off below Off is allowed"


async def test_a_channel_cannot_raise_a_limit_above_the_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1")
    await change_feature(
        db_session_factory, settings, actor, key="history_depth", tier="workspace", value="25",
        place=place,
    )  # fmt: skip
    with pytest.raises(ChannelChangeRefused, match="only choose that or lower") as raised:
        await change_feature(
            db_session_factory, settings, actor, key="history_depth", tier="channel",
            value="100", place=place,
        )  # fmt: skip
    assert raised.value.reason == "looser_than_above"
    await change_feature(
        db_session_factory, settings, actor, key="history_depth", tier="channel", value="10",
        place=place,
    )  # fmt: skip
    found = await feature_in(db_session, settings, place, "history_depth")
    assert (found.value, found.tier) == ("10", "channel"), "a lower step is allowed"


@pytest.mark.parametrize("key", _history_keys())
async def test_the_channel_history_lists_every_feature_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    key: str,
) -> None:
    spec = FEATURES[key]
    tier = "thread" if "thread" in spec.tiers else "channel"
    # A lower level may only narrow: a guardrail takes its strictest value, the rest Off or
    # any non-default value.
    values = list(spec.values)
    value = values[-1] if spec.effect == "guardrail" else spec.off or values[-1]
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1", thread_id="T1")
    await change_feature(
        db_session_factory,
        _integrated_settings(),
        actor,
        key=key,
        tier=tier,
        value=value,
        place=place,  # pyright: ignore[reportArgumentType]
    )
    async with db_session_factory() as session:
        changes = await list_changes(session, tenant_id=tenant.id, channel_id="C1")
    # Two thread features keep their older history names; every other write lists under its key.
    expected = {"participation": "thread_participation", "progress_plans": "thread_progress_plans"}
    expected = expected.get(key, key) if tier == "thread" else key
    assert [change.setting for change in changes] == [expected], "the write lists back"
