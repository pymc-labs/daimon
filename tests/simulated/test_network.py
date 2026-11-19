"""The simulated network refuses what is not loopback, whichever client reaches for it."""

from __future__ import annotations

import socket

import pytest
from daimon.testing.sim.network import ProviderNetwork


def test_a_raw_socket_to_the_internet_is_refused_and_fails_the_world_on_exit() -> None:
    network = ProviderNetwork({})
    with pytest.raises(AssertionError, match="unexpected traffic"), network.installed():
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("203.0.113.7", 443), timeout=1)
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect_ex(("203.0.113.7", 443))
    assert network.refused == ["connect 203.0.113.7 (socket)"] * 2


def test_loopback_and_unix_paths_still_connect() -> None:
    with ProviderNetwork({}).installed(), socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", 1)) != 0  # refused by the machine, not by us
