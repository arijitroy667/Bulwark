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
* **Dynatrace OAuth2 token.** Dynatrace MCP Gateway requires a Platform Token
  (Bearer).  We fetch one via client-credentials flow from the Dynatrace SSO
  endpoint using CLIENT_ID / CLIENT_SECRET / CLIENT_URL from settings.  Tokens
  are cached in-process until 30 s before expiry and refreshed automatically.
* **Auth header injection.** For non-Dynatrace servers, if ``MCP_AUTH_TOKEN``
  is set, it is sent as ``Authorization: Bearer <token>`` on every request.
* **Structured logging** — every call logs server, tool, payload size, duration,
  and the error type on failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any

import httpx
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
# Dynatrace OAuth2 token cache
# ──────────────────────────────────────────────

_DT_SSO_TOKEN_URL = "https://sso.dynatrace.com/sso/oauth2/token"
_TOKEN_EXPIRY_BUFFER_S: float = 30.0  # refresh this many seconds before expiry


class _DynatraceTokenCache:
    """Thread-safe (asyncio-safe) in-process cache for the Dynatrace Bearer token."""

    def __init__(self) -> None:
        self._token: str = ""
        self._expires_at: float = 0.0  # monotonic time
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """
        Return a valid Dynatrace Bearer token, refreshing it if needed.

        Raises MCPConfigError if the OAuth2 credentials are not configured.
        Raises MCPCallError if the token fetch from Dynatrace SSO fails.
        """
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token  # still valid

            client_id = settings.client_id.strip()
            client_secret = settings.client_secret.strip()
            resource = settings.client_url.strip()

            if not client_id or not client_secret or not resource:
                raise MCPConfigError(
                    "Dynatrace OAuth2 credentials not configured. "
                    "Set CLIENT-ID, CLIENT-SECRET, and CLIENT_URL in your .env file."
                )

            scope = "mcp-gateway:servers:invoke mcp-gateway:servers:read storage:logs:read storage:metrics:read storage:entities:read storage:events:read storage:bizevents:read"

            logger.info("Fetching new Dynatrace Bearer token from SSO…")
            try:
                async with httpx.AsyncClient(timeout=15.0) as http:
                    resp = await http.post(
                        _DT_SSO_TOKEN_URL,
                        data={
                            "grant_type": "client_credentials",
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "scope": scope,
                            "resource": resource,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPStatusError as exc:
                raise MCPCallError(
                    f"Dynatrace SSO token request failed ({exc.response.status_code}): "
                    f"{exc.response.text}"
                ) from exc
            except Exception as exc:
                raise MCPCallError(
                    f"Dynatrace SSO token request error: {exc}"
                ) from exc

            self._token = body["access_token"]
            expires_in: float = float(body.get("expires_in", 300))
            self._expires_at = time.monotonic() + expires_in - _TOKEN_EXPIRY_BUFFER_S
            logger.info(
                "Dynatrace Bearer token obtained (expires_in=%ss, scope=%s)",
                int(expires_in),
                body.get("scope", ""),
            )
            return self._token


_dt_token_cache = _DynatraceTokenCache()


async def _dynatrace_bearer_token() -> str:
    """Return a valid Dynatrace Bearer token (cached, refreshed on expiry)."""
    return await _dt_token_cache.get()


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

    For Dynatrace, uses the real MCP Gateway URL (derived from env_id or
    ``DYNATRACE_MCP_URL`` override).  For other servers uses their MCP URL.

    Raises MCPConfigError if the URL is not configured.
    """
    if server == MCPServer.DYNATRACE:
        url = settings.dynatrace_mcp_gateway_url.strip()
    else:
        mapping: dict[MCPServer, str] = {
            MCPServer.GITLAB: settings.gitlab_mcp_url,
            MCPServer.ARIZE: settings.arize_mcp_url,
        }
        url = mapping.get(server, "").strip()

    if not url:
        raise MCPConfigError(
            f"MCP server URL for '{server.value}' is not configured. "
            f"Set the {server.value.upper()}_MCP_URL environment variable."
        )
    return url


def _auth_headers(server: MCPServer | None = None) -> dict[str, str]:
    """Return Authorization header dict using the shared MCP_AUTH_TOKEN (non-Dynatrace)."""
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
            server=server,
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
    server: MCPServer | None = None,
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
                _do_call(url, tool_name, args, server=server),
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


async def _do_call(url: str, tool_name: str, args: dict[str, Any], server: MCPServer | None = None) -> Any:
    """
    Execute a single MCP tool call.

    For Dynatrace, fetches a fresh OAuth2 Bearer token from the Dynatrace SSO
    and injects it as the Authorization header.  For all other servers the
    shared MCP_AUTH_TOKEN (if set) is used.

    Uses SSE transport for /sse endpoints and streamable-HTTP for others.

    Opens a fresh session per call (stateless; no persistent connection kept
    between calls so that Cloud Run scale-to-zero is not impacted).

    Dynatrace MCP Gateway quirk: the gateway responds with HTTP 405 on the GET
    SSE stream (it is a pure request/response endpoint, not a push-capable SSE
    server).  The mcp SDK raises an ExceptionGroup from its internal TaskGroup
    when the GET stream fails at session teardown — but the tool result is
    already fully returned by that point.  We detect this situation and return
    the captured result rather than propagating the teardown error.
    """
    if server == MCPServer.DYNATRACE:
        token = await _dynatrace_bearer_token()
        headers = {"Authorization": f"Bearer {token}"}
    else:
        headers = _auth_headers()

    if server == MCPServer.ARIZE:
        from mcp.client.stdio import stdio_client, StdioServerParameters
        
        npx_args = [
            "-y",
            "@arizeai/phoenix-mcp@latest",
            "--baseUrl",
            settings.phoenix_collector_endpoint,
            "--apiKey",
            settings.arize_api_key,
        ]
        
        server_params = StdioServerParameters(
            command="npx",
            args=npx_args,
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
        return _extract_result(result)

    if url.endswith("/sse"):
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=args)
        return _extract_result(result)
    else:
        # Streamable-HTTP path — used for Dynatrace MCP Gateway and other
        # non-SSE endpoints.  We capture the tool result inside the session
        # context so we can return it even if the context manager exit raises
        # due to the server not supporting the SSE GET stream (HTTP 405).
        _result: Any = None
        _call_error: Exception | None = None

        try:
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    _result = await session.call_tool(tool_name, arguments=args)
        except ExceptionGroup as eg:
            # Python 3.11+ ExceptionGroup from the SDK's internal TaskGroup.
            # If the result was already captured (tool call succeeded), we
            # check whether ALL sub-exceptions are the benign GET-405 teardown
            # error.  If so, suppress them and return the result.
            if _result is not None:
                non_405 = [
                    exc for exc in eg.exceptions
                    if not _is_benign_get_405_error(exc)
                ]
                if not non_405:
                    logger.debug(
                        "Suppressing benign MCP GET-405 teardown error for %s",
                        url,
                    )
                    return _extract_result(_result)
            # At least one non-benign sub-exception, or no result captured —
            # re-raise as a plain MCPCallError for the retry layer.
            raise MCPCallError(
                f"MCP streamable-HTTP call to {url}/{tool_name} failed: {eg}"
            ) from eg
        except Exception as exc:
            if _result is not None and _is_benign_get_405_error(exc):
                logger.debug(
                    "Suppressing benign MCP GET-405 teardown error for %s",
                    url,
                )
                return _extract_result(_result)
            raise

        return _extract_result(_result)


def _is_benign_get_405_error(exc: BaseException) -> bool:
    """
    Return True if *exc* looks like the HTTP 405 error produced when the
    mcp SDK tries to open a GET SSE stream against a Dynatrace MCP Gateway
    endpoint that only supports POST (request/response).

    We inspect the exception message for the tell-tale 405 status text rather
    than importing private mcp internals.
    """
    msg = str(exc).lower()
    return "405" in msg or "method not allowed" in msg




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
