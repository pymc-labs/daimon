"""Tests for render_error -- structured error rendering with emoji, bold labels, and request IDs."""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import discord
import httpx
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.core.errors import DaimonError, SpecError, StoreError

TEST_RID = "01JTZXTEST000000000000000"


class TestRenderError:
    def test_spec_error(self) -> None:
        exc = SpecError("invalid field 'foo'")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("⚠️"), "should start with warning emoji"
        assert "**Spec validation failed**" in result, "should have bold label"
        assert "invalid field" in result, "should include error detail"
        assert TEST_RID in result, "should include request ID"

    def test_store_error(self) -> None:
        exc = StoreError("agent not found")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("⚠️"), "should start with warning emoji"
        assert "**Store error**" in result, "should have bold label"
        assert "agent not found" in result, "should include error detail"
        assert TEST_RID in result, "should include request ID"

    def test_daimon_error_generic(self) -> None:
        exc = DaimonError("something broke")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("⚠️"), "should start with warning emoji"
        assert "**Error**" in result, "should have bold label"
        assert "something broke" in result, "should include error detail"
        assert TEST_RID in result, "should include request ID"

    def test_api_connection_error(self) -> None:
        exc = anthropic.APIConnectionError(
            request=httpx.Request("GET", "https://api.anthropic.com"),
        )
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("\U0001f50c"), "should start with plug emoji"
        assert "**Connection Error**" in result, "should have bold label"
        assert "Could not connect" in result, "should mention connection failure"
        assert TEST_RID in result, "should include request ID"

    def test_api_status_error(self) -> None:
        exc = anthropic.APIStatusError(
            message="rate limited",
            response=httpx.Response(
                status_code=429,
                request=httpx.Request("POST", "https://api.anthropic.com"),
            ),
            body=None,
        )
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("❌"), "should start with cross mark emoji"
        assert "**API Error (429)**" in result, "should have bold label with status"
        assert "rate limited" in result, "should include error message"
        assert TEST_RID in result, "should include request ID"

    def test_generic_api_error(self) -> None:
        exc = anthropic.APIError(
            message="unknown api error",
            request=httpx.Request("POST", "https://api.anthropic.com"),
            body=None,
        )
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("❌"), "should start with cross mark emoji"
        assert "**API Error**" in result, "should have bold label"
        assert "unknown api error" in result, "should include message"
        assert TEST_RID in result, "should include request ID"

    def test_unexpected_error(self) -> None:
        exc = RuntimeError("boom")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("❌"), "should start with cross mark emoji"
        assert "**Unexpected error**" in result, "should have bold label"
        assert "boom" in result, "should include error text"
        assert TEST_RID in result, "should include request ID"

    def test_value_error(self) -> None:
        exc = ValueError("bad input")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("⚠️"), "should start with warning emoji"
        assert "**Invalid input**" in result, "should have bold label"
        assert "bad input" in result, "should include error detail"
        assert TEST_RID in result, "should include request ID"

    def test_discord_http_exception(self) -> None:
        response = MagicMock()
        response.status = 403
        exc = discord.HTTPException(response, "Forbidden")
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("❌"), "should start with cross mark emoji"
        assert "Discord Error" in result, "should mention Discord error"
        assert "403" in result, "should include status code"
        assert TEST_RID in result, "should include request ID"


class TestGenerateRequestId:
    def test_generate_request_id_is_ulid(self) -> None:
        rid = generate_request_id()
        assert isinstance(rid, str), "should return a string"
        assert len(rid) == 26, "ULID should be 26 characters"

    def test_generate_request_id_unique(self) -> None:
        rid1 = generate_request_id()
        rid2 = generate_request_id()
        assert rid1 != rid2, "consecutive ULIDs should be unique"
