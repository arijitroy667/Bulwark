#!/bin/bash
# Phase 6: Start all local FastMCP test servers for development

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "Starting GitLab FastMCP Server on port 8001..."
.venv/bin/uvicorn tests.servers.gitlab_mcp_server:mcp.streamable_http_app --host 127.0.0.1 --port 8001 &
GITLAB_PID=$!

echo "Starting Dynatrace FastMCP Server on port 8002..."
.venv/bin/uvicorn tests.servers.dynatrace_mcp_server:mcp.streamable_http_app --host 127.0.0.1 --port 8002 &
DYNATRACE_PID=$!

echo "Starting Arize FastMCP Server on port 8003..."
.venv/bin/uvicorn tests.servers.arize_mcp_server:mcp.streamable_http_app --host 127.0.0.1 --port 8003 &
ARIZE_PID=$!

echo ""
echo "All local FastMCP servers started."
echo "  GitLab MCP:    http://127.0.0.1:8001/mcp"
echo "  Dynatrace MCP: http://127.0.0.1:8002/mcp"
echo "  Arize MCP:     http://127.0.0.1:8003/mcp"
echo ""
echo "Press Ctrl+C to stop."

trap "echo 'Stopping servers...'; kill $GITLAB_PID $DYNATRACE_PID $ARIZE_PID; exit 0" SIGINT SIGTERM

wait
