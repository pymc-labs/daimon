"""The registration template uses the published Teams v1.16 field names."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
# Top-level properties from the versioned Microsoft schema (offline test):
# https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json
V116_PROPERTIES = {
    "$schema",
    "manifestVersion",
    "version",
    "id",
    "packageName",
    "localizationInfo",
    "developer",
    "name",
    "description",
    "icons",
    "accentColor",
    "configurableTabs",
    "staticTabs",
    "bots",
    "connectors",
    "subscriptionOffer",
    "composeExtensions",
    "permissions",
    "devicePermissions",
    "validDomains",
    "webApplicationInfo",
    "graphConnector",
    "showLoadingIndicator",
    "isFullScreen",
    "activities",
    "configurableProperties",
    "supportedChannelTypes",
    "defaultBlockUntilAdminAction",
    "publisherDocsUrl",
    "defaultInstallScope",
    "defaultGroupCapability",
    "meetingExtensionDefinition",
    "authorization",
}


def test_manifest_uses_v116_schema_keys() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    assert set(manifest) <= V116_PROPERTIES
    assert manifest["manifestVersion"] == "1.16"
    assert {"version", "id", "developer", "name", "description", "icons", "accentColor"} <= set(
        manifest
    )
    assert {"short", "full"} <= set(manifest["name"])
