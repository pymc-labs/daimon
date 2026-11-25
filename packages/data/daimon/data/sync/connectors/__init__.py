"""The connectors this release ships; importing the package registers them."""

from __future__ import annotations

from daimon.data.sync.connector import register
from daimon.data.sync.connectors.discord import DiscordConnector
from daimon.data.sync.connectors.postgres import PostgresConnector

register(PostgresConnector())
register(DiscordConnector())
