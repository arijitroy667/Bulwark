"""
Local GitLab MCP server for Phase 6 tests and E2E Demo proxy.

Implements real MCP tools over Streamable-HTTP using FastMCP.
If settings.gitlab_pat is configured with a real token (starts with glpat-)
and demo mode is active, the server proxies all calls to the real GitLab API.
Otherwise, it returns mock data from the state dictionary (fallback/test mode).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from shared.config import settings
from shared.demo_mode import is_demo_mode_active

logger = logging.getLogger("bulwark.gitlab-mcp-server")

# ──────────────────────────────────────────────────────────────────────────────
# Shared state — tests read/write this to control server responses
# ──────────────────────────────────────────────────────────────────────────────

state: dict[str, Any] = {
    "mr": {
        "iid": 42,
        "title": "Add N+1 regression",
        "description": "This MR introduces an N+1 query pattern",
        "source_branch": "feature/n-plus-one",
        "target_branch": "main",
        "author": {"username": "testuser"},
        "head_sha": "abc123def456abc123def456abc123def456",
        "state": "opened",
        "web_url": "https://gitlab.example.com/group/demo-app/-/merge_requests/42",
    },
    "diff": [
        {
            "new_path": "src/main.py",
            "old_path": "src/main.py",
            "diff": "@@ -10,5 +10,8 @@\n+for user in users:\n+    prefs = db.query(user.id)\n",
            "new_file": False,
            "deleted_file": False,
        },
        {
            "new_path": "Dockerfile",
            "old_path": "Dockerfile",
            "diff": "@@ -1 +1 @@\n-FROM python:3.11\n+FROM python:3.13",
            "new_file": False,
            "deleted_file": False,
        },
    ],
    "files": {
        "src/main.py": "for user in users:\n    prefs = db.query(user.id)\n",
    },
    "comments": [],          # populated by createMergeRequestNote calls
    "pipeline_statuses": [], # populated by createPipelineStatus calls
    "approvals": [],         # populated by updateMergeRequest calls
    # Incrementing ID for note/status responses
    "_next_note_id": 1001,
}


def reset_state() -> None:
    """Reset mutable tracking state between tests."""
    state["comments"].clear()
    state["pipeline_statuses"].clear()
    state["approvals"].clear()
    state["_next_note_id"] = 1001


# ──────────────────────────────────────────────────────────────────────────────
# Helper for proxying to real GitLab API
# ──────────────────────────────────────────────────────────────────────────────

async def _get_active_pat() -> str:
    """Fetch the active GitLab PAT from MongoDB, falling back to settings."""
    try:
        from shared.db import get_gitlab_pat
        return await get_gitlab_pat()
    except Exception:
        pass
    return settings.gitlab_pat


async def _is_real_token() -> bool:
    """Check if settings has a real GitLab PAT configured and demo mode is active."""
    if not await is_demo_mode_active():
        return False
    pat = await _get_active_pat()
    pat = pat.strip() if pat else ""
    return bool(pat and (pat.startswith("glpat-") or len(pat) > 10))


async def _call_gitlab_api(method: str, path: str, json_data: Any = None) -> Any:
    """Proxy HTTP call to the real GitLab API."""
    pat = await _get_active_pat()
    url = f"{settings.gitlab_url.rstrip('/')}/api/v4/{path}"
    headers = {
        "PRIVATE-TOKEN": pat,
        "Accept": "application/json",
    }
    logger.info("Proxying %s to real GitLab: %s", method, url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        elif method.upper() == "POST":
            resp = await client.post(url, headers=headers, json=json_data)
        elif method.upper() == "PUT":
            resp = await client.put(url, headers=headers, json=json_data)
        else:
            return None
        
        if resp.status_code >= 400:
            logger.warning("GitLab API error %d: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()


async def _call_gitlab_api_raw(path: str) -> str | None:
    """Fetch raw file content from GitLab."""
    pat = await _get_active_pat()
    url = f"{settings.gitlab_url.rstrip('/')}/api/v4/{path}"
    headers = {"PRIVATE-TOKEN": pat}
    logger.info("Proxying GET RAW to real GitLab: %s", url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            resp.raise_for_status()
        return resp.text


# ──────────────────────────────────────────────────────────────────────────────
# FastMCP server
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("gitlab-local")


@mcp.tool()
async def getMergeRequest(projectId: str, mergeRequestIid: int) -> str:
    """Return MR metadata."""
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        path = f"projects/{encoded_project}/merge_requests/{mergeRequestIid}"
        try:
            mr_data = await _call_gitlab_api("GET", path)
            return json.dumps(mr_data)
        except Exception as e:
            logger.error("Failed to proxy getMergeRequest: %s", e)
            raise

    # Fallback to mock state
    mr = dict(state["mr"])
    mr["iid"] = mergeRequestIid
    return json.dumps(mr)


@mcp.tool()
async def getMergeRequestDiff(projectId: str, mergeRequestIid: int) -> str:
    """Return MR diff changes."""
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        path = f"projects/{encoded_project}/merge_requests/{mergeRequestIid}/changes"
        try:
            diff_data = await _call_gitlab_api("GET", path)
            return json.dumps(diff_data)
        except Exception as e:
            logger.error("Failed to proxy getMergeRequestDiff: %s", e)
            raise

    # Fallback to mock state
    return json.dumps(state["diff"])


@mcp.tool()
async def getRepositoryFiles(
    projectId: str,
    ref: str,
    filePaths: list[str] | None = None,
) -> str:
    """Return file contents for the requested paths."""
    paths = filePaths or []
    result: dict[str, str] = {}
    
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        for path in paths:
            encoded_path = urllib.parse.quote_plus(path)
            api_path = f"projects/{encoded_project}/repository/files/{encoded_path}/raw?ref={ref}"
            try:
                content = await _call_gitlab_api_raw(api_path)
                if content is not None:
                    result[path] = content
            except Exception as e:
                logger.warning("Failed to proxy getRepositoryFiles for %s: %s", path, e)
        return json.dumps(result)

    # Fallback to mock state
    for path in paths:
        content = state["files"].get(path, "")
        result[path] = content
    return json.dumps(result)


@mcp.tool()
async def createMergeRequestNote(
    projectId: str,
    mergeRequestIid: int,
    body: str,
) -> str:
    """Post a comment on an MR. Records the body for test inspection."""
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        path = f"projects/{encoded_project}/merge_requests/{mergeRequestIid}/notes"
        try:
            note_data = await _call_gitlab_api("POST", path, {"body": body})
            return json.dumps(note_data)
        except Exception as e:
            logger.error("Failed to proxy createMergeRequestNote: %s", e)
            raise

    # Fallback to mock state
    note_id = state["_next_note_id"]
    state["_next_note_id"] += 1
    state["comments"].append(
        {
            "id": note_id,
            "body": body,
            "project_id": projectId,
            "mr_iid": mergeRequestIid,
        }
    )
    return json.dumps({"id": note_id, "body": body})


@mcp.tool()
async def createPipelineStatus(
    projectId: str,
    sha: str,
    state: str,
    name: str = "Bulwark/production-ready",
    description: str = "",
    targetUrl: str = "",
) -> str:
    """Set a commit pipeline status. Records the call for test inspection."""
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        path = f"projects/{encoded_project}/statuses/{sha}"
        payload = {
            "state": state,
            "name": name,
            "description": description,
        }
        if targetUrl:
            payload["target_url"] = targetUrl
            
        try:
            status_data = await _call_gitlab_api("POST", path, payload)
            return json.dumps(status_data)
        except Exception as e:
            logger.error("Failed to proxy createPipelineStatus: %s", e)
            raise

    # Note: parameter shadowing `state` dict — use a local name
    import tests.servers.gitlab_mcp_server as _self_module  # type: ignore[import]

    status_record = {
        "sha": sha,
        "state": state,
        "name": name,
        "description": description,
    }
    _self_module.state["pipeline_statuses"].append(status_record)
    return json.dumps({"id": 9001, "sha": sha, "status": state})


@mcp.tool()
async def updateMergeRequest(
    projectId: str,
    mergeRequestIid: int,
    action: str = "approve",
) -> str:
    """Approve or request changes on an MR."""
    if await _is_real_token():
        encoded_project = urllib.parse.quote_plus(projectId)
        path = f"projects/{encoded_project}/merge_requests/{mergeRequestIid}/{action}"
        try:
            approval_data = await _call_gitlab_api("POST", path)
            return json.dumps(approval_data)
        except Exception as e:
            logger.error("Failed to proxy updateMergeRequest (%s): %s", action, e)
            raise

    # Fallback to mock state
    state["approvals"].append(
        {"mr_iid": mergeRequestIid, "action": action}
    )
    return json.dumps({"iid": mergeRequestIid, "action": action})
