"""
Bulwark — GitLab REST Integration Tool.

This uses direct REST API calls instead of MCP, to bypass the missing GitLab MCP server.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from pathlib import Path

import httpx

from shared.config import settings
from shared.models import FileDiff, MergeRequestDetails

logger = logging.getLogger("bulwark.gitlab")

DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".docx"}
DOC_DIRECTORIES = {"docs/", "doc/", "documentation/"}

def _encode_project_id(project_id: str) -> str:
    return urllib.parse.quote(project_id, safe="")

def _api_url(path: str) -> str:
    return f"{settings.gitlab_url.rstrip('/')}/api/v4{path}"

def _get_headers() -> dict[str, str]:
    # ponytail: reads the env PAT; the dashboard /api/setup PAT (stored in Mongo) is only
    # used by the archive fetch — make this async + get_gitlab_pat() if both must match.
    return {"PRIVATE-TOKEN": settings.gitlab_pat}

async def get_mr_details(project_id: str, mr_id: int) -> MergeRequestDetails:
    encoded_project = _encode_project_id(project_id)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get MR metadata
        mr_resp = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}"),
            headers=_get_headers(),
        )
        mr_resp.raise_for_status()
        mr_data = mr_resp.json()
        
        # Get MR changes
        changes_resp = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/changes"),
            headers=_get_headers(),
        )
        changes_resp.raise_for_status()
        changes_data = changes_resp.json()

    changes = changes_data.get("changes", [])
    changed_files = [change.get("new_path", change.get("old_path", "")) for change in changes]

    dockerfile_path = None
    for f in changed_files:
        if Path(f).name.lower() == "dockerfile":
            dockerfile_path = f
            break

    if dockerfile_path is None:
        dockerfile_path = await _find_file_in_repo(project_id, mr_data.get("source_branch", ""), "Dockerfile")

    author_name = mr_data.get("author", {}).get("username", "unknown") if isinstance(mr_data.get("author"), dict) else mr_data.get("author", "unknown")

    return MergeRequestDetails(
        mr_id=mr_data.get("iid", mr_id),
        title=mr_data.get("title", ""),
        description=mr_data.get("description", ""),
        source_branch=mr_data.get("source_branch", ""),
        target_branch=mr_data.get("target_branch", ""),
        author=author_name,
        changed_files=changed_files,
        dockerfile_path=dockerfile_path,
        ci_config_path=".gitlab-ci.yml",
        has_llm_components=False,
        head_sha=mr_data.get("sha", ""),
    )

async def get_mr_diff(project_id: str, mr_id: int) -> list[FileDiff]:
    encoded_project = _encode_project_id(project_id)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/changes"),
            headers=_get_headers(),
        )
        response.raise_for_status()
        data = response.json()

    changes = data.get("changes", [])
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
    encoded_project = _encode_project_id(project_id)
    encoded_filepath = urllib.parse.quote(filepath, safe="")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            _api_url(f"/projects/{encoded_project}/repository/files/{encoded_filepath}/raw"),
            params={"ref": ref},
            headers=_get_headers(),
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

async def post_mr_comment(project_id: str, mr_id: int, body: str) -> int | None:
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

async def set_pipeline_status(project_id: str, sha: str, state: str, description: str, target_url: str = "") -> None:
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

    logger.info("Set pipeline status for %s: %s (%s)", sha[:8], state, description)

async def update_mr_approval(project_id: str, mr_id: int, approve: bool) -> None:
    encoded_project = _encode_project_id(project_id)
    endpoint = "approve" if approve else "unapprove"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _api_url(f"/projects/{encoded_project}/merge_requests/{mr_id}/{endpoint}"),
            headers=_get_headers(),
        )
        if response.status_code in (401, 403):
            logger.warning("Cannot %s MR !%d — insufficient permissions", endpoint, mr_id)
            return
        response.raise_for_status()

    logger.info("MR !%d %sd", mr_id, endpoint)

async def get_file_contents_batch(project_id: str, ref: str, filepaths: list[str], concurrency: int = 5) -> dict[str, str]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, str] = {}

    async def fetch_one(fp: str) -> None:
        async with semaphore:
            content = await get_file_content(project_id, ref, fp)
            if content is not None:
                results[fp] = content

    await asyncio.gather(*[fetch_one(fp) for fp in filepaths])
    return results

def is_doc_only_mr(changed_files: list[str]) -> bool:
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

DIFF_SUMMARY_MAX_CHARS = 6000  # keeps the Gemini context window tight


def generate_diff_summary(diffs: list[FileDiff]) -> str:
    """File list with line counts, followed by the (truncated) diff hunks so
    Gemini can relate novel errors to the actual code change (PRD §7)."""
    summary_parts = []
    hunks: list[str] = []
    for diff in diffs:
        action = "Modified"
        if diff.new_file:
            action = "Added"
        elif diff.deleted_file:
            action = "Deleted"
        elif diff.renamed_file:
            action = f"Renamed (from {diff.old_path})"

        additions = len(re.findall(r"^\+(?!\+\+)", diff.diff, re.MULTILINE))
        deletions = len(re.findall(r"^-(?!--)", diff.diff, re.MULTILINE))

        summary_parts.append(f"- {action}: `{diff.new_path}` (+{additions}/-{deletions} lines)")
        if diff.diff:
            hunks.append(f"--- {diff.new_path}\n{diff.diff.rstrip()}")

    summary = "\n".join(summary_parts)
    if hunks:
        body = "\n".join(hunks)
        if len(body) > DIFF_SUMMARY_MAX_CHARS:
            body = body[:DIFF_SUMMARY_MAX_CHARS] + "\n... (diff truncated)"
        summary += f"\n\n```diff\n{body}\n```"
    return summary

async def _find_file_in_repo(project_id: str, ref: str, filename: str) -> str | None:
    content = await get_file_content(project_id, ref, filename)
    if content is not None:
        return filename
    return None

def identify_services_from_diff(changed_files: list[str], ci_config: str | None = None) -> list[str]:
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
            if not services:
                services.add("default")

    return list(services)
