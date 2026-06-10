"""
Bulwark — Shared MCP Client Adapter (Phase 2, PLAN.md §Phase 2).

Provides a single typed ``call_tool(server, tool_name, payload)`` wrapper
that routes calls to the correct remote MCP server (GitLab / Dynatrace / Arize)
using the official ``mcp`` Python SDK over the Streamable-HTTP transport.

Design decisions
----------------
* **No production REST fallback.** Per PLAN.md: "No production REST fallback."
  Tests inject fake MCP responses by patching ``call_tool``.
* **Config errors are explicit.** If a server URL is not configured,
  ``MCPConfigError`` is raised immediately rather than silently falling back.
* **Auth header injection.** If ``MCP_AUTH_TOKEN`` is set, it is sent as
  ``Authorization: Bearer <token>`` on every request.
* **Structured logging** — every call logs server, tool, payload size, duration,
  and the error type on failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

from shared.config import settings

logger = logging.getLogger("bulwark.mcp_client")

# Default per-call timeout in seconds
DEFAULT_TIMEOUT_S: float = 30.0

# Phase 5: maximum retries for transient transport failures
# Tool-level errors (isError=True) and MCPConfigError are never retried.
MCP_MAX_RETRIES: int = 3


# ──────────────────────────────────────────────
# Error types
# ──────────────────────────────────────────────


class MCPConfigError(RuntimeError):
    """Raised when a required MCP server URL is not configured."""


class MCPCallError(RuntimeError):
    """Raised when an MCP tool call fails (timeout, transport error, or server error)."""


# ──────────────────────────────────────────────
# Server registry
# ──────────────────────────────────────────────


class MCPServer(str, Enum):
    """Registered MCP servers."""

    GITLAB = "gitlab"
    DYNATRACE = "dynatrace"
    ARIZE = "arize"


def _server_url(server: MCPServer) -> str:
    """
    Resolve the URL for a given MCP server from settings.

    Raises MCPConfigError if the URL is not configured.
    """
    mapping: dict[MCPServer, str] = {
        MCPServer.GITLAB: settings.gitlab_mcp_url,
        MCPServer.DYNATRACE: settings.dynatrace_mcp_url,
        MCPServer.ARIZE: settings.arize_mcp_url,
    }
    url = mapping.get(server, "").strip()
    if not url:
        raise MCPConfigError(
            f"MCP server URL for '{server.value}' is not configured. "
            f"Set the {server.value.upper()}_MCP_URL environment variable."
        )
    return url


def _auth_headers() -> dict[str, str]:
    """Return Authorization header dict if MCP_AUTH_TOKEN is set."""
    token = settings.mcp_auth_token.strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ──────────────────────────────────────────────
# Core call_tool wrapper
# ──────────────────────────────────────────────


async def call_tool(
    server: MCPServer,
    tool_name: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """
    Call a tool on a remote MCP server and return the parsed result.

    Phase 5: Transient transport errors and timeouts are retried up to
    MCP_MAX_RETRIES (3) times with exponential back-off (2^n seconds).
    MCPConfigError (missing URL) and tool-level server errors (isError=True)
    are NOT retried — they fail immediately.

    Args:
        server:    The target MCP server (GITLAB, DYNATRACE, ARIZE).
        tool_name: The MCP tool name (e.g. ``getMergeRequest``).
        payload:   Tool input arguments dict (sent as ``arguments``).
        timeout:   Per-call timeout in seconds (applied per attempt).

    Returns:
        The parsed result from the first ``content`` item returned by the tool.
        If the server returns a list, the full list is returned.

    Raises:
        MCPConfigError: If the server URL is not configured.
        MCPCallError:   If the call fails after all retries.
    """
    url = _server_url(server)  # may raise MCPConfigError immediately
    args = payload or {}
    start = time.monotonic()

    logger.info(
        "MCP call → %s/%s (args_keys=%s)",
        server.value,
        tool_name,
        list(args.keys()),
    )

    try:
        result = await _do_call_with_retry(
            url=url,
            tool_name=tool_name,
            args=args,
            timeout=timeout,
            server_label=server.value,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "MCP call ← %s/%s OK (%dms)",
            server.value,
            tool_name,
            elapsed_ms,
        )
        return result

    except MCPConfigError:
        raise

    except MCPCallError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "MCP call ✗ %s/%s failed after %dms (all retries exhausted)",
            server.value,
            tool_name,
            elapsed_ms,
        )
        raise

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "MCP call ✗ %s/%s ERROR after %dms: %s",
            server.value,
            tool_name,
            elapsed_ms,
            exc,
        )
        raise MCPCallError(
            f"MCP call to {server.value}/{tool_name} failed: {exc}"
        ) from exc


async def _do_call_with_retry(
    url: str,
    tool_name: str,
    args: dict[str, Any],
    timeout: float,
    server_label: str,
    max_retries: int = MCP_MAX_RETRIES,
) -> Any:
    """
    Execute an MCP tool call with bounded retries and exponential back-off.

    Retry policy:
    - Retries on: asyncio.TimeoutError, ConnectionError, OSError, and generic
      Exception (transient transport failures).
    - Does NOT retry: MCPCallError with isError=True (server-side tool errors),
      MCPConfigError (configuration problem).
    - Back-off: sleeps 2^attempt seconds between retries (2s, 4s, 8s).
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):  # attempt 0 is the first try
        try:
            result = await asyncio.wait_for(
                _do_call(url, tool_name, args),
                timeout=timeout,
            )
            return result

        except MCPCallError as exc:
            # Tool-level server error (isError=True) — do not retry
            raise

        except asyncio.TimeoutError as exc:
            last_exc = MCPCallError(
                f"MCP call to {server_label}/{tool_name} timed out after {timeout}s"
            )
            logger.warning(
                "MCP call %s/%s timeout (attempt %d/%d)",
                server_label,
                tool_name,
                attempt + 1,
                max_retries + 1,
            )

        except Exception as exc:
            last_exc = exc
            logger.warning(
                "MCP call %s/%s transient error (attempt %d/%d): %s",
                server_label,
                tool_name,
                attempt + 1,
                max_retries + 1,
                exc,
            )

        if attempt < max_retries:
            backoff = 2 ** attempt  # 1s, 2s, 4s
            logger.info(
                "MCP retry %s/%s in %ds (attempt %d/%d)",
                server_label,
                tool_name,
                backoff,
                attempt + 1,
                max_retries + 1,
            )
            await asyncio.sleep(backoff)

    raise MCPCallError(
        f"MCP call to {server_label}/{tool_name} failed after {max_retries + 1} attempts: {last_exc}"
    ) from last_exc


async def _do_call(url: str, tool_name: str, args: dict[str, Any]) -> Any:
    """
    Execute a single MCP tool call.
    Uses SSE transport for /sse endpoints and streamable-HTTP for others.

    Opens a fresh session per call (stateless; no persistent connection kept
    between calls so that Cloud Run scale-to-zero is not impacted).
    """
    headers = _auth_headers()

    if url.endswith("/sse"):
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
        return _extract_result(result)
    else:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
        return _extract_result(result)


def _extract_result(result: Any) -> Any:
    """
    Extract the usable value from an MCP CallToolResult.

    * If ``result.isError`` is True → raise MCPCallError.
    * If ``result.content`` has exactly one TextContent → return its parsed value.
    * Otherwise → return the raw content list.
    """
    import json as _json

    # Guard: tool execution error reported by the server
    if getattr(result, "isError", False):
        content_text = _first_text(result) or "unknown server error"
        raise MCPCallError(f"MCP server reported tool error: {content_text}")

    content = getattr(result, "content", [])

    if not content:
        return None

    if len(content) == 1:
        text = getattr(content[0], "text", None)
        if text is not None:
            # Try to parse JSON; fall back to raw string
            try:
                return _json.loads(text)
            except (_json.JSONDecodeError, TypeError):
                return text

    # Multiple items — return them as-is for the caller to interpret
    return content


def _first_text(result: Any) -> str | None:
    """Return the text of the first TextContent item, if any."""
    content = getattr(result, "content", [])
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            return text
    return None
