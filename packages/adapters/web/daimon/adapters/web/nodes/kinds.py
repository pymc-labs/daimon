"""The node kinds this deployment knows, built from `DAIMON_WEB__NODES__*`."""

from __future__ import annotations

from daimon.adapters.web.nodes.base import ConnectionNode
from daimon.adapters.web.nodes.data_server import DataServerNode
from daimon.core.config import Settings


def build_nodes(settings: Settings) -> tuple[ConnectionNode, ...]:
    """Each configured node, or a `NodeConfigError` for one the web service cannot run.

    Settings leave a node's fields optional, since other processes parse them too.

    A new kind adds its block to `WebNodesSettings` and its node here.
    """
    web = settings.web
    if web is None or web.nodes.data_server is None:
        return ()
    return (DataServerNode.from_settings(web.nodes.data_server, settings),)
