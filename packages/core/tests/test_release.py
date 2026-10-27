"""The release stamp is the image's commit when baked in, else the package version."""

from collections.abc import Iterator
from importlib.metadata import version

import pytest
from daimon.core.config import load_settings
from daimon.core.release import current_release


@pytest.fixture(autouse=True)
def fresh_release() -> Iterator[None]:
    current_release.cache_clear()
    yield
    current_release.cache_clear()


def test_baked_release_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_RELEASE", "0123456789abcdef0123456789abcdef01234567")
    assert current_release() == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("value", [None, "", "  "])
def test_package_version_when_nothing_is_baked(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DAIMON_RELEASE", raising=False)
    else:
        monkeypatch.setenv("DAIMON_RELEASE", value)
    assert current_release() == version("daimon-core")


def test_settings_ignore_the_release_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DAIMON_` is the settings prefix; the stamp must not be read as a setting."""
    monkeypatch.setenv("DAIMON_RELEASE", "0123456789abcdef0123456789abcdef01234567")
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert not hasattr(settings, "release")


def test_release_is_read_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_RELEASE", "first")
    assert current_release() == "first"
    monkeypatch.setenv("DAIMON_RELEASE", "second")
    assert current_release() == "first"
    current_release.cache_clear()
    assert current_release() == "second"
