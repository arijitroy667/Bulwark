"""
Phase 6: Real MCP GitLab Tool Tests.

These tests call the GitLab tool wrappers (get_mr_details, post_mr_comment,
set_pipeline_status, update_mr_approval) against a real local FastMCP server.

The full call path is exercised:
  Python wrapper → call_tool() → streamablehttp_client (SDK) → local FastMCP
  server → tool function → JSON response → parsed result

No _do_call patching. No mocks at the MCP layer.
"""

from __future__ import annotations

import pytest

from orchestrator.tools.gitlab_tool import (
    get_mr_details,
    get_mr_diff,
    post_mr_comment,
    set_pipeline_status,
    update_mr_approval,
)
from shared.mcp_client import MCPConfigError, MCPServer, call_tool
from shared.models import MergeRequestDetails


# ──────────────────────────────────────────────────────────────────────────────
# call_tool direct — verify raw MCP round-trip
# ──────────────────────────────────────────────────────────────────────────────


class TestRawCallTool:
    """Verify the full SDK round-trip using call_tool() directly."""

    @pytest.mark.asyncio
    async def test_get_merge_request_returns_dict(self, gitlab_server_state):
        """call_tool getMergeRequest returns a dict with expected fields."""
        result = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequest",
            {"projectId": "group/demo-app", "mergeRequestIid": 42},
        )
        assert isinstance(result, dict)
        assert result["iid"] == 42
        assert result["title"] == "Add N+1 regression"
        assert result["source_branch"] == "feature/n-plus-one"

    @pytest.mark.asyncio
    async def test_get_mr_diff_returns_list(self, gitlab_server_state):
        """call_tool getMergeRequestDiff returns a list of change dicts."""
        result = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequestDiff",
            {"projectId": "group/demo-app", "mergeRequestIid": 42},
        )
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "new_path" in result[0]

    @pytest.mark.asyncio
    async def test_create_note_records_body(self, gitlab_server_state):
        """call_tool createMergeRequestNote records the comment body."""
        await call_tool(
            MCPServer.GITLAB,
            "createMergeRequestNote",
            {
                "projectId": "group/demo-app",
                "mergeRequestIid": 42,
                "body": "## Bulwark Verdict\n\nAPPROVED",
            },
        )
        assert len(gitlab_server_state["comments"]) == 1
        assert "APPROVED" in gitlab_server_state["comments"][0]["body"]

    @pytest.mark.asyncio
    async def test_create_pipeline_status_records_state(self, gitlab_server_state):
        """call_tool createPipelineStatus records the status entry."""
        await call_tool(
            MCPServer.GITLAB,
            "createPipelineStatus",
            {
                "projectId": "group/demo-app",
                "sha": "abc123",
                "state": "success",
                "name": "Bulwark/production-ready",
            },
        )
        assert len(gitlab_server_state["pipeline_statuses"]) == 1
        assert gitlab_server_state["pipeline_statuses"][0]["state"] == "success"

    @pytest.mark.asyncio
    async def test_update_merge_request_records_action(self, gitlab_server_state):
        """call_tool updateMergeRequest records the approval action."""
        await call_tool(
            MCPServer.GITLAB,
            "updateMergeRequest",
            {
                "projectId": "group/demo-app",
                "mergeRequestIid": 42,
                "action": "approve",
            },
        )
        assert len(gitlab_server_state["approvals"]) == 1
        assert gitlab_server_state["approvals"][0]["action"] == "approve"


# ──────────────────────────────────────────────────────────────────────────────
# get_mr_details
# ──────────────────────────────────────────────────────────────────────────────


class TestGetMrDetails:
    """Tests for get_mr_details() — the Phase 2 GitLab MCP wrapper."""

    @pytest.mark.asyncio
    async def test_returns_merge_request_details(self, gitlab_server_state):
        """get_mr_details returns a correctly typed MergeRequestDetails."""
        details = await get_mr_details("group/demo-app", 42)
        assert isinstance(details, MergeRequestDetails)

    @pytest.mark.asyncio
    async def test_title_populated(self, gitlab_server_state):
        """Title is extracted from MR metadata."""
        details = await get_mr_details("group/demo-app", 42)
        assert details.title == "Add N+1 regression"

    @pytest.mark.asyncio
    async def test_source_branch_populated(self, gitlab_server_state):
        """source_branch is extracted correctly."""
        details = await get_mr_details("group/demo-app", 42)
        assert details.source_branch == "feature/n-plus-one"

    @pytest.mark.asyncio
    async def test_changed_files_from_diff(self, gitlab_server_state):
        """changed_files is built from the diff response."""
        details = await get_mr_details("group/demo-app", 42)
        # Default diff has src/main.py and Dockerfile
        assert "src/main.py" in details.changed_files

    @pytest.mark.asyncio
    async def test_dockerfile_detected(self, gitlab_server_state):
        """dockerfile_path is detected when Dockerfile appears in the diff."""
        details = await get_mr_details("group/demo-app", 42)
        assert details.dockerfile_path == "Dockerfile"

    @pytest.mark.asyncio
    async def test_custom_mr_metadata(self, gitlab_server_state):
        """Overriding server state changes what get_mr_details returns."""
        gitlab_server_state["mr"]["title"] = "Fix N+1 regression (batched)"
        gitlab_server_state["mr"]["source_branch"] = "fix/n-plus-one-batch"

        details = await get_mr_details("group/demo-app", 42)
        assert details.title == "Fix N+1 regression (batched)"
        assert details.source_branch == "fix/n-plus-one-batch"


# ──────────────────────────────────────────────────────────────────────────────
# get_mr_diff
# ──────────────────────────────────────────────────────────────────────────────


class TestGetMrDiff:
    """Tests for get_mr_diff()."""

    @pytest.mark.asyncio
    async def test_returns_file_diffs(self, gitlab_server_state):
        """get_mr_diff returns a list of FileDiff objects."""
        diffs = await get_mr_diff("group/demo-app", 42)
        assert isinstance(diffs, list)
        assert len(diffs) >= 1

    @pytest.mark.asyncio
    async def test_diff_paths_populated(self, gitlab_server_state):
        """FileDiff entries have old_path and new_path set."""
        diffs = await get_mr_diff("group/demo-app", 42)
        paths = [d.new_path for d in diffs]
        assert "src/main.py" in paths


# ──────────────────────────────────────────────────────────────────────────────
# post_mr_comment
# ──────────────────────────────────────────────────────────────────────────────


class TestPostMrComment:
    """Tests for post_mr_comment() — the Bulwark verdict delivery mechanism."""

    @pytest.mark.asyncio
    async def test_returns_note_id(self, gitlab_server_state):
        """post_mr_comment returns a non-None note ID on success."""
        note_id = await post_mr_comment("group/demo-app", 42, "## Verdict\nAPPROVED")
        assert note_id is not None
        assert isinstance(note_id, int)

    @pytest.mark.asyncio
    async def test_comment_body_delivered(self, gitlab_server_state):
        """The comment body is received verbatim by the server."""
        body = "## 🟢 Bulwark Verdict: APPROVED (91/100)\n\nNo regressions detected."
        await post_mr_comment("group/demo-app", 42, body)
        assert len(gitlab_server_state["comments"]) == 1
        assert gitlab_server_state["comments"][0]["body"] == body

    @pytest.mark.asyncio
    async def test_multiple_comments_accumulate(self, gitlab_server_state):
        """Multiple post_mr_comment calls append to the server comment log."""
        await post_mr_comment("group/demo-app", 42, "Comment 1")
        await post_mr_comment("group/demo-app", 42, "Comment 2")
        assert len(gitlab_server_state["comments"]) == 2

    @pytest.mark.asyncio
    async def test_note_ids_are_unique(self, gitlab_server_state):
        """Each comment gets a unique note ID."""
        id1 = await post_mr_comment("group/demo-app", 42, "First")
        id2 = await post_mr_comment("group/demo-app", 42, "Second")
        assert id1 != id2


# ──────────────────────────────────────────────────────────────────────────────
# set_pipeline_status
# ──────────────────────────────────────────────────────────────────────────────


class TestSetPipelineStatus:
    """Tests for set_pipeline_status() — the MR gate mechanism."""

    @pytest.mark.asyncio
    async def test_success_state_posted(self, gitlab_server_state):
        """APPROVED verdict → pipeline status 'success'."""
        await set_pipeline_status(
            project_id="group/demo-app",
            sha="abc123def456",
            state="success",
            description="APPROVED (91/100) — No regressions detected",
        )
        statuses = gitlab_server_state["pipeline_statuses"]
        assert len(statuses) == 1
        assert statuses[0]["state"] == "success"

    @pytest.mark.asyncio
    async def test_failed_state_posted(self, gitlab_server_state):
        """BLOCKED verdict → pipeline status 'failed'."""
        await set_pipeline_status(
            project_id="group/demo-app",
            sha="abc123def456",
            state="failed",
            description="BLOCKED (43/100) — N+1 regression detected",
        )
        statuses = gitlab_server_state["pipeline_statuses"]
        assert statuses[0]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_status_name_is_bulwark(self, gitlab_server_state):
        """Pipeline status name must be 'Bulwark/production-ready'."""
        await set_pipeline_status("group/demo-app", "sha1", "success", "OK")
        statuses = gitlab_server_state["pipeline_statuses"]
        assert statuses[0]["name"] == "Bulwark/production-ready"


# ──────────────────────────────────────────────────────────────────────────────
# update_mr_approval
# ──────────────────────────────────────────────────────────────────────────────


class TestUpdateMrApproval:
    """Tests for update_mr_approval()."""

    @pytest.mark.asyncio
    async def test_approve_action_sent(self, gitlab_server_state):
        """Approving sends action='approve'."""
        await update_mr_approval("group/demo-app", 42, approve=True)
        assert gitlab_server_state["approvals"][0]["action"] == "approve"

    @pytest.mark.asyncio
    async def test_unapprove_action_sent(self, gitlab_server_state):
        """Unapproving sends action='unapprove'."""
        await update_mr_approval("group/demo-app", 42, approve=False)
        assert gitlab_server_state["approvals"][0]["action"] == "unapprove"


# ──────────────────────────────────────────────────────────────────────────────
# Config error when MCP URL is not set
# ──────────────────────────────────────────────────────────────────────────────


class TestMcpConfigError:
    """Verify MCPConfigError is raised when GitLab URL is missing."""

    @pytest.mark.asyncio
    async def test_raises_config_error_when_url_empty(self, monkeypatch):
        """Clearing gitlab_mcp_url raises MCPConfigError before any network call."""
        from shared.config import settings

        monkeypatch.setattr(settings, "gitlab_mcp_url", "")
        with pytest.raises(MCPConfigError, match="GITLAB_MCP_URL"):
            await call_tool(MCPServer.GITLAB, "getMergeRequest", {"projectId": "x", "mergeRequestIid": 1})
