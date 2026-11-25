"""The connectors this release ships; importing the package registers them."""

from __future__ import annotations

from daimon.data.sync.connector import register
from daimon.data.sync.connectors.bluedot import BluedotConnector
from daimon.data.sync.connectors.discord import DiscordConnector
from daimon.data.sync.connectors.gcal import GCalConnector
from daimon.data.sync.connectors.gdrive import GDriveConnector
from daimon.data.sync.connectors.github import GitHubConnector
from daimon.data.sync.connectors.github_markdown import GitHubMarkdownConnector
from daimon.data.sync.connectors.linkedin import LinkedInConnector
from daimon.data.sync.connectors.notion import NotionConnector
from daimon.data.sync.connectors.postgres import PostgresConnector
from daimon.data.sync.connectors.slack import SlackConnector

register(PostgresConnector())
register(DiscordConnector())
register(SlackConnector())
register(GDriveConnector())
register(GCalConnector())
register(GitHubConnector())
register(GitHubMarkdownConnector())
register(NotionConnector())
register(BluedotConnector())
register(LinkedInConnector())
