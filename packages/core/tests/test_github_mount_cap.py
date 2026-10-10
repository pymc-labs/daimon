"""A session mounts only the agent's working repo (MA provisioning failed with 75)."""

from daimon.core.github_app_session import _mounted_repo_ids


def test_only_the_working_repo_is_mounted() -> None:
    assert _mounted_repo_ids(list(range(1, 76)), working_ids={42}) == {42}


def test_nothing_is_mounted_without_a_working_repo() -> None:
    assert _mounted_repo_ids(list(range(1, 76)), working_ids=set()) == set()
