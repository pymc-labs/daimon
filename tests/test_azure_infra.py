"""Security and startup contracts for offline Azure deployment templates."""

from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
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


def test_blob_delegation_is_account_scoped_and_data_access_is_charts_only(
    templates: dict[str, dict],
) -> None:
    foundation = templates["modules/foundation.bicep"]
    roles = [
        r
        for r in foundation["resources"]
        if r["type"] == "Microsoft.Authorization/roleAssignments"
        and "Microsoft.Storage/storageAccounts" in r["scope"]
    ]
    expected_scopes = {
        "db58b8e5-c6ad-4a2a-8342-4190687cbf4a": (
            "[resourceId('Microsoft.Storage/storageAccounts', "
            "format('{0}s', parameters('compactName')))]"
        ),
        "ba92f5b4-2d11-453d-a403-e96b0029c9fe": (
            "[resourceId('Microsoft.Storage/storageAccounts/blobServices/containers', "
            "format('{0}s', parameters('compactName')), 'default', 'charts')]"
        ),
    }
    assert len(roles) == len(expected_scopes), "no additional account-wide Blob data roles"
    for role_id, scope in expected_scopes.items():
        matches = [r for r in roles if role_id in r["properties"]["roleDefinitionId"]]
        assert len(matches) == 1
        role = matches[0]
        assert role["scope"] == scope
        assert role["condition"] == "[equals(parameters('chartStorage'), 'azure_blob')]"
        assert (
            role["properties"]["principalId"]
            == foundation["outputs"]["identityPrincipalId"]["value"]
        )
        assert role["properties"]["principalType"] == "ServicePrincipal"


def test_only_mcp_receives_blob_settings_from_foundation_outputs(
    templates: dict[str, dict],
) -> None:
    main = templates["main.bicep"]
    artifacts = main["outputs"]["mcpArtifactsEnvironment"]["value"]
    assert artifacts == (
        "[__bicep.chartArtifactsEnvironment(variables('checkedChartStorage'), "
        "reference('foundation').outputs.blobEndpoint.value, "
        "reference('foundation').outputs.chartsContainerName.value, "
        "reference('foundation').outputs.identityClientId.value)]"
    )
    foundation = templates["modules/foundation.bicep"]
    assert foundation["outputs"]["chartsContainerName"]["value"] == "charts"
    assert ".primaryEndpoints.blob" in foundation["outputs"]["blobEndpoint"]["value"]
    assert "DAIMON_ARTIFACTS__" not in json.dumps(main["outputs"]["daimonEnvironment"])
    app = next(r for r in main["resources"] if r.get("copy", {}).get("name") == "apps")
    environment = app["properties"]["parameters"]["environment"]["value"]
    assert (
        "if(equals(variables('serviceDefinitions')[copyIndex()].name, 'mcp'), "
        "__bicep.chartArtifactsEnvironment(variables('checkedChartStorage')"
    ) in environment
    assert artifacts.strip("[]") in environment
    init = next(r for r in main["resources"] if r["name"] == "init")
    assert "DAIMON_ARTIFACTS__" not in init["properties"]["parameters"]["environment"]["value"]
    assert (
        templates["azd/main.bicep"]["outputs"]["DAIMON_MCP_ARTIFACTS_ENVIRONMENT"]["value"]
        == "[reference('stack').outputs.mcpArtifactsEnvironment.value]"
    )


def test_explicit_artifact_backend_overrides_blob_defaults(templates: dict[str, dict]) -> None:
    main = templates["main.bicep"]
    app = next(r for r in main["resources"] if r.get("copy", {}).get("name") == "apps")
    environment = app["properties"]["parameters"]["environment"]["value"]
    # ARM union gives the last object precedence. An explicit backend=s3 in
    # appEnvironment must survive even when serviceEnvironment.mcp is empty;
    # per-service overrides must win over both shared settings and defaults.
    assert environment.startswith(
        "[union(if(equals(variables('serviceDefinitions')[copyIndex()].name, 'mcp'), "
        "__bicep.chartArtifactsEnvironment(variables('checkedChartStorage')"
    )
    assert environment.index("parameters('appEnvironment')") > environment.index(
        "__bicep.chartArtifactsEnvironment("
    )
    assert "parameters('appEnvironment')), if(" in environment
    assert environment.endswith(
        "coalesce(tryGet(parameters('serviceEnvironment'), "
        "variables('serviceDefinitions')[copyIndex()].name), createObject()))]"
    )


@pytest.mark.parametrize("mode", ["azure_blob", "external"])
@pytest.mark.parametrize(
    ("scope", "key"),
    [
        ("shared", "DAIMON_ARTIFACTS__ACCESS_KEY_ID"),
        ("shared", "DAIMON_ARTIFACTS__SECRET_ACCESS_KEY"),
        ("mcp", "DAIMON_ARTIFACTS__ACCESS_KEY_ID"),
        ("mcp", "DAIMON_ARTIFACTS__SECRET_ACCESS_KEY"),
        ("shared", "DAIMON_ANTHROPIC__API_KEY"),
        ("mcp", "DAIMON_ANTHROPIC__API_KEY"),
        ("none", ""),
    ],
)
def test_chart_storage_modes_and_implicit_s3_configuration(
    templates: dict[str, dict], tmp_path: Path, mode: str, scope: str, key: str
) -> None:
    # Evaluate the production functions offline. This S3 configuration omits
    # BACKEND, as existing installations rely on the application's s3 default.
    shared = {key: "existing-secret"} if scope == "shared" else {}
    mcp = {key: "existing-secret"} if scope == "mcp" else {}
    source = tmp_path / "charts.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure/chart-storage.bicep", tmp_path)
    source.write_text(
        f"using none\nimport {{ isValidChartStorage, chartArtifactsEnvironment }} from '{relative}'\n"
        f"param valid = isValidChartStorage('{mode}', "
        f"json('{json.dumps(shared)}'), json('{json.dumps(mcp)}'))\n"
        f"param defaults = chartArtifactsEnvironment('{mode}', "
        "'https://storage.blob.core.windows.net/', 'charts', 'identity')\n"
        "param environment = union(defaults, {\n"
        "  DAIMON_ARTIFACTS__ENDPOINT_URL: 'https://s3.example.test'\n"
        "  DAIMON_ARTIFACTS__BUCKET: 'existing-charts'\n"
        "})\n"
    )
    target = tmp_path / "charts.json"
    result = subprocess.run(
        ["bicep", "build-params", str(source), "--outfile", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    parameters = json.loads(target.read_text())["parameters"]
    expected_valid = mode == "external" or not key.startswith("DAIMON_ARTIFACTS__")
    assert parameters["valid"]["value"] is expected_valid
    defaults = parameters["defaults"]["value"]
    if mode == "external":
        assert defaults == {}
        assert "DAIMON_ARTIFACTS__BACKEND" not in parameters["environment"]["value"]
        from daimon.core.config import ArtifactsSettings

        settings = {
            name.removeprefix("DAIMON_ARTIFACTS__").lower(): value
            for name, value in parameters["environment"]["value"].items()
        }
        settings.update(access_key_id="synthetic", secret_access_key="synthetic")
        assert ArtifactsSettings.model_validate(settings).backend == "s3"
    else:
        assert defaults == {
            "DAIMON_ARTIFACTS__BACKEND": "azure_blob",
            "DAIMON_ARTIFACTS__ENDPOINT_URL": "https://storage.blob.core.windows.net/",
            "DAIMON_ARTIFACTS__BUCKET": "charts",
            "DAIMON_ARTIFACTS__AZURE_CLIENT_ID": "identity",
        }


def test_chart_storage_guard_controls_defaults_and_role_assignments(
    templates: dict[str, dict],
) -> None:
    for entrypoint, module_name in (("main.bicep", "foundation"), ("azd/main.bicep", "daimon")):
        template = templates[entrypoint]
        assert template["parameters"]["chartStorage"]["defaultValue"] == "azure_blob"
        assert template["parameters"]["chartStorage"]["allowedValues"] == ["azure_blob", "external"]
        assert template["variables"]["checkedChartStorage"] == (
            "[__bicep.validateChartStorage(parameters('chartStorage'), parameters('secretNames'), "
            "coalesce(tryGet(parameters('serviceSecretNames'), 'mcp'), createObject()))]"
        )
        module = next(r for r in template["resources"] if r["name"] == module_name)
        assert module["properties"]["parameters"]["chartStorage"]["value"] == (
            "[variables('checkedChartStorage')]"
        )
        validator = next(
            f
            for group in template["functions"]
            for name, f in group["members"].items()
            if name == "validateChartStorage"
        )
        assert "isValidChartStorage(" in validator["output"]["value"]
        assert (
            "fail('S3 artifact secret mappings are incompatible with chartStorage=azure_blob; "
            "set chartStorage=''external''.')"
        ) in validator["output"]["value"]


def test_model_parameters_flow_through_azd_and_ai(templates: dict[str, dict]) -> None:
    names = ("modelName", "modelVersion", "modelDeploymentName", "modelSku", "modelCapacity")
    for entrypoint, module_name in (("main.bicep", "ai"), ("azd/main.bicep", "daimon")):
        template = templates[entrypoint]
        params = template["parameters"]
        assert not any(name.startswith("gpt5") for name in params)
        assert params["modelName"]["defaultValue"] == "gpt-6.1-sol"
        assert params["modelVersion"]["defaultValue"] == (
            "[__bicep.defaultModelVersion(parameters('modelName'))]"
        )
        assert params["modelDeploymentName"]["defaultValue"] == "[parameters('modelName')]"
        assert params["modelSku"]["defaultValue"] == "DataZoneStandard"
        assert params["modelSku"]["allowedValues"] == ["DataZoneStandard", "GlobalStandard"]
        assert params["modelCapacity"]["defaultValue"] == 1
        assert params["modelCapacity"]["minValue"] == 1
        module = next(r for r in template["resources"] if r["name"] == module_name)
        for name in names:
            expected = (
                "[variables('checkedModelDeploymentName')]"
                if name == "modelDeploymentName"
                else f"[parameters('{name}')]"
            )
            assert module["properties"]["parameters"][name]["value"] == expected
    deployment = next(
        r
        for r in templates["modules/ai.bicep"]["resources"]
        if r["type"] == "Microsoft.CognitiveServices/accounts/deployments"
    )
    assert deployment["properties"]["model"] == {
        "format": "OpenAI",
        "name": "[parameters('modelName')]",
        "version": "[parameters('modelVersion')]",
    }
    assert "parameters('modelDeploymentName')" in deployment["name"]
    assert deployment["sku"] == {
        "name": "[parameters('modelSku')]",
        "capacity": "[parameters('modelCapacity')]",
    }
    assert (
        templates["main.bicep"]["outputs"]["harnessEnvironment"]["value"]["AZURE_OPENAI_DEPLOYMENT"]
        == "[if(parameters('deployAi'), variables('checkedModelDeploymentName'), '')]"
    )


def test_bicep_production_model_defaults_agree(templates: dict[str, dict]) -> None:
    from mux.profiles import backend_selection

    for entrypoint in ("main.bicep", "azd/main.bicep"):
        assert templates[entrypoint]["parameters"]["modelName"]["defaultValue"] == (
            backend_selection("selfhosted").model
        )


@pytest.mark.parametrize(
    "model,version",
    [("gpt-6.1-sol", "2026-09-29"), ("gpt-6-luna", "2026-09-22"), ("custom-model", "")],
)
def test_default_model_versions_evaluate_offline(templates, tmp_path, model, version):
    source = tmp_path / "models.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure/models.bicep", tmp_path)
    source.write_text(
        f"using none\nimport {{ knownModelVersion }} from '{relative}'\n"
        f"param version = knownModelVersion('{model}')\n"
    )
    target = tmp_path / "models.json"
    result = subprocess.run(
        ["bicep", "build-params", str(source), "--outfile", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text())["parameters"]["version"]["value"] == version


def test_azd_model_name_uses_env_with_sol_default():
    parameters = json.loads((ROOT / "deploy/azure/azd/main.parameters.json").read_text())
    assert parameters["parameters"]["modelName"]["value"] == "${AZURE_MODEL_NAME=gpt-6.1-sol}"
    # Leave the version unset so the production mapping follows a Luna selection.
    assert "modelVersion" not in parameters["parameters"]


@pytest.mark.parametrize(
    "model,deployment,valid",
    [
        ("gpt-6.1-sol", "gpt-6-luna", False),
        ("gpt-6.1-sol", "GPT-6-Luna", False),
        ("gpt-6-luna", "GPT-6.1-SOL", False),
        ("GPT-6.1-SOL", "gpt-6-luna", False),
        ("gpt-6-luna", "GPT-6-Luna", True),
        ("gpt-6-luna", "gpt-6.1-sol", False),
        ("another-model", "gpt-6-luna", False),
        ("gpt-6-luna", "gpt-6-luna", True),
        ("gpt-6.1-sol", "gpt-6.1-sol", True),
        ("gpt-6-luna", "custom-smoke-alias", True),
    ],
)
def test_model_alias_guard_offline(templates, tmp_path, model, deployment, valid):
    source = tmp_path / "alias.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure/models.bicep", tmp_path)
    source.write_text(
        f"using none\nimport {{ isValidModelDeploymentName }} from '{relative}'\n"
        f"param valid = isValidModelDeploymentName('{model}', '{deployment}')\n"
    )
    target = tmp_path / "alias.json"
    result = subprocess.run(
        ["bicep", "build-params", str(source), "--outfile", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text())["parameters"]["valid"]["value"] is valid
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        template = templates[entrypoint]
        assert template["variables"]["checkedModelDeploymentName"] == (
            "[__bicep.validateModelDeploymentName(parameters('modelName'), "
            "parameters('modelDeploymentName'))]"
        )
        validator = next(
            definition
            for group in template["functions"]
            for name, definition in group["members"].items()
            if name == "validateModelDeploymentName"
        )
        assert "isValidModelDeploymentName(" in validator["output"]["value"]
        assert "fail('modelDeploymentName is a reserved model name" in validator["output"]["value"]


@pytest.mark.parametrize("entrypoint", ["main.bicep", "azd/main.bicep"])
@pytest.mark.parametrize(
    "model,deployment,error",
    [
        ("gpt-6.1-sol", "gpt-6-luna", "reserved model name and must equal modelName"),
        ("gpt-6.1-sol", "GPT-6-Luna", "reserved model name and must equal modelName"),
        ("gpt-6-luna", "GPT-6.1-SOL", "reserved model name and must equal modelName"),
        ("gpt-6-luna", "custom-smoke-alias", None),
        ("unknown-model", "custom-smoke-alias", "No default version for modelName"),
    ],
)
def test_model_guards_fire_in_deployment_snapshots(
    templates, tmp_path, entrypoint, model, deployment, error
):
    # The local deployment engine evaluates fail(), unlike build-params. All
    # identities/passwords are synthetic; snapshot performs no Azure deployment.
    source = tmp_path / "guard.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure" / entrypoint, tmp_path)
    source.write_text(
        f"using '{relative}'\n"
        "param location = 'eastus2'\n"
        "param postgresAdministratorPassword = 'SnapshotOnlyPassword123'\n"
        f"param modelName = '{model}'\n"
        f"param modelDeploymentName = '{deployment}'\n"
        + ("param environmentName = 'offline'\n" if entrypoint.startswith("azd/") else "")
    )
    result = subprocess.run(
        [
            "bicep",
            "snapshot",
            str(source),
            "--mode",
            "overwrite",
            "--tenant-id",
            "00000000-0000-0000-0000-000000000001",
            "--subscription-id",
            "00000000-0000-0000-0000-000000000002",
            "--location",
            "eastus2",
        ]
        + ([] if entrypoint.startswith("azd/") else ["--resource-group", "rg-offline-test"]),
        capture_output=True,
        text=True,
        timeout=30,
    )
    diagnostics = result.stdout + result.stderr
    if error is None:
        assert result.returncode == 0, diagnostics
    else:
        assert result.returncode != 0, (
            "Invalid model configuration unexpectedly deployed in snapshot"
        )
        assert error in diagnostics


TEAMS_IDS = (
    "{ DAIMON_TEAMS__CLIENT_ID: '00000000-0000-4000-8000-000000000001', "
    "DAIMON_TEAMS__TENANT_ID: '00000000-0000-4000-8000-000000000002' }"
)


def _snapshot(tmp_path: Path, *params: str) -> tuple[int, str, dict]:
    """Evaluate main.bicep with the local deployment engine; no Azure call."""
    source = tmp_path / "snapshot.bicepparam"
    relative = os.path.relpath(ROOT / "deploy/azure/main.bicep", tmp_path)
    source.write_text(
        f"using '{relative}'\n"
        "param location = 'eastus2'\n"
        "param postgresAdministratorPassword = 'SnapshotOnlyPassword123'\n"
        + "".join(f"param {line}\n" for line in params)
    )
    result = subprocess.run(
        ["bicep", "snapshot", str(source), "--mode", "overwrite"]
        + ["--tenant-id", "00000000-0000-0000-0000-000000000001"]
        + ["--subscription-id", "00000000-0000-0000-0000-000000000002"]
        + ["--location", "eastus2", "--resource-group", "rg-offline-test"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    snapshot = tmp_path / "snapshot.snapshot.json"
    data = json.loads(snapshot.read_text()) if result.returncode == 0 else {}
    return result.returncode, result.stdout + result.stderr, data


def _bot_resources(snapshot: dict) -> dict[str, dict]:
    return {
        r["type"].rsplit("/", 1)[-1]: r
        for r in snapshot["predictedResources"]
        if r["type"].startswith("Microsoft.BotService/")
    }


def test_bot_module_is_single_tenant_with_an_opt_in_teams_channel(
    templates: dict[str, dict],
) -> None:
    bot = templates["modules/bot.bicep"]
    resources = {r["type"]: r for r in bot["resources"]}
    assert set(resources) == {
        "Microsoft.BotService/botServices",
        "Microsoft.BotService/botServices/channels",
    }
    service = resources["Microsoft.BotService/botServices"]
    assert service["location"] == "global"
    assert service["properties"]["msaAppType"] == "SingleTenant"
    channel = resources["Microsoft.BotService/botServices/channels"]
    assert channel["condition"] == "[parameters('teamsChannelTermsAccepted')]"
    settings = channel["properties"]["properties"]
    assert settings["deploymentEnvironment"] == "CommercialDeployment"
    assert settings["enableCalling"] is False
    assert bot["parameters"]["teamsChannelTermsAccepted"]["defaultValue"] is False
    assert "/api/messages" in bot["variables"]["endpoint"]
    assert not any("secret" in name.lower() for name in bot["parameters"]), "no credential"


def test_bot_identity_requests_no_permissions_and_makes_no_secret(
    templates: dict[str, dict],
) -> None:
    identity = templates["teams/bot-identity.bicep"]
    resources = identity["resources"]
    application = next(r for r in resources if r["type"].startswith("Microsoft.Graph/applications"))
    properties = application["properties"]
    assert properties["signInAudience"] == "AzureADMyOrg"
    assert properties["requiredResourceAccess"] == []
    assert properties["web"]["redirectUris"] == []
    assert properties["identifierUris"] == []
    assert "passwordCredentials" not in json.dumps(identity)
    assert set(identity["outputs"]) == {"clientId", "tenantId"}


def test_teams_bot_is_off_by_default_and_follows_the_teams_app(
    templates: dict[str, dict], tmp_path: Path
) -> None:
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        params = templates[entrypoint]["parameters"]
        assert params["deployTeamsBot"]["defaultValue"] is False
        assert params["teamsChannelTermsAccepted"]["defaultValue"] is False
    code, diagnostics, default = _snapshot(tmp_path)
    assert code == 0, diagnostics
    assert _bot_resources(default) == {}, "nothing changes for current installs"

    code, diagnostics, snapshot = _snapshot(
        tmp_path, "deployTeamsBot = true", f"serviceEnvironment = {{ teams: {TEAMS_IDS} }}"
    )
    assert code == 0, diagnostics
    bots = _bot_resources(snapshot)
    assert set(bots) == {"botServices"}, "no Teams channel until its terms are accepted"
    properties = bots["botServices"]["properties"]
    assert properties["msaAppId"] == "00000000-0000-4000-8000-000000000001"
    assert properties["msaAppTenantId"] == "00000000-0000-4000-8000-000000000002"
    endpoint = properties["endpoint"]
    assert "https://" in endpoint and "-teams." in endpoint and "/api/messages" in endpoint, (
        "points at the Teams app's own hostname"
    )

    code, diagnostics, snapshot = _snapshot(
        tmp_path,
        "deployTeamsBot = true",
        "teamsChannelTermsAccepted = true",
        "teamsBaseUrl = 'https://teams.example.com'",
        "teamsBotDisplayName = 'Example analyst'",
        f"serviceEnvironment = {{ teams: {TEAMS_IDS} }}",
    )
    assert code == 0, diagnostics
    bots = _bot_resources(snapshot)
    assert set(bots) == {"botServices", "channels"}
    assert bots["botServices"]["properties"]["endpoint"] == "https://teams.example.com/api/messages"
    assert bots["botServices"]["properties"]["displayName"] == "Example analyst"
    assert snapshot["outputs"]["teamsMessagingEndpoint"] == (
        "https://teams.example.com/api/messages"
    )


@pytest.mark.parametrize(
    ("params", "error"),
    [
        (("deployTeamsBot = true",), "DAIMON_TEAMS__CLIENT_ID"),
        (
            (
                "deployTeamsBot = true",
                "serviceEnvironment = { teams: "
                "{ DAIMON_TEAMS__CLIENT_ID: '00000000-0000-4000-8000-000000000001' } }",
            ),
            "DAIMON_TEAMS__TENANT_ID",
        ),
        (
            (
                "deployTeamsBot = true",
                "serviceEnvironment = { teams: { DAIMON_TEAMS__CLIENT_ID: '', "
                "DAIMON_TEAMS__TENANT_ID: '00000000-0000-4000-8000-000000000002' } }",
            ),
            "DAIMON_TEAMS__CLIENT_ID",
        ),
        (
            (
                "deployTeamsBot = true",
                "services = ['discord', 'mcp', 'scheduler']",
                f"serviceEnvironment = {{ teams: {TEAMS_IDS} }}",
            ),
            "needs teams in services",
        ),
        (
            (
                "deployTeamsBot = true",
                "teamsBaseUrl = 'https://teams.example.com/'",
                f"serviceEnvironment = {{ teams: {TEAMS_IDS} }}",
            ),
            "no trailing slash",
        ),
    ],
)
def test_teams_bot_refuses_incomplete_settings(
    templates: dict[str, dict], tmp_path: Path, params: tuple[str, ...], error: str
) -> None:
    code, diagnostics, _ = _snapshot(tmp_path, *params)
    assert code != 0, "an incomplete Teams bot configuration deployed in snapshot"
    assert error in diagnostics


def _fake_az(tmp_path: Path, script: str) -> dict[str, str]:
    """An `az` on PATH that logs its arguments and runs the given Python body."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    az = bin_dir / "az"
    az.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "open(os.environ['AZ_LOG'], 'a').write(json.dumps(args) + '\\n')\n" + script
    )
    az.chmod(0o755)
    log = tmp_path / "az.log"
    log.touch()
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "AZ_LOG": str(log)}


def _az_calls(env: dict[str, str]) -> list[list[str]]:
    return [json.loads(line) for line in Path(env["AZ_LOG"]).read_text().splitlines()]


def _run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/azure" / script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.fixture
def outside_git(tmp_path: Path) -> Path:
    if any((parent / ".git").exists() for parent in (tmp_path, *tmp_path.parents)):
        pytest.skip("the temporary directory is inside a git checkout")
    return tmp_path


BOT_CLIENT_ID = "00000000-0000-4000-8000-000000000001"
BOT_TENANT_ID = "00000000-0000-4000-8000-000000000002"
GRAPH_REPLY = (
    "if args[:2] == ['account', 'show']:\n"
    f"    print(os.environ.get('AZ_TENANT', '{BOT_TENANT_ID}'))\n"
    "else:\n"
    "    print(json.dumps({'secretText': 'one-time-secret-value', 'keyId': 'k1',"
    " 'displayName': 'x', 'endDateTime': '2030-01-01T00:00:00Z'}))\n"
)


def test_bot_credential_plans_by_default_and_saves_the_secret_privately(
    outside_git: Path,
) -> None:
    env = _fake_az(outside_git, GRAPH_REPLY)
    ids = ["--client-id", BOT_CLIENT_ID, "--tenant-id", BOT_TENANT_ID]
    state = outside_git / "credential"
    plan = _run("create_bot_credential.py", env, *ids, "--state", str(state))
    assert plan.returncode == 0, plan.stderr
    assert _az_calls(env) == [] and not state.exists(), "the plan makes no Azure call"

    created = _run("create_bot_credential.py", env, *ids, "--state", str(state), "--create")
    assert created.returncode == 0, created.stderr
    assert "one-time-secret-value" not in created.stdout + created.stderr
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert sorted(p.name for p in state.iterdir()) == [
        "client-secret.txt",
        "credential-metadata.json",
        "request.json",
    ]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in state.iterdir())
    assert (state / "client-secret.txt").read_text() == "one-time-secret-value"
    [_, graph] = _az_calls(env)
    assert graph[:4] == ["rest", "--method", "POST", "--uri"]
    assert f"applications(appId='{BOT_CLIENT_ID}')/addPassword" in graph[4]

    again = _run("create_bot_credential.py", env, *ids, "--state", str(state), "--create")
    assert again.returncode != 0 and "must not exist" in again.stderr

    bad = ["--client-id", "not-a-guid", "--tenant-id", BOT_TENANT_ID]
    refused = _run("create_bot_credential.py", env, *bad, "--state", str(outside_git / "x"))
    assert refused.returncode != 0 and "must be GUIDs" in refused.stderr


def test_bot_credential_refuses_another_tenant(outside_git: Path) -> None:
    env = _fake_az(outside_git, GRAPH_REPLY)
    env["AZ_TENANT"] = "00000000-0000-4000-8000-000000000009"
    state = outside_git / "credential"
    result = _run(
        "create_bot_credential.py",
        env,
        *["--client-id", BOT_CLIENT_ID, "--tenant-id", BOT_TENANT_ID],
        *["--state", str(state), "--create"],
    )
    assert result.returncode != 0 and "another tenant" in result.stderr
    assert [call[0] for call in _az_calls(env)] == ["account"], "Graph is never called"
    assert not state.exists()


VAULT_REPLY = (
    "if args[:3] == ['keyvault', 'secret', 'show']:\n"
    "    name = args[args.index('--name') + 1]\n"
    "    if name in os.environ.get('AZ_EXISTING', '').split(','):\n"
    "        print('{}')\n"
    "    else:\n"
    "        sys.stderr.write('(SecretNotFound) not found')\n"
    "        sys.exit(3)\n"
    "if args[:3] == ['keyvault', 'secret', 'show-deleted']:\n"
    "    name = args[args.index('--name') + 1]\n"
    "    if name not in os.environ.get('AZ_DELETED', '').split(','):\n"
    "        sys.stderr.write('(SecretNotFound) not found')\n"
    "        sys.exit(3)\n"
)


def test_bootstrap_generates_new_keys_and_sets_them_once(outside_git: Path) -> None:
    env = _fake_az(outside_git, VAULT_REPLY)
    bot_secret = outside_git / "client-secret.txt"
    bot_secret.write_text("bot-secret\n")
    bot_secret.chmod(0o600)
    state = outside_git / "secrets"
    result = _run(
        "bootstrap_secrets.py",
        env,
        *["--state", str(state), "--teams-client-secret-file", str(bot_secret)],
        *["--vault", "example-vault"],
    )
    assert result.returncode == 0, result.stderr
    names = ["crypto-keys", "mcp-jwt-secret", "teams-client-secret"]
    assert sorted(p.name for p in state.iterdir()) == names
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in state.iterdir())
    assert len(base64.urlsafe_b64decode((state / "crypto-keys").read_text())) == 32
    assert len((state / "mcp-jwt-secret").read_text()) == 64
    assert (state / "teams-client-secret").read_text() == "bot-secret"
    sets = [call for call in _az_calls(env) if call[:3] == ["keyvault", "secret", "set"]]
    assert sorted(call[call.index("--name") + 1] for call in sets) == names
    for call in sets:
        assert call[call.index("--file") + 1].startswith(str(state)), "values never on argv"


def test_bootstrap_never_overwrites_an_existing_secret(outside_git: Path) -> None:
    env = _fake_az(outside_git, VAULT_REPLY)
    env["AZ_EXISTING"] = "crypto-keys"
    result = _run("bootstrap_secrets.py", env, "--state", str(outside_git / "s"), "--vault", "v")
    assert result.returncode != 0 and "already exists" in result.stderr
    assert not [call for call in _az_calls(env) if "set" in call], "nothing set"


def test_bootstrap_refuses_a_deleted_but_recoverable_secret(outside_git: Path) -> None:
    env = _fake_az(outside_git, VAULT_REPLY)
    env["AZ_DELETED"] = "mcp-jwt-secret"
    result = _run("bootstrap_secrets.py", env, "--state", str(outside_git / "s"), "--vault", "v")
    assert result.returncode != 0 and "recover or purge" in result.stderr
    assert not [call for call in _az_calls(env) if "set" in call], "nothing set"


def test_bootstrap_without_a_vault_makes_no_azure_call(outside_git: Path) -> None:
    env = _fake_az(outside_git, VAULT_REPLY)
    loose = outside_git / "loose.txt"
    loose.write_text("x")
    loose.chmod(0o644)
    refused = _run(
        "bootstrap_secrets.py",
        env,
        *["--state", str(outside_git / "a"), "--teams-client-secret-file", str(loose)],
    )
    assert refused.returncode != 0 and "mode 0600" in refused.stderr
    prepared = _run("bootstrap_secrets.py", env, "--state", str(outside_git / "b"))
    assert prepared.returncode == 0, prepared.stderr
    assert _az_calls(env) == []
    again = _run("bootstrap_secrets.py", env, "--state", str(outside_git / "b"))
    assert again.returncode != 0 and "never regenerates" in again.stderr


def _apps(snapshot: dict) -> dict[str, dict]:
    return {
        r["name"].rsplit("-", 1)[-1]: r
        for r in snapshot["predictedResources"]
        if r["type"] == "Microsoft.App/containerApps"
    }


def _mcp_urls(apps: dict[str, dict]) -> set[str]:
    return {
        e["value"]
        for app in apps.values()
        for e in app["properties"]["template"]["containers"][0]["env"]
        if e["name"] == "DAIMON_MCP__PUBLIC_URL"
    }


SERVING = (
    "containerImage = 'example.azurecr.io/daimon@sha256:" + "0" * 64 + "'",
    "deployServices = true",
)


def test_mcp_can_stay_private_while_teams_is_public(
    templates: dict[str, dict], tmp_path: Path
) -> None:
    for entrypoint in ("main.bicep", "azd/main.bicep"):
        assert templates[entrypoint]["parameters"]["mcpPublicIngress"]["defaultValue"] is True
    assert templates["modules/app.bicep"]["parameters"]["externalIngress"]["defaultValue"] is True

    code, diagnostics, default = _snapshot(tmp_path, *SERVING)
    assert code == 0, diagnostics
    apps = _apps(default)
    assert set(apps) == {"discord", "teams", "mcp", "scheduler"}
    assert apps["mcp"]["properties"]["configuration"]["ingress"]["external"] is True
    assert apps["teams"]["properties"]["configuration"]["ingress"]["external"] is True
    [url] = _mcp_urls(apps)
    assert "-mcp.{" in url and ".internal." not in url, "current installs keep a public MCP"

    code, diagnostics, private = _snapshot(tmp_path, *SERVING, "mcpPublicIngress = false")
    assert code == 0, diagnostics
    apps = _apps(private)
    assert apps["mcp"]["properties"]["configuration"]["ingress"]["external"] is False
    assert apps["teams"]["properties"]["configuration"]["ingress"]["external"] is True
    for worker in ("discord", "scheduler"):
        assert apps[worker]["properties"]["configuration"].get("ingress") is None
    [url] = _mcp_urls(apps)
    assert "-mcp.internal.{" in url, "every process uses the environment-internal MCP URL"
