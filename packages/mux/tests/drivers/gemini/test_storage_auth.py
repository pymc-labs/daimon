"""Actual google-auth CAB exchange, with only the HTTP edge replaced."""

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from mux.drivers.gemini.deployment import downscoped_storage_token
from mux.errors import ProviderError

SOURCE = "offline-broad-worker-credential"
RESTRICTED = "offline-bucket-prefix-only"
BUCKET = "owned-skills"
PREFIX = "skills/run/immutable/"


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "sts_refused",
        "unchanged",
        "short",
        "malformed",
        "wrong_bucket",
        "wrong_prefix",
        "root_prefix",
        "broader_prefix",
    ],
)
def test_storage_exchange_has_one_exact_read_only_cab_and_no_broad_fallback(
    monkeypatch, caplog, defect
):
    calls = []
    client_type = httpx.Client

    def handle(request):
        calls.append(request)
        assert (
            request.method == "POST" and str(request.url) == "https://sts.googleapis.com/v1/token"
        )
        body = parse_qs(request.content.decode())
        assert body["subject_token"] == [SOURCE]
        boundary = json.loads(unquote(body["options"][0]))
        assert boundary == {
            "accessBoundary": {
                "accessBoundaryRules": [
                    {
                        "availableResource": f"//storage.googleapis.com/projects/_/buckets/{BUCKET}",
                        "availablePermissions": ["inRole:roles/storage.objectViewer"],
                        "availabilityCondition": {
                            "expression": f"resource.name.startsWith('projects/_/buckets/{BUCKET}/objects/{PREFIX}') || api.getAttribute('storage.googleapis.com/objectListPrefix', '').startsWith('{PREFIX}')"
                        },
                    }
                ]
            }
        }
        if defect == "sts_refused":
            return httpx.Response(403, json={"error": SOURCE})
        if defect == "malformed":
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "access_token": SOURCE if defect == "unchanged" else RESTRICTED,
                "expires_in": 5 if defect == "short" else 3300,
                "token_type": "Bearer",
            },
        )

    class Client(client_type):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["proxy"] is None
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    caplog.set_level(logging.DEBUG)
    current = datetime.now(UTC)
    sources = (f"gs://{BUCKET}/{PREFIX}skill/version/", f"gs://{BUCKET}/{PREFIX}other/version/")
    if defect == "wrong_bucket":
        sources = (f"gs://another-tenant/{PREFIX}skill/version/",)
    elif defect == "wrong_prefix":
        sources = (f"gs://{BUCKET}/other-tenant/skill/version/",)
    kwargs = dict(
        token=SOURCE,
        expires_at=current + timedelta(minutes=55),
        retained_until=current + timedelta(minutes=45),
        bucket=BUCKET,
        prefix="/"
        if defect == "root_prefix"
        else "skills/"
        if defect == "broader_prefix"
        else PREFIX,
        sources=sources,
    )
    if defect:
        with pytest.raises(ProviderError, match="gcs_downscoping_failed") as error:
            downscoped_storage_token(**kwargs)
        assert SOURCE not in str(error.value) and RESTRICTED not in str(error.value)
    else:
        assert downscoped_storage_token(**kwargs) == RESTRICTED
    assert len(calls) == (
        0 if defect in ("wrong_bucket", "wrong_prefix", "root_prefix", "broader_prefix") else 1
    )
    assert SOURCE not in caplog.text and RESTRICTED not in caplog.text
