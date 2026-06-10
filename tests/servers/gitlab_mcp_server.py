"""
Local GitLab MCP server for Phase 6 tests.

Implements real MCP tools over Streamable-HTTP using FastMCP.
All tool responses are controlled via the `state` dict so tests
can configure what the server returns and inspect what was posted.

Tools implemented:
  getMergeRequest        → returns state["mr"]
  getMergeRequestDiff    → returns state["diff"]
  getRepositoryFiles     → returns state["files"] for requested paths
  createMergeRequestNote → appends to state["comments"], returns note id
  createPipelineStatus   → appends to state["pipeline_statuses"]
  updateMergeRequest     → appends to state["approvals"]
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

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
# FastMCP server
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("gitlab-local")


@mcp.tool()
def getMergeRequest(projectId: str, mergeRequestIid: int) -> str:
    """Return MR metadata."""
    mr = dict(state["mr"])
    mr["iid"] = mergeRequestIid
    return json.dumps(mr)


@mcp.tool()
def getMergeRequestDiff(projectId: str, mergeRequestIid: int) -> str:
    """Return MR diff changes."""
    return json.dumps(state["diff"])


@mcp.tool()
def getRepositoryFiles(
    projectId: str,
    ref: str,
    filePaths: list[str] | None = None,
) -> str:
    """Return file contents for the requested paths."""
    paths = filePaths or []
    result: dict[str, str] = {}
    for path in paths:
        content = state["files"].get(path, "")
        result[path] = content
    return json.dumps(result)


@mcp.tool()
def createMergeRequestNote(
    projectId: str,
    mergeRequestIid: int,
    body: str,
) -> str:
    """Post a comment on an MR. Records the body for test inspection."""
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
def createPipelineStatus(
    projectId: str,
    sha: str,
    state: str,
    name: str = "Bulwark/production-ready",
    description: str = "",
    targetUrl: str = "",
) -> str:
    """Set a commit pipeline status. Records the call for test inspection."""
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
def updateMergeRequest(
    projectId: str,
    mergeRequestIid: int,
    action: str = "approve",
) -> str:
    """Approve or request changes on an MR."""
    state["approvals"].append(
        {"mr_iid": mergeRequestIid, "action": action}
    )
    return json.dumps({"iid": mergeRequestIid, "action": action})
