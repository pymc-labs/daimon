"""Vault request schemas: secret material is resolved only inside the driver."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from mux.drivers.anthropic.schemas import NativeConfig


class NoTokenAuth(NativeConfig):
    type: Literal["none"]


class ClientTokenAuth(NativeConfig):
    type: Literal["client_secret_basic", "client_secret_post"]
    client_secret_ref: str


class OAuthRefresh(NativeConfig):
    client_id: str
    refresh_token_ref: str
    token_endpoint: str
    token_endpoint_auth: Annotated[NoTokenAuth | ClientTokenAuth, Field(discriminator="type")]
    resource: str | None = None
    scope: str | None = None


class BearerCreate(NativeConfig):
    type: Literal["static_bearer"]
    mcp_server_url: str
    token_ref: str


class OAuthCreate(NativeConfig):
    type: Literal["mcp_oauth"]
    mcp_server_url: str
    access_token_ref: str
    expires_at: str | datetime | None = None
    refresh: OAuthRefresh | None = None


class CredentialUnrestrictedNetwork(NativeConfig):
    type: Literal["unrestricted"]


class CredentialLimitedNetwork(NativeConfig):
    type: Literal["limited"]
    allowed_hosts: list[str]


type CredentialNetwork = Annotated[
    CredentialUnrestrictedNetwork | CredentialLimitedNetwork, Field(discriminator="type")
]


class InjectionLocation(NativeConfig):
    body: bool | None = None
    header: bool | None = None


class EnvironmentCreate(NativeConfig):
    type: Literal["environment_variable"]
    secret_name: str
    secret_value_ref: str
    networking: CredentialNetwork
    injection_location: InjectionLocation | None = None


class CredentialCreate(NativeConfig):
    auth: Annotated[BearerCreate | OAuthCreate | EnvironmentCreate, Field(discriminator="type")]
    display_name: str | None = None
    metadata: dict[str, str] | None = None


class BearerUpdate(NativeConfig):
    type: Literal["static_bearer"]
    token_ref: str | None = None


class EnvironmentUpdate(NativeConfig):
    type: Literal["environment_variable"]
    secret_value_ref: str | None = None
    networking: CredentialNetwork | None = None
    injection_location: InjectionLocation | None = None


class ClientTokenAuthUpdate(NativeConfig):
    type: Literal["client_secret_basic", "client_secret_post"]
    client_secret_ref: str | None = None


class OAuthRefreshUpdate(NativeConfig):
    refresh_token_ref: str | None = None
    token_endpoint_auth: ClientTokenAuthUpdate | None = None
    scope: str | None = None


class OAuthUpdate(NativeConfig):
    type: Literal["mcp_oauth"]
    access_token_ref: str | None = None
    expires_at: str | datetime | None = None
    refresh: OAuthRefreshUpdate | None = None


class CredentialUpdate(NativeConfig):
    auth: (
        Annotated[BearerUpdate | EnvironmentUpdate | OAuthUpdate, Field(discriminator="type")]
        | None
    ) = None
    display_name: str | None = None
    metadata: dict[str, str | None] | None = None
