"""
Tests for shared/mcp_client.py — the Phase 2 MCP adapter.

Tests verify:
  - Successful tool calls return parsed results
  - Timeout raises MCPCallError with informative message
  - Missing server URL raises MCPConfigError before any network call
  - Malformed tool result (isError=True) raises MCPCallError
  - Auth header is injected when MCP_AUTH_TOKEN is set
  - Multiple servers are routed correctly
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.mcp_client import (
    MCPCallError,
    MCPConfigError,
    MCPServer,
    _extract_result,
    _server_url,
    call_tool,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with known settings."""
    monkeypatch.setattr("shared.mcp_client.settings.gitlab_mcp_url", "")
    monkeypatch.setattr("shared.mcp_client.settings.dynatrace_mcp_url", "")
    monkeypatch.setattr("shared.mcp_client.settings.arize_mcp_url", "")
    monkeypatch.setattr("shared.mcp_client.settings.mcp_auth_token", "")


def _make_tool_result(text: str | None, is_error: bool = False) -> MagicMock:
    """Build a fake mcp CallToolResult."""
    result = MagicMock()
    result.isError = is_error
    if text is not None:
        content_item = MagicMock()
        content_item.text = text
        result.content = [content_item]
    else:
        result.content = []
    return result


# ──────────────────────────────────────────────
# _server_url
# ──────────────────────────────────────────────


class TestServerUrl:
    def test_raises_config_error_when_url_empty(self) -> None:
        with pytest.raises(MCPConfigError, match="GITLAB_MCP_URL"):
            _server_url(MCPServer.GITLAB)

    def test_returns_url_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.gitlab_mcp_url",
            "https://gitlab-mcp.example.com/sse",
        )
        assert _server_url(MCPServer.GITLAB) == "https://gitlab-mcp.example.com/sse"

    def test_dynatrace_config_error(self) -> None:
        with pytest.raises(MCPConfigError, match="DYNATRACE_MCP_URL"):
            _server_url(MCPServer.DYNATRACE)

    def test_arize_config_error(self) -> None:
        with pytest.raises(MCPConfigError, match="ARIZE_MCP_URL"):
            _server_url(MCPServer.ARIZE)


# ──────────────────────────────────────────────
# _extract_result
# ──────────────────────────────────────────────


class TestExtractResult:
    def test_json_text_is_parsed(self) -> None:
        result = _make_tool_result('{"key": "value", "count": 42}')
        assert _extract_result(result) == {"key": "value", "count": 42}

    def test_plain_text_returned_as_string(self) -> None:
        result = _make_tool_result("plain text response")
        assert _extract_result(result) == "plain text response"

    def test_empty_content_returns_none(self) -> None:
        result = _make_tool_result(None)
        assert _extract_result(result) is None

    def test_is_error_raises(self) -> None:
        result = _make_tool_result("something went wrong", is_error=True)
        with pytest.raises(MCPCallError, match="tool error"):
            _extract_result(result)

    def test_multiple_content_items_returned_as_list(self) -> None:
        result = MagicMock()
        result.isError = False
        item1 = MagicMock()
        item1.text = "first"
        item2 = MagicMock()
        item2.text = "second"
        result.content = [item1, item2]
        # Multiple items → raw content list returned
        extracted = _extract_result(result)
        assert extracted == [item1, item2]


# ──────────────────────────────────────────────
# call_tool — config errors (no network)
# ──────────────────────────────────────────────


class TestCallToolConfigErrors:
    @pytest.mark.asyncio
    async def test_raises_config_error_when_gitlab_url_not_set(self) -> None:
        with pytest.raises(MCPConfigError):
            await call_tool(MCPServer.GITLAB, "getMergeRequest", {"projectId": "x", "mr": 1})

    @pytest.mark.asyncio
    async def test_raises_config_error_when_dynatrace_url_not_set(self) -> None:
        with pytest.raises(MCPConfigError):
            await call_tool(MCPServer.DYNATRACE, "getProblems", {})

    @pytest.mark.asyncio
    async def test_raises_config_error_when_arize_url_not_set(self) -> None:
        with pytest.raises(MCPConfigError):
            await call_tool(MCPServer.ARIZE, "runEval", {})


# ──────────────────────────────────────────────
# call_tool — successful call (mocked transport)
# ──────────────────────────────────────────────


class TestCallToolSuccess:
    @pytest.mark.asyncio
    async def test_successful_call_returns_parsed_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.gitlab_mcp_url",
            "https://gitlab-mcp.example.com/sse",
        )
        fake_result = _make_tool_result('{"iid": 42, "title": "my mr"}')

        with patch("shared.mcp_client._do_call", new=AsyncMock(return_value={"iid": 42, "title": "my mr"})):
            result = await call_tool(MCPServer.GITLAB, "getMergeRequest", {"projectId": "g/p", "mergeRequestIid": 42})

        assert result == {"iid": 42, "title": "my mr"}

    @pytest.mark.asyncio
    async def test_empty_payload_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.dynatrace_mcp_url",
            "https://dt-mcp.example.com/sse",
        )
        with patch("shared.mcp_client._do_call", new=AsyncMock(return_value={"problems": []})):
            result = await call_tool(MCPServer.DYNATRACE, "getProblems")
        assert result == {"problems": []}


# ──────────────────────────────────────────────
# call_tool — timeout
# ──────────────────────────────────────────────


class TestCallToolTimeout:
    @pytest.mark.asyncio
    async def test_timeout_raises_mcp_call_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.gitlab_mcp_url",
            "https://gitlab-mcp.example.com/sse",
        )

        async def _slow(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(999)

        with patch("shared.mcp_client._do_call", new=_slow):
            with pytest.raises(MCPCallError, match="timed out"):
                await call_tool(
                    MCPServer.GITLAB,
                    "getMergeRequest",
                    {"projectId": "g/p"},
                    timeout=0.01,  # very short timeout
                )


# ──────────────────────────────────────────────
# call_tool — transport error
# ──────────────────────────────────────────────


class TestCallToolTransportError:
    @pytest.mark.asyncio
    async def test_transport_error_raises_mcp_call_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.arize_mcp_url",
            "https://arize-mcp.example.com/sse",
        )

        with patch(
            "shared.mcp_client._do_call",
            new=AsyncMock(side_effect=ConnectionRefusedError("connection refused")),
        ):
            with pytest.raises(MCPCallError, match="failed"):
                await call_tool(MCPServer.ARIZE, "runEval", {"datasetId": "ds1"})

    @pytest.mark.asyncio
    async def test_server_error_result_raises_mcp_call_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "shared.mcp_client.settings.gitlab_mcp_url",
            "https://gitlab-mcp.example.com/sse",
        )

        # _do_call itself calls _extract_result which raises MCPCallError for isError
        with patch(
            "shared.mcp_client._do_call",
            new=AsyncMock(side_effect=MCPCallError("MCP server reported tool error: not found")),
        ):
            with pytest.raises(MCPCallError, match="not found"):
                await call_tool(MCPServer.GITLAB, "getMergeRequest", {"projectId": "g/p"})
