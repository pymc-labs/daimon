"""Security and startup contracts for offline Azure deployment templates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    bicep = shutil.which("bicep")
    if bicep is None:
        pytest.skip("Standalone Bicep CLI required; installed by azure-infra CI")
    build_dir = tmp_path_factory.mktemp("azure-templates")
    result = {}
    for source in sorted((ROOT / "infra/azure").rglob("*.bicep")):
        target = build_dir / f"{source.parent.name}-{source.stem}.json"
        subprocess.run(
            [bicep, "build", str(source), "--outfile", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        result[str(source.relative_to(ROOT / "infra/azure"))] = json.loads(target.read_text())
    return result


def test_password_is_required_secure_input_and_never_an_output(templates: dict[str, dict]) -> None:
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        template = templates[entrypoint]
        password = template["parameters"]["postgresAdministratorPassword"]
        assert password["type"] == "securestring"
        assert "defaultValue" not in password
        outputs = json.dumps(template["outputs"])
        assert "postgresAdministratorPassword" not in outputs
    database = templates["modules/database.bicep"]
    assert database["parameters"]["administratorPassword"]["type"] == "securestring"
    secret = next(
        r for r in database["resources"] if r["type"] == "Microsoft.KeyVault/vaults/secrets"
    )
    assert "administratorPassword" in secret["properties"]["value"]
    assert "ssl=verify-full" in secret["properties"]["value"]
    assert list(database["outputs"]) == ["host"]


def test_runtime_secrets_are_key_vault_references(templates: dict[str, dict]) -> None:
    for module in ("modules/app.bicep", "modules/init.bicep"):
        resource = templates[module]["resources"][0]
        assert resource["identity"]["type"] == "UserAssigned"
        configuration = resource["properties"]["configuration"]
        secrets = next(c for c in configuration["copy"] if c["name"] == "secrets")
        assert set(secrets["input"]) == {"name", "keyVaultUrl", "identity"}
        assert configuration["registries"][0]["identity"] == "[parameters('identityId')]"
        assert "passwordSecretRef" not in configuration["registries"][0]


def test_init_is_manual_and_serving_is_opt_in(templates: dict[str, dict]) -> None:
    main = templates["main.bicep"]
    assert main["parameters"]["deployServices"]["defaultValue"] is False
    assert main["parameters"]["containerImage"]["defaultValue"] == ""
    assert main["parameters"]["enablePrivateNetworking"]["allowedValues"] == [False]
    init = templates["modules/init.bicep"]["resources"][0]
    config = init["properties"]["configuration"]
    assert config["triggerType"] == "Manual"
    assert config["replicaRetryLimit"] == 0
    command = init["properties"]["template"]["containers"][0]["command"]
    assert command == ["sh", "-c", "alembic upgrade head && daimon defaults apply"]
    app = templates["modules/app.bicep"]["resources"][0]
    assert app["properties"]["template"]["scale"] == {"minReplicas": 1, "maxReplicas": 1}


def test_storage_and_ai_refuse_shared_keys(templates: dict[str, dict]) -> None:
    foundation = templates["modules/foundation.bicep"]["resources"]
    storage = next(r for r in foundation if r["type"] == "Microsoft.Storage/storageAccounts")
    assert storage["properties"]["allowBlobPublicAccess"] is False
    assert storage["properties"]["allowSharedKeyAccess"] is False
    vault = next(r for r in foundation if r["type"] == "Microsoft.KeyVault/vaults")
    assert vault["properties"]["enableRbacAuthorization"] is True
    assert vault["properties"]["enablePurgeProtection"] is True
    account = next(
        r
        for r in templates["modules/ai.bicep"]["resources"]
        if r["type"] == "Microsoft.CognitiveServices/accounts"
    )
    assert account["kind"] == "AIServices"
    assert account["properties"]["disableLocalAuth"] is True
    assert account["properties"]["allowProjectManagement"] is True
