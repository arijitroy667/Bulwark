"""
Bulwark — GitLab MCP Integration Tool (Phase 2, PLAN.md §Phase 2).

All operations go through the GitLab MCP server via ``shared.mcp_client``.
Python function signatures are unchanged from Phase 1 so that ``agent.py``
requires no modifications.

MCP operations implemented (per PRD §6.1):
  getMergeRequest       → get_mr_details()
  getMergeRequestDiff   → get_mr_diff()
  getRepositoryFiles    → get_file_content() / get_file_contents_batch()
  createMergeRequestNote → post_mr_comment()
  createPipelineStatus  → set_pipeline_status()
  updateMergeRequest    → update_mr_approval()

LLM component detection has been moved to orchestrator/llm_detector.py.
The v1 string-matching approach (`system_prompt` etc.) is removed — it
produced false positives in the majority of non-LLM codebases.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from shared.mcp_client import MCPCallError, MCPConfigError, MCPServer, call_tool
from shared.models import FileDiff, MergeRequestDetails

logger = logging.getLogger("bulwark.gitlab")

# Documentation file extensions (for doc-only MR detection, FR-005)
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".docx"}
DOC_DIRECTORIES = {"docs/", "doc/", "documentation/"}


# ──────────────────────────────────────────────
# MR Operations
# ──────────────────────────────────────────────


async def get_mr_details(project_id: str, mr_id: int) -> MergeRequestDetails:
    """
    Fetch full merge request metadata from GitLab via MCP.

    Returns a MergeRequestDetails object with title, branches, author,
    changed files, and whether LLM components are detected.
    """
    try:
        mr_data = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequest",
            {"projectId": project_id, "mergeRequestIid": mr_id},
        )

        changes_data = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequestDiff",
            {"projectId": project_id, "mergeRequestIid": mr_id},
        )
    except (MCPConfigError, MCPCallError) as exc:
        logger.error("GitLab MCP error fetching MR details: %s", exc)
        raise

    # Normalize response — MCP servers may return different shapes
    if isinstance(mr_data, list) and mr_data:
        mr_data = mr_data[0]
    if not isinstance(mr_data, dict):
        mr_data = {}

    changes = changes_data if isinstance(changes_data, list) else (
        changes_data.get("changes", []) if isinstance(changes_data, dict) else []
    )

    # Extract changed files
    changed_files = [
        change.get("new_path", change.get("old_path", ""))
        for change in changes
    ]

    # Detect Dockerfile
    dockerfile_path: str | None = None
    for f in changed_files:
        if Path(f).name.lower() == "dockerfile":
            dockerfile_path = f
            break
    # If not in changed files, check default location
    if dockerfile_path is None:
        dockerfile_path = await _find_file_in_repo(
            project_id,
            mr_data.get("source_branch", ""),
            "Dockerfile",
        )

    return MergeRequestDetails(
        mr_id=mr_data.get("iid", mr_id),
        title=mr_data.get("title", ""),
        description=mr_data.get("description", ""),
        source_branch=mr_data.get("source_branch", ""),
        target_branch=mr_data.get("target_branch", ""),
        author=mr_data.get("author", {}).get("username", "unknown")
        if isinstance(mr_data.get("author"), dict)
        else mr_data.get("author", "unknown"),
        changed_files=changed_files,
        dockerfile_path=dockerfile_path,
        ci_config_path=mr_data.get("ci_config_path", ".gitlab-ci.yml") or ".gitlab-ci.yml",
        has_llm_components=False,  # Set later by llm_detector
        head_sha=mr_data.get("sha", ""),
    )


async def get_mr_diff(project_id: str, mr_id: int) -> list[FileDiff]:
    """Fetch the full code diff for a merge request via MCP."""
    try:
        data = await call_tool(
            MCPServer.GITLAB,
            "getMergeRequestDiff",
            {"projectId": project_id, "mergeRequestIid": mr_id},
        )
    except (MCPConfigError, MCPCallError) as exc:
        logger.error("GitLab MCP error fetching diff: %s", exc)
        raise

    changes = data if isinstance(data, list) else (
        data.get("changes", []) if isinstance(data, dict) else []
    )

    diffs = []
    for change in changes:
        diffs.append(FileDiff(
            old_path=change.get("old_path", ""),
            new_path=change.get("new_path", ""),
            diff=change.get("diff", ""),
            new_file=change.get("new_file", False),
            renamed_file=change.get("renamed_file", False),
            deleted_file=change.get("deleted_file", False),
        ))

    logger.info("Fetched %d file diffs for MR !%d", len(diffs), mr_id)
    return diffs


async def get_file_content(project_id: str, ref: str, filepath: str) -> str | None:
    """
    Read a file's content from the GitLab repository at a specific ref via MCP.
    Returns None if the file does not exist.
    """
    try:
        result = await call_tool(
            MCPServer.GITLAB,
            "getRepositoryFiles",
            {"projectId": project_id, "ref": ref, "filePath": filepath},
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        # File not found signals return None rather than propagating
        if "not found" in str(exc).lower() or "404" in str(exc):
            logger.debug("File not found via MCP: %s @ %s", filepath, ref)
            return None
        logger.warning("GitLab MCP error fetching file %s: %s", filepath, exc)
        return None

    if result is None:
        return None

    # MCP server may return the file content directly or wrapped
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("content") or result.get("blob") or result.get("raw_content")
    return None


async def post_mr_comment(project_id: str, mr_id: int, body: str) -> int | None:
    """
    Post a comment (note) on a GitLab merge request via MCP.
    Returns the note ID, or None on failure.
    """
    try:
        result = await call_tool(
            MCPServer.GITLAB,
            "createMergeRequestNote",
            {
                "projectId": project_id,
                "mergeRequestIid": mr_id,
                "body": body,
            },
        )
    except (MCPConfigError, MCPCallError) as exc:
        logger.error("GitLab MCP error posting comment: %s", exc)
        return None

    note_id: int | None = None
    if isinstance(result, dict):
        note_id = result.get("id")
    elif isinstance(result, int):
        note_id = result

    logger.info("Posted comment on MR !%d: note_id=%s", mr_id, note_id)
    return note_id


async def set_pipeline_status(
    project_id: str,
    sha: str,
    state: str,
    description: str,
    target_url: str = "",
) -> None:
    """
    Set a commit status check on a GitLab commit via MCP.

    Creates the 'Bulwark/production-ready' pipeline status that gates the MR.

    Args:
        project_id: GitLab project path (e.g., "group/project")
        sha: The commit SHA to set status on
        state: One of "pending", "running", "success", "failed", "canceled"
        description: Human-readable status description
        target_url: Optional URL to the full simulation report
    """
    payload: dict = {
        "projectId": project_id,
        "sha": sha,
        "state": state,
        "name": "Bulwark/production-ready",
        "description": description,
        "context": "Bulwark/production-ready",
    }
    if target_url:
        payload["targetUrl"] = target_url

    try:
        await call_tool(MCPServer.GITLAB, "createPipelineStatus", payload)
    except (MCPConfigError, MCPCallError) as exc:
        logger.error("GitLab MCP error setting pipeline status: %s", exc)
        raise

    logger.info(
        "Set pipeline status for %s: %s (%s)",
        sha[:8],
        state,
        description,
    )


async def update_mr_approval(
    project_id: str,
    mr_id: int,
    approve: bool,
) -> None:
    """
    Approve or unapprove a merge request via MCP.

    Args:
        approve: True to approve, False to unapprove
    """
    try:
        await call_tool(
            MCPServer.GITLAB,
            "updateMergeRequest",
            {
                "projectId": project_id,
                "mergeRequestIid": mr_id,
                "action": "approve" if approve else "unapprove",
            },
        )
    except MCPConfigError:
        raise
    except MCPCallError as exc:
        # Approval rights may not be available — log but don't fail
        if "403" in str(exc) or "401" in str(exc) or "insufficient" in str(exc).lower():
            logger.warning(
                "Cannot %s MR !%d — insufficient permissions: %s",
                "approve" if approve else "unapprove",
                mr_id,
                exc,
            )
            return
        raise

    logger.info("MR !%d %sd", mr_id, "approve" if approve else "unapprove")


# ──────────────────────────────────────────────
# File content batch fetch (for AST-based LLM detection)
# ──────────────────────────────────────────────


async def get_file_contents_batch(
    project_id: str,
    ref: str,
    filepaths: list[str],
    concurrency: int = 5,
) -> dict[str, str]:
    """
    Fetch the content of multiple files concurrently via MCP.
    Used by the LLM detector for Stage 2 AST scanning.

    Returns dict of {filepath: content}. Files that 404 are excluded.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, str] = {}

    async def fetch_one(fp: str) -> None:
        async with semaphore:
            content = await get_file_content(project_id, ref, fp)
            if content is not None:
                results[fp] = content

    await asyncio.gather(*[fetch_one(fp) for fp in filepaths])
    logger.debug("Fetched %d/%d files for AST scan", len(results), len(filepaths))
    return results


def is_doc_only_mr(changed_files: list[str]) -> bool:
    """Check if all changed files are documentation files."""
    if not changed_files:
        return False
    for filepath in changed_files:
        filepath_lower = filepath.lower()
        if any(filepath_lower.startswith(d) for d in DOC_DIRECTORIES):
            continue
        ext = Path(filepath_lower).suffix
        if ext in DOC_EXTENSIONS:
            continue
        return False
    return True


def generate_diff_summary(diffs: list[FileDiff]) -> str:
    """
    Generate a human-readable summary of all code changes in an MR.
    Used as input to the Gemini reasoning step.
    """
    summary_parts = []
    for diff in diffs:
        action = "Modified"
        if diff.new_file:
            action = "Added"
        elif diff.deleted_file:
            action = "Deleted"
        elif diff.renamed_file:
            action = f"Renamed (from {diff.old_path})"

        # Count additions and deletions
        additions = len(re.findall(r"^\+(?!\+\+)", diff.diff, re.MULTILINE))
        deletions = len(re.findall(r"^-(?!--)", diff.diff, re.MULTILINE))

        summary_parts.append(
            f"- {action}: `{diff.new_path}` (+{additions}/-{deletions} lines)"
        )

    return "\n".join(summary_parts)


async def _find_file_in_repo(
    project_id: str,
    ref: str,
    filename: str,
) -> str | None:
    """Search for a file by name in the repository root."""
    content = await get_file_content(project_id, ref, filename)
    if content is not None:
        return filename
    return None


def identify_services_from_diff(
    changed_files: list[str],
    ci_config: str | None = None,
) -> list[str]:
    """
    Identify which services are modified by an MR based on the changed file paths.

    Heuristic: looks for service directory patterns like services/<name>/ or
    apps/<name>/ or packages/<name>/.
    """
    services = set()
    service_path_patterns = [
        re.compile(r"^services/([^/]+)/"),
        re.compile(r"^apps/([^/]+)/"),
        re.compile(r"^packages/([^/]+)/"),
        re.compile(r"^src/([^/]+)/"),
    ]

    for filepath in changed_files:
        for pattern in service_path_patterns:
            match = pattern.match(filepath)
            if match:
                services.add(match.group(1))
                break
        else:
            # If no service pattern matches, use the repo root as the service
            if not services:
                services.add("default")

    return list(services)
