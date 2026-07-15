"""Tests for Slack gating pure decision functions."""

from __future__ import annotations

from daimon.adapters.slack.gating import (
    is_app_mention,
    is_external_interactive,
    is_slack_connect_external,
)


class TestIsExternalInteractive:
    def test_returns_true_when_actor_home_team_differs_from_host(self) -> None:
        payload = {"user": {"id": "U1", "team_id": "T_EXTERNAL"}, "team": {"id": "T_HOST"}}
        assert is_external_interactive(payload) is True, (
            "a block action whose actor's home workspace differs from the host "
            "must be flagged external (Slack Connect)"
        )

    def test_returns_false_when_actor_is_in_host_workspace(self) -> None:
        payload = {"user": {"id": "U1", "team_id": "T_HOST"}, "team": {"id": "T_HOST"}}
        assert is_external_interactive(payload) is False, (
            "same-workspace actor must not be flagged external"
        )

    def test_returns_false_when_team_fields_absent(self) -> None:
        assert is_external_interactive({"user": {"id": "U1"}, "team": {}}) is False, (
            "absent team fields must not spuriously flag external (fail-open to "
            "normal handling; other guards still apply)"
        )


class TestIsAppMention:
    def test_returns_true_when_event_type_is_app_mention(self) -> None:
        assert is_app_mention({"type": "app_mention", "text": "<@U1> hello"}) is True, (
            "is_app_mention should return True for app_mention events"
        )

    def test_returns_false_when_event_type_is_message(self) -> None:
        assert is_app_mention({"type": "message", "text": "hello"}) is False, (
            "is_app_mention should return False for non-app_mention events"
        )

    def test_returns_false_when_event_has_no_type(self) -> None:
        assert is_app_mention({}) is False, (
            "is_app_mention should return False when event has no type field"
        )


class TestIsSlackConnectExternal:
    def test_returns_true_when_user_team_differs_from_team_id(self) -> None:
        assert is_slack_connect_external({"user_team": "T2"}, team_id="T1") is True, (
            "is_slack_connect_external should return True when user_team != team_id"
        )

    def test_returns_false_when_user_team_id_matches_team_id(self) -> None:
        assert is_slack_connect_external({"user_team_id": "T1"}, team_id="T1") is False, (
            "is_slack_connect_external should return False when user_team_id == team_id"
        )

    def test_returns_true_when_source_team_differs_from_team_id(self) -> None:
        assert is_slack_connect_external({"source_team": "T9"}, team_id="T1") is True, (
            "is_slack_connect_external should return True when source_team != team_id"
        )

    def test_returns_false_when_no_slack_connect_field_present(self) -> None:
        assert is_slack_connect_external({}, team_id="T1") is False, (
            "is_slack_connect_external should return False when no home-team field is present"
        )


async def test_fake_slack_web_client_intercepts_registered_method(
    fake_slack_web_client,
) -> None:
    """Smoke: aioresponses intercepts auth.test on the real AsyncWebClient."""
    resp = await fake_slack_web_client.client.auth_test()
    assert resp["ok"] is True, (
        "transport-level fake should intercept auth.test and return canned ok=True"
    )
