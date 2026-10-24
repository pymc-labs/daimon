"""Private, strictly validated Teams QA target configuration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Persona(StrictModel):
    upn: str
    role: Literal["admin", "member"]
    vault_item: str
    expected_oid: str | None = None


class FileTokenStore(StrictModel):
    kind: Literal["file"]
    token_dir: Path = Field(strict=False)


class SecretManagerTokenStore(StrictModel):
    kind: Literal["secret_manager"]
    project: str
    secret_prefix: str = "teams-qa"


TokenStoreConfig = Annotated[FileTokenStore | SecretManagerTokenStore, Field(discriminator="kind")]


class TeamsQaConfig(StrictModel):
    tenant_id: str
    client_id: str
    redirect_host: Literal["localhost"] = "localhost"
    team_id: str
    channel_id: str
    team_display_name: str
    channel_display_name: str
    bot_app_id: str
    bot_display_name: str
    token_dir: Path | None = Field(default=None, strict=False)
    token_store: TokenStoreConfig | None = None
    personas: dict[str, Persona]
    company: str = "teams-qa"

    @model_validator(mode="before")
    @classmethod
    def legacy_token_dir_override(cls, data: object) -> object:
        """Let an explicit legacy token_dir override a file-store path."""
        if isinstance(data, dict) and "token_dir" in data:
            values = cast(dict[str, object], data)
            store = values.get("token_store")
            if isinstance(store, dict):
                file_store = cast(dict[str, object], store)
                if file_store.get("kind") == "file":
                    return {
                        **values,
                        "token_store": {**file_store, "token_dir": values["token_dir"]},
                    }
        return cast(object, data)

    @model_validator(mode="after")
    def private_cache(self) -> Self:
        """Require private cache paths and safe persona filenames."""
        repo = Path(__file__).resolve().parents[3]
        if self.token_store is None:
            if self.token_dir is None:
                raise ValueError("token_dir or token_store is required")
            self.token_store = FileTokenStore(kind="file", token_dir=self.token_dir)
        if isinstance(self.token_store, FileTokenStore):
            target = self.token_store.token_dir.expanduser().resolve()
            if target == repo or repo in target.parents:
                raise ValueError("token_dir must be outside the repository")
            self.token_store.token_dir = target
            self.token_dir = target
        else:
            if self.token_dir is not None:
                raise ValueError("token_dir is only valid for a file token_store")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", self.token_store.project):
                raise ValueError("invalid Secret Manager project")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", self.token_store.secret_prefix):
                raise ValueError("invalid Secret Manager secret_prefix")
        if not self.personas:
            raise ValueError("at least one persona is required")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in self.personas):
            raise ValueError("invalid persona name")
        upns = [identity.upn.casefold() for identity in self.personas.values()]
        if len(upns) != len(set(upns)):
            raise ValueError("persona UPNs must be distinct")
        oids = [
            identity.expected_oid.casefold()
            for identity in self.personas.values()
            if identity.expected_oid is not None
        ]
        if len(oids) != len(set(oids)):
            raise ValueError("persona object IDs must be distinct")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.company):
            raise ValueError("invalid company name")
        return self

    @classmethod
    def load(cls, path: Path | None = None, config_secret: str | None = None) -> TeamsQaConfig:
        """Load a private JSON config and secure its token directory."""
        from qa.live.teams.secret_store import read_secret

        selected = path or (None if config_secret else os.environ.get("DAIMON_TEAMS_QA_CONFIG"))
        secret = config_secret or os.environ.get("DAIMON_TEAMS_QA_CONFIG_SECRET")
        if selected is not None:
            content = Path(selected).read_text()
        elif secret is not None:
            if not re.fullmatch(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+", secret):
                raise ValueError("invalid config secret name")
            content = read_secret(secret)
        else:
            raise ValueError("--config or --config-secret is required")
        config = cls.model_validate(json.loads(content))
        store = config.token_store
        if secret is not None and selected is None and isinstance(store, SecretManagerTokenStore):
            tokens = {
                f"projects/{store.project}/secrets/{store.secret_prefix}-token-{persona}"
                for persona in config.personas
            }
            if secret in tokens:
                raise ValueError("config secret must not be a token secret")
        if isinstance(config.token_store, FileTokenStore):
            config.token_store.token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            config.token_store.token_dir.chmod(0o700)
        return config
