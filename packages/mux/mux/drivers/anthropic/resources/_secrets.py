"""Resolve credential references for one request; keep errors free of values."""

import json
import logging
import traceback
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import cast

import httpx
from anthropic import AnthropicError, APIConnectionError, APIStatusError, APITimeoutError

from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources._errors import normalize_error
from mux.drivers.anthropic.schemas import NativeConfig
from mux.errors import ProviderError, ScopeViolation

SecretResolver = Callable[[Scope, str], str]

# The SDK logs request options at DEBUG, including resolved auth values. Keep
# their redaction local to this request, including concurrent/nested requests.
# The filter holds no material; the context is reset and cleared after I/O.
_active_materials: ContextVar[list[str] | None] = ContextVar(
    "anthropic_credential_materials", default=None
)


def _material_variants(materials: list[str]) -> list[str]:
    # SDK messages/logs format JSON bodies with repr; their escaped form must
    # be removed alongside the original text, including multiline env values.
    variants = [
        variant
        for material in materials
        for variant in (
            material,
            repr(material)[1:-1],
            json.dumps(material)[1:-1],
            material.replace("\\", "\\\\").replace("'", "\\'"),
            material.replace("\\", "\\\\").replace('"', '\\"'),
        )
    ]
    # An upstream message can already contain an escaped value, then the SDK
    # includes that message in a repr/JSON-formatted error or request log.
    return [
        encoded
        for variant in variants
        for encoded in (variant, repr(variant)[1:-1], json.dumps(variant)[1:-1])
    ]


class _CredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        materials = _active_materials.get()
        if not materials:
            return True
        variants = _material_variants(materials)
        record.msg = _redact(record.getMessage(), variants)
        record.args = ()
        if record.exc_info:
            record.exc_text = cast(
                str, _redact("".join(traceback.format_exception(*record.exc_info)), variants)
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = cast(str, _redact(record.exc_text, variants))
        return True


logging.getLogger("anthropic._base_client").addFilter(_CredentialLogFilter())


class CredentialFileUpload(NativeConfig):
    filename: str
    media_type: str
    content_ref: str
    secret_refs: tuple[str, ...] = ()


def unavailable_secret(scope: Scope, reference: str) -> str:
    raise ScopeViolation("credential", "no host credential resolver configured")


def _redact(value: object, secrets: list[str]) -> object:
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, Mapping):
        return {
            cast(str, _redact(str(k), secrets)): _redact(v, secrets)
            for k, v in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in cast(list[object] | tuple[object, ...], value)]
    return value


def _safe_error(error: AnthropicError, secrets: list[str]) -> AnthropicError:
    secrets = _material_variants(secrets)
    # SDK request objects hold credential bodies and authorization headers.
    # Retain method/path/status/request-id and today's message unless it echoes
    # a credential, but never retain that request's values in the cause.
    if isinstance(error, (APIStatusError, APIConnectionError)):
        request = httpx.Request(
            error.request.method, cast(str, _redact(str(error.request.url), secrets))
        )
        if isinstance(error, APIStatusError):
            response = httpx.Response(
                error.status_code,
                headers={
                    name: cast(str, _redact(value, secrets))
                    for name, value in error.response.headers.items()
                },
                request=request,
            )
            return type(error)(
                cast(str, _redact(error.message, secrets)),
                response=response,
                body=_redact(error.body, secrets),
            )
        if isinstance(error, APITimeoutError):
            return APITimeoutError(request=request)
        return APIConnectionError(message=cast(str, _redact(str(error), secrets)), request=request)
    return AnthropicError(cast(str, _redact(str(error), secrets)))


async def credential_request[T](
    scope: Scope,
    config: NativeConfig,
    resolver: SecretResolver,
    send: Callable[[dict[str, object]], Awaitable[T]],
) -> T:
    values: list[str] = []
    material_context = _active_materials.set(values)
    kwargs: dict[str, object] = config.model_dump(mode="python", exclude_unset=True)
    error: ProviderError | None = None

    def resolve(node: object) -> object:
        if isinstance(node, (list, tuple)):
            return [resolve(item) for item in cast(list[object] | tuple[object, ...], node)]
        if isinstance(node, Mapping):
            result: dict[str, object] = {}
            for name, value in cast(Mapping[str, object], node).items():
                if name in {
                    "token_ref",
                    "access_token_ref",
                    "refresh_token_ref",
                    "client_secret_ref",
                    "secret_value_ref",
                    "authorization_token_ref",
                }:
                    if value is None:
                        result[name.removesuffix("_ref")] = None
                    else:
                        material = resolver(scope, cast(str, value))
                        values.append(material)
                        result[name.removesuffix("_ref")] = material
                else:
                    result[name] = resolve(value)
            return result
        return node

    pending: Awaitable[T] | None = None
    try:
        # Metadata is free text and is never interpreted as secret references.
        if "auth" in kwargs:
            kwargs["auth"] = resolve(kwargs["auth"])
        elif "authorization_token_ref" in kwargs:
            kwargs = cast(dict[str, object], resolve(kwargs))
        elif "resources" in kwargs:
            kwargs["resources"] = resolve(kwargs["resources"])
        elif "content_ref" in kwargs:
            content = resolver(scope, cast(str, kwargs.pop("content_ref")))
            values.append(content)
            for reference in cast(tuple[str, ...], kwargs.pop("secret_refs", ())):
                values.append(resolver(scope, reference))
            kwargs["content"] = content.encode("utf-8")
            content = ""
        pending = send(kwargs)
        return await pending
    except AnthropicError as native:
        safe = _safe_error(native, values)
        error = normalize_error(safe)
        error.__cause__ = safe
    finally:
        _active_materials.reset(material_context)
        kwargs.clear()
        values.clear()
        pending = None
    # Raise outside the SDK exception context: no original credential request
    # remains attached to the normalized error or its restored host cause.
    assert error is not None
    raise error from error.__cause__
