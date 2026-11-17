"""
Bulwark Phase 6 — pytest conftest.py

Session-scoped fixtures that start 3 local FastMCP servers
(GitLab, Dynatrace, Arize) on ephemeral ports using uvicorn,
then wire shared.mcp_client.settings to point at them.

All Phase 6 MCP tests import fixtures from here.  The servers
run inside asyncio tasks for the duration of the test session
and are shut down cleanly in the fixture teardown.

Fixture hierarchy
-----------------
  local_mcp_servers   (session-scoped)
    ↳ sets settings.gitlab_mcp_url  = "http://127.0.0.1:<port>/mcp"
    ↳ sets settings.dynatrace_mcp_url
    ↳ sets settings.arize_mcp_url

  gitlab_server_state  (function-scoped)  → resets GitLab server state
  dynatrace_server_state (function-scoped) → resets Dynatrace server state
  arize_server_state   (function-scoped)  → resets Arize server state
"""

from __future__ import annotations

import os

# Must run before shared.config is imported: tests never read the developer's .env
os.environ["BULWARK_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "no-such.env")

import asyncio
import socket
import threading
import time
from typing import Generator

import pytest
import uvicorn

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _get_free_port() -> int:
    """Return an OS-allocated free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    """
    Runs a uvicorn server for one FastMCP ASGI app in a daemon thread.

    Uses a threading.Event so the main thread can wait until the server
    is ready to accept connections before tests begin.
    """

    def __init__(self, app, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self._config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",  # suppress uvicorn request logs in test output
            access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._started = threading.Event()

    def run(self) -> None:
        """Entry point: run the uvicorn event loop."""
        
        # Override the startup method to signal when ready
        original_startup = self._server.startup
        
        async def startup(*args, **kwargs):
            await original_startup(*args, **kwargs)
            self._started.set()
            
        self._server.startup = startup
        
        # Run the server
        self._server.run()

    def wait_ready(self, timeout: float = 10.0) -> None:
        """Block until the server is ready, or raise TimeoutError."""
        if not self._started.wait(timeout=timeout):
            raise TimeoutError(
                f"Local MCP server on {self.host}:{self.port} did not start in {timeout}s"
            )

    def stop(self) -> None:
        """Signal the server to shut down."""
        self._server.should_exit = True


# ──────────────────────────────────────────────────────────────────────────────
# Session fixture: start all 3 local MCP servers
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def local_mcp_servers():
    """
    Start 3 local FastMCP servers (GitLab / Dynatrace / Arize) on free ports.

    Yields a dict with port numbers:
        {"gitlab": <port>, "dynatrace": <port>, "arize": <port>}

    Also patches shared.mcp_client settings so that call_tool() routes
    to the local servers for the entire test session.
    """
    from tests.servers import gitlab_mcp_server, dynatrace_mcp_server, arize_mcp_server

    host = "127.0.0.1"
    gitlab_port = _get_free_port()
    dynatrace_port = _get_free_port()
    arize_port = _get_free_port()

    # Get the ASGI apps from each FastMCP instance
    gitlab_app = gitlab_mcp_server.mcp.streamable_http_app()
    dynatrace_app = dynatrace_mcp_server.mcp.streamable_http_app()
    arize_app = arize_mcp_server.mcp.streamable_http_app()

    # Start each server in a daemon thread
    gitlab_thread = _ServerThread(gitlab_app, host, gitlab_port)
    dynatrace_thread = _ServerThread(dynatrace_app, host, dynatrace_port)
    arize_thread = _ServerThread(arize_app, host, arize_port)

    gitlab_thread.start()
    dynatrace_thread.start()
    arize_thread.start()

    # Wait for all servers to be ready
    gitlab_thread.wait_ready()
    dynatrace_thread.wait_ready()
    arize_thread.wait_ready()

    # Patch shared.mcp_client settings to point at local servers
    from shared.config import settings

    original_gitlab = settings.gitlab_mcp_url
    original_gitlab_rest = settings.gitlab_url
    original_dynatrace = settings.dynatrace_mcp_url
    original_arize = settings.arize_mcp_url
    original_auth = settings.mcp_auth_token

    settings.gitlab_url = f"http://{host}:{gitlab_port}"  # REST routes of the fake
    settings.gitlab_mcp_url = f"http://{host}:{gitlab_port}/mcp"
    settings.dynatrace_mcp_url = f"http://{host}:{dynatrace_port}/mcp"
    settings.arize_mcp_url = f"http://{host}:{arize_port}/mcp"
    settings.mcp_auth_token = ""  # no auth for local servers

    ports = {
        "gitlab": gitlab_port,
        "dynatrace": dynatrace_port,
        "arize": arize_port,
        "host": host,
    }

    yield ports

    # Teardown: restore original settings
    settings.gitlab_mcp_url = original_gitlab
    settings.gitlab_url = original_gitlab_rest
    settings.dynatrace_mcp_url = original_dynatrace
    settings.arize_mcp_url = original_arize
    settings.mcp_auth_token = original_auth

    # Stop all servers
    gitlab_thread.stop()
    dynatrace_thread.stop()
    arize_thread.stop()


# ──────────────────────────────────────────────────────────────────────────────
# Function-scoped state reset fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def gitlab_server_state(local_mcp_servers):
    """
    Reset GitLab server tracking state before each test.

    Yields the state dict for inspection after calls.
    """
    from tests.servers.gitlab_mcp_server import state, reset_state

    reset_state()
    yield state


@pytest.fixture()
def dynatrace_server_state(local_mcp_servers):
    """
    Reset Dynatrace server state before each test.

    Yields the state dict so tests can configure it.
    """
    from tests.servers.dynatrace_mcp_server import state, reset_state

    reset_state()
    yield state


@pytest.fixture()
def arize_server_state(local_mcp_servers):
    """
    Reset Arize server state before each test.

    Yields the state dict for configuration and inspection.
    """
    from tests.servers.arize_mcp_server import state, reset_state

    reset_state()
    yield state
