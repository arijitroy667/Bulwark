"""
Bulwark — GitLab MCP Integration Tool (v3.0).

Wrapper functions over the GitLab REST API for all operations in PRD §6.1:
  - getMergeRequest / getCommits / getRepositoryFiles
  - createMergeRequestNote  (post verdict comment)
  - createPipelineStatus    (set Bulwark/production-ready gate)
  - updateMergeRequest      (approve or request changes)

LLM component detection has been moved to orchestrator/llm_detector.py.
The v1 string-matching approach (`system_prompt` etc.) is removed — it
produced false positives in the majority of non-LLM codebases.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from shared.config import settings
from shared.models import FileDiff, MergeRequestDetails

logger = logging.getLogger("bulwark.gitlab")

# Documentation file extensions (for doc-only MR detection, FR-005)
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".docx"}
DOC_DIRECTORIES = {"docs/", "doc/", "documentation/"}


# ──────────────────────────────────────────────
# HTTP client
# ──────────────────────────────────────────────


def _get_headers() -> dict[str, str]:
    """Build GitLab API request headers with authentication."""
    return {
        "PRIVATE-TOKEN": settings.gitlab_pat,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_url(path: str) -> str:
    """Build a full GitLab API URL."""
    return f"{settings.gitlab_api_url}{path}"


def _encode_project_id(project_id: str) -> str:
    """URL-encode a project path for the GitLab API (slashes → %2F)."""
    return project_id.replace("/", "%2F")


# ──────────────────────────────────────────────
# MR Operations
# ──────────────────────────────────────────────


async def get_mr_details(project_id: str, mr_id: int) -> MergeRequestDetails:
    """
    Fetch full merge request metadata from GitLab.

    Returns a MergeRequestDetails object with title, branches, author,
    changed files, and whether LLM components are detected.
    """
    encoded_project = _encode_project_id(project_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch MR metadata
        response = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}"),
            headers=_get_headers(),
        )
        response.raise_for_status()
        mr_data = response.json()

        # Fetch MR changes (includes file diffs)
        changes_response = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/changes"),
            headers=_get_headers(),
        )
        changes_response.raise_for_status()
        changes_data = changes_response.json()

    # Extract changed files
    changed_files = [
        change.get("new_path", change.get("old_path", ""))
        for change in changes_data.get("changes", [])
    ]

    # Detect Dockerfile
    dockerfile_path = None
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

    # LLM detection is performed separately via orchestrator/llm_detector.py
    # (two-stage AST scan — not string matching, which produces false positives)
    return MergeRequestDetails(
        mr_id=mr_data.get("iid", mr_id),
        title=mr_data.get("title", ""),
        description=mr_data.get("description", ""),
        source_branch=mr_data.get("source_branch", ""),
        target_branch=mr_data.get("target_branch", ""),
        author=mr_data.get("author", {}).get("username", "unknown"),
        changed_files=changed_files,
        dockerfile_path=dockerfile_path,
        ci_config_path=mr_data.get("ci_config_path", ".gitlab-ci.yml") or ".gitlab-ci.yml",
        has_llm_components=False,  # Set later by llm_detector
        head_sha=mr_data.get("sha", ""),
    )


async def get_mr_diff(project_id: str, mr_id: int) -> list[FileDiff]:
    """Fetch the full code diff for a merge request."""
    encoded_project = _encode_project_id(project_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/changes"),
            headers=_get_headers(),
        )
        response.raise_for_status()
        data = response.json()

    diffs = []
    for change in data.get("changes", []):
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
    Read a file's content from the GitLab repository at a specific ref.
    Returns None if the file does not exist.
    """
    encoded_project = _encode_project_id(project_id)
    encoded_filepath = filepath.replace("/", "%2F")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            _api_url(
                f"/projects/{encoded_project}/repository/files/{encoded_filepath}/raw"
            ),
            params={"ref": ref},
            headers=_get_headers(),
        )
        if response.status_code == 404:
            logger.debug("File not found: %s @ %s", filepath, ref)
            return None
        response.raise_for_status()
        return response.text


async def post_mr_comment(project_id: str, mr_id: int, body: str) -> int | None:
    """
    Post a comment (note) on a GitLab merge request.
    Returns the note ID, or None on failure.
    """
    encoded_project = _encode_project_id(project_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/notes"),
            headers=_get_headers(),
            json={"body": body},
        )
        response.raise_for_status()
        note_data = response.json()

    note_id = note_data.get("id")
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
    Set a commit status check on a GitLab commit.

    Creates the 'Bulwark/production-ready' pipeline status that gates the MR.

    Args:
        project_id: GitLab project path (e.g., "group/project")
        sha: The commit SHA to set status on
        state: One of "pending", "running", "success", "failed", "canceled"
        description: Human-readable status description
        target_url: Optional URL to the full simulation report
    """
    encoded_project = _encode_project_id(project_id)

    payload = {
        "state": state,
        "name": "Bulwark/production-ready",
        "description": description,
        "context": "Bulwark/production-ready",
    }
    if target_url:
        payload["target_url"] = target_url

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _api_url(f"/projects/{encoded_project}/statuses/{sha}"),
            headers=_get_headers(),
            json=payload,
        )
        response.raise_for_status()

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
    Approve or unapprove a merge request.

    Args:
        approve: True to approve, False to unapprove
    """
    encoded_project = _encode_project_id(project_id)
    endpoint = "approve" if approve else "unapprove"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _api_url(
                f"/projects/{encoded_project}/merge_requests/{mr_id}/{endpoint}"
            ),
            headers=_get_headers(),
        )
        # 401 can happen if the PAT doesn't have approval rights — log but don't fail
        if response.status_code in (401, 403):
            logger.warning(
                "Cannot %s MR !%d — insufficient permissions (status=%d)",
                endpoint,
                mr_id,
                response.status_code,
            )
            return
        response.raise_for_status()

    logger.info("MR !%d %sd", mr_id, endpoint)


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
    Fetch the content of multiple files concurrently.
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
