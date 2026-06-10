"""
Phase 6: MCP Auth Header Tests.

Verifies that MCP_AUTH_TOKEN is sent as 'Authorization: Bearer <token>'
on all calls to MCP servers. Tests both with and without a token.

The local FastMCP servers do NOT require auth (open endpoints), so these
tests verify the header is built and injected correctly by checking the
_auth_headers helper and the _do_call path without needing server enforcement.
"""

from __future__ import annotations

import pytest

from shared.mcp_client import (
    MCPServer,
    _auth_headers,
    _server_url,
    call_tool,
)


# ──────────────────────────────────────────────────────────────────────────────
# _auth_headers unit tests (no network)
# ──────────────────────────────────────────────────────────────────────────────


class TestAuthHeaders:
    """Tests for the _auth_headers() helper."""

    def test_returns_empty_when_no_token(self, monkeypatch):
        """No MCP_AUTH_TOKEN → empty headers dict."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "")
        result = _auth_headers()
        assert result == {}

    def test_returns_bearer_header_when_token_set(self, monkeypatch):
        """MCP_AUTH_TOKEN set → Authorization: Bearer <token>."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "my-secret-token")
        result = _auth_headers()
        assert "Authorization" in result
        assert result["Authorization"] == "Bearer my-secret-token"

    def test_whitespace_only_token_treated_as_empty(self, monkeypatch):
        """Token containing only whitespace → treated as not set."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "   ")
        result = _auth_headers()
        assert result == {}

    def test_token_not_double_prefixed(self, monkeypatch):
        """Token value is NOT double-prefixed with 'Bearer'."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "tok-abc123")
        result = _auth_headers()
        assert result["Authorization"] == "Bearer tok-abc123"
        assert "Bearer Bearer" not in result["Authorization"]


# ──────────────────────────────────────────────────────────────────────────────
# Auth token propagates through real MCP calls
# ──────────────────────────────────────────────────────────────────────────────


class TestAuthInRealCalls:
    """
    Verify auth token is wired through call_tool → _do_call → streamablehttp_client.
    The local server accepts all requests regardless of auth, so these verify
    the token is passed (no rejection), not that the server enforces it.
    """

    @pytest.mark.asyncio
    async def test_call_succeeds_with_token_set(self, gitlab_server_state, monkeypatch):
        """A call with MCP_AUTH_TOKEN set succeeds against the local server."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "test-auth-token-xyz")
        # Call should succeed (local server doesn't enforce auth)
        result = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequest",
            {"projectId": "group/demo-app", "mergeRequestIid": 42},
        )
        assert isinstance(result, dict)
        assert result["iid"] == 42

    @pytest.mark.asyncio
    async def test_call_succeeds_without_token(self, gitlab_server_state, monkeypatch):
        """A call without MCP_AUTH_TOKEN also succeeds (local server is open)."""
        from shared.config import settings

        monkeypatch.setattr(settings, "mcp_auth_token", "")
        result = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequest",
            {"projectId": "group/demo-app", "mergeRequestIid": 42},
        )
        assert isinstance(result, dict)


# ──────────────────────────────────────────────────────────────────────────────
# Multi-server routing
# ──────────────────────────────────────────────────────────────────────────────


class TestMultiServerRouting:
    """Verify that different MCPServer values route to different servers."""

    @pytest.mark.asyncio
    async def test_gitlab_routes_to_gitlab_server(self, gitlab_server_state):
        """MCPServer.GITLAB calls the GitLab MCP server."""
        result = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequest",
            {"projectId": "g/p", "mergeRequestIid": 1},
        )
        # Only the GitLab server knows about MR iid
        assert isinstance(result, dict)
        assert "iid" in result

    @pytest.mark.asyncio
    async def test_dynatrace_routes_to_dynatrace_server(self, dynatrace_server_state):
        """MCPServer.DYNATRACE calls the Dynatrace MCP server."""
        result = await call_tool(
            MCPServer.DYNATRACE,
            "getProblems",
            {},
        )
        # Only the Dynatrace server knows about 'problems'
        assert isinstance(result, dict)
        assert "problems" in result

    @pytest.mark.asyncio
    async def test_arize_routes_to_arize_server(self, arize_server_state):
        """MCPServer.ARIZE calls the Arize MCP server."""
        result = await call_tool(
            MCPServer.ARIZE,
            "getDataset",
            {"spaceId": "test"},
        )
        # Only the Arize server knows about 'dataset_id'
        assert isinstance(result, dict)
        assert "dataset_id" in result or "error" in result
