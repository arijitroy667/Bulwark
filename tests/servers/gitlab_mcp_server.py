"""
Local fake GitLab for tests.

Serves the same in-memory state two ways:
  * MCP tools over Streamable-HTTP (FastMCP) — raw MCP round-trip tests
  * GitLab REST v4 routes (/api/v4/...) — used by orchestrator.tools.gitlab_tool,
    which talks to GitLab over REST
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

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
    "comments": [],          # populated by note creation
    "pipeline_statuses": [], # populated by commit status creation
    "approvals": [],         # populated by approve/unapprove
    "_next_note_id": 1001,
}


def reset_state() -> None:
    """Reset mutable tracking state between tests."""
    state["comments"].clear()
    state["pipeline_statuses"].clear()
    state["approvals"].clear()
    state["_next_note_id"] = 1001


def _add_comment(project_id: str, iid: int, body: str) -> dict[str, Any]:
    note_id = state["_next_note_id"]
    state["_next_note_id"] += 1
    state["comments"].append({"id": note_id, "body": body, "project_id": project_id, "mr_iid": iid})
    return {"id": note_id, "body": body}


def _add_status(sha: str, status: str, name: str, description: str) -> dict[str, Any]:
    state["pipeline_statuses"].append(
        {"sha": sha, "state": status, "name": name, "description": description}
    )
    return {"id": 9001, "sha": sha, "status": status}


def _mr(iid: int) -> dict[str, Any]:
    mr = dict(state["mr"])
    mr["iid"] = iid
    mr["sha"] = mr.get("head_sha", "")  # REST field name
    return mr


# ──────────────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("gitlab-local")


@mcp.tool()
async def getMergeRequest(projectId: str, mergeRequestIid: int) -> str:
    """Return MR metadata."""
    return json.dumps(_mr(mergeRequestIid))


@mcp.tool()
async def getMergeRequestDiff(projectId: str, mergeRequestIid: int) -> str:
    """Return MR diff changes."""
    return json.dumps(state["diff"])


@mcp.tool()
async def getRepositoryFiles(projectId: str, ref: str, filePaths: list[str] | None = None) -> str:
    """Return file contents for the requested paths."""
    return json.dumps({p: state["files"].get(p, "") for p in filePaths or []})


@mcp.tool()
async def createMergeRequestNote(projectId: str, mergeRequestIid: int, body: str) -> str:
    """Post a comment on an MR. Records the body for test inspection."""
    return json.dumps(_add_comment(projectId, mergeRequestIid, body))


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
    return json.dumps(_add_status(sha, state, name, description))


@mcp.tool()
async def updateMergeRequest(projectId: str, mergeRequestIid: int, action: str = "approve") -> str:
    """Approve or request changes on an MR."""
    state["approvals"].append({"mr_iid": mergeRequestIid, "action": action})
    return json.dumps({"iid": mergeRequestIid, "action": action})


# ──────────────────────────────────────────────────────────────────────────────
# REST v4 routes (subset used by gitlab_tool)
# ──────────────────────────────────────────────────────────────────────────────

_P = "/api/v4/projects/{project:path}"


@mcp.custom_route(_P + "/merge_requests/{iid}", methods=["GET"])
async def rest_get_mr(request: Request) -> JSONResponse:
    return JSONResponse(_mr(int(request.path_params["iid"])))


@mcp.custom_route(_P + "/merge_requests/{iid}/changes", methods=["GET"])
async def rest_get_changes(request: Request) -> JSONResponse:
    return JSONResponse({**_mr(int(request.path_params["iid"])), "changes": state["diff"]})


@mcp.custom_route(_P + "/repository/files/{file:path}/raw", methods=["GET"])
async def rest_get_file(request: Request) -> PlainTextResponse:
    content = state["files"].get(request.path_params["file"])
    if content is None:
        return PlainTextResponse("404 File Not Found", status_code=404)
    return PlainTextResponse(content)


@mcp.custom_route(_P + "/merge_requests/{iid}/notes", methods=["POST"])
async def rest_post_note(request: Request) -> JSONResponse:
    body = (await request.json()).get("body", "")
    note = _add_comment(request.path_params["project"], int(request.path_params["iid"]), body)
    return JSONResponse(note, status_code=201)


@mcp.custom_route(_P + "/statuses/{sha}", methods=["POST"])
async def rest_post_status(request: Request) -> JSONResponse:
    data = await request.json()
    status = _add_status(
        request.path_params["sha"], data.get("state", ""), data.get("name", ""), data.get("description", "")
    )
    return JSONResponse(status, status_code=201)


@mcp.custom_route(_P + "/merge_requests/{iid}/approve", methods=["POST"])
async def rest_approve(request: Request) -> JSONResponse:
    state["approvals"].append({"mr_iid": int(request.path_params["iid"]), "action": "approve"})
    return JSONResponse({"iid": int(request.path_params["iid"])}, status_code=201)


@mcp.custom_route(_P + "/merge_requests/{iid}/unapprove", methods=["POST"])
async def rest_unapprove(request: Request) -> JSONResponse:
    state["approvals"].append({"mr_iid": int(request.path_params["iid"]), "action": "unapprove"})
    return JSONResponse({"iid": int(request.path_params["iid"])}, status_code=201)
