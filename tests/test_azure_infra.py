"""Security and startup contracts for offline Azure deployment templates."""

from __future__ import annotations

import json
import shutil
import ssl
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
    for source in sorted((ROOT / "deploy/azure").rglob("*.bicep")):
        target = build_dir / f"{source.parent.name}-{source.stem}.json"
        subprocess.run(
            [bicep, "build", str(source), "--outfile", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        template = json.loads(target.read_text())
        if isinstance(template.get("resources"), dict):
            template["resources"] = list(template["resources"].values())
        result[str(source.relative_to(ROOT / "deploy/azure"))] = template
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
    assert vault["properties"]["enablePurgeProtection"] == "[parameters('vaultPurgeProtection')]"
    assert "enabledForTemplateDeployment" not in vault["properties"]
    account = next(
        r
        for r in templates["modules/ai.bicep"]["resources"]
        if r["type"] == "Microsoft.CognitiveServices/accounts"
    )
    assert account["kind"] == "AIServices"
    assert account["properties"]["disableLocalAuth"] is True
    assert account["properties"]["allowProjectManagement"] is True


def test_postgres_tls_loads_system_ca_without_a_home_certificate(
    templates: dict[str, dict], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Exercise asyncpg's actual TLS setup without opening a socket. verify-full
    # alone would try ~/.postgresql/root.crt and fail in the container.
    from asyncpg.connect_utils import _parse_connect_arguments
    from sqlalchemy.ext.asyncio import create_async_engine

    main = templates["main.bicep"]
    for name in ("init", "apps"):
        resource = next(
            r for r in main["resources"] if r.get("copy", {}).get("name", r["name"]) == name
        )
        env = resource["properties"]["parameters"]["environment"]["value"]
        assert "'PGSSLROOTCERT', '/etc/ssl/certs/ca-certificates.crt'" in env
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PGSSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt")
    engine = create_async_engine(
        "postgresql+asyncpg://daimon:synthetic@localhost:5432/daimon?ssl=verify-full"
    )
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    assert not args
    assert "sslrootcert" not in kwargs
    _, connection, _ = _parse_connect_arguments(
        dsn=None,
        **kwargs,
        passfile=None,
        command_timeout=None,
        statement_cache_size=100,
        max_cached_statement_lifetime=300,
        max_cacheable_statement_size=15360,
        direct_tls=False,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
        service=None,
        servicefile=None,
    )
    assert isinstance(connection.ssl, ssl.SSLContext)
    assert connection.ssl.verify_mode == ssl.CERT_REQUIRED
    assert connection.ssl.check_hostname is True
    assert connection.ssl.cert_store_stats()["x509_ca"] > 0


def test_mcp_receives_selected_platform_secrets_and_teams_settings(
    templates: dict[str, dict],
) -> None:
    main = templates["main.bicep"]
    variables = main["variables"]
    assert "DAIMON_DISCORD__BOT_TOKEN" in variables["discordSecrets"]
    assert "DAIMON_TEAMS__CLIENT_SECRET" in variables["teamsSecrets"]
    mcp = variables["defaultServiceSecrets"]["mcp"]
    assert (
        "if(contains(parameters('services'), 'discord'), variables('discordSecrets'), createObject())"
        in mcp
    )
    assert (
        "if(contains(parameters('services'), 'teams'), variables('teamsSecrets'), createObject())"
        in mcp
    )
    app = next(r for r in main["resources"] if r.get("copy", {}).get("name") == "apps")
    params = app["properties"]["parameters"]
    assert "variables('defaultServiceSecrets')" in params["secretNames"]["value"]
    environment = params["environment"]["value"]
    assert "'mcp'" in environment
    assert "contains(parameters('services'), 'teams')" in environment
    assert "tryGet(parameters('serviceEnvironment'), 'teams')" in environment
    assert (
        "contains(parameters('services'), variables('serviceDefinitions')[copyIndex()].name)"
        in app["condition"]
    )


def test_workers_have_http_liveness_probes(templates: dict[str, dict]) -> None:
    app = templates["modules/app.bicep"]["resources"][0]
    probes = app["properties"]["template"]["containers"][0]["probes"]
    assert "'Liveness'" in probes
    assert "'discord', 'scheduler'" in probes
    assert "if(equals(parameters('serviceName'), 'discord'), 8081, 8082)" in probes
    assert "'httpGet'" in probes


def test_ai_is_optional_and_vault_protection_is_opt_in(templates: dict[str, dict]) -> None:
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        template = templates[entrypoint]
        assert template["parameters"]["deployAi"]["defaultValue"] is True
        assert template["parameters"]["vaultPurgeProtection"]["defaultValue"] is False
        assert template["parameters"]["services"]["defaultValue"] == [
            "discord",
            "teams",
            "mcp",
            "scheduler",
        ]
        ai_outputs = {
            k: v
            for k, v in template["outputs"].items()
            if k
            in (
                "harnessEnvironment",
                "foundryProjectId",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_AI_PROJECT_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
            )
        }
        assert "if(parameters('deployAi')" in json.dumps(ai_outputs)
    main = templates["main.bicep"]
    ai = next(r for r in main["resources"] if r["name"] == "ai")
    assert ai["condition"] == "[parameters('deployAi')]"


@pytest.mark.parametrize(
    "prefix, valid",
    [
        ("abc", True),
        ("abc12345", True),
        ("a01", True),
        ("0abc", False),
        ("1ab", False),
        ("123", False),
        ("9abcdefg", False),
        ("", False),
        ("ABC", False),
        ("a-b", False),
        ("a_b", False),
        ("ab", False),
        ("abcdefghi", False),
        ("aé1", False),
    ],
)
def test_name_prefix_validation_runs_offline(
    templates: dict[str, dict], tmp_path: Path, prefix: str, valid: bool
) -> None:
    import os

    # build-params evaluates the production predicate locally. fail() itself
    # requires ARM; check below that both entrypoints use its guarded wrapper.
    source = tmp_path / "names.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure/names.bicep", tmp_path)
    source.write_text(
        f"using none\nimport {{ isValidNamePrefix }} from '{relative}'\n"
        f"param valid = isValidNamePrefix('{prefix}')\n"
    )
    result = subprocess.run(
        ["bicep", "build-params", str(source), "--outfile", str(tmp_path / "names.json")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((tmp_path / "names.json").read_text())["parameters"]["valid"]["value"] is valid
    )
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        template = templates[entrypoint]
        assert (
            template["variables"]["checkedNamePrefix"]
            == "[__bicep.validateNamePrefix(parameters('namePrefix'))]"
        )
        validator = next(
            f
            for group in template["functions"]
            for name, f in group["members"].items()
            if name == "validateNamePrefix"
        )
        assert (
            "fail('namePrefix must be 3-8 lowercase alphanumeric characters starting with a lowercase letter')"
            in validator["output"]["value"]
        )
