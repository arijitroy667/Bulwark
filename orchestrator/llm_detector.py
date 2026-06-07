"""
Bulwark — Two-Stage LLM Component Detection (v3.0, PRD §10).

Stage 1: File path heuristic — fast scan on all changed files.
Stage 2: AST-based import scan — precision scan on Python/JS files (cap: 50 files).

A match at either stage triggers the Arize evaluation path.
No match → Arize step skipped (noted in verdict).

Why two stages:
  v1.0 used string matching on tokens like `system_prompt`.
  This produces false positives in ~majority of non-LLM codebases.
  AST-based import detection is far more precise.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from shared.models import LLMDetectionResult

logger = logging.getLogger("bulwark.llm_detector")

# ──────────────────────────────────────────────
# Stage 1: File path heuristic
# ──────────────────────────────────────────────

# Directories that strongly suggest LLM component presence
LLM_DIRECTORIES = {
    "prompts/",
    "system_prompts/",
    "llm/",
    "ai/",
    "genai/",
    "evals/",
    "evaluations/",
    "langchain/",
}

# File name patterns that suggest LLM prompts/configs
LLM_FILENAME_PATTERNS = [
    re.compile(r".*_prompt\.py$", re.IGNORECASE),
    re.compile(r".*_prompt\.txt$", re.IGNORECASE),
    re.compile(r"prompt_.*\.py$", re.IGNORECASE),
    re.compile(r".*\.prompt$", re.IGNORECASE),
    re.compile(r".*\.system$", re.IGNORECASE),
    re.compile(r".*\.jinja$", re.IGNORECASE),
    re.compile(r".*\.jinja2$", re.IGNORECASE),
]

# ──────────────────────────────────────────────
# Stage 2: AST import scan
# ──────────────────────────────────────────────

# Python packages that indicate LLM usage
PYTHON_LLM_IMPORTS = {
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "vertexai.generative_models",
    "litellm",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "llama_index",
    "llama_index.core",
    "cohere",
    "mistralai",
}

# JavaScript/TypeScript patterns (regex on file content, since we can't run AST)
# Use simple `from 'package'` patterns which cover both ESM imports and require()
JS_LLM_IMPORT_PATTERNS = [
    re.compile(r"from\s+['\"]openai['\"]", re.MULTILINE),
    re.compile(r"require\s*\(['\"]openai['\"]\)", re.MULTILINE),
    re.compile(r"from\s+['\"]@anthropic-ai/sdk['\"]", re.MULTILINE),
    re.compile(r"require\s*\(['\"]@anthropic-ai/sdk['\"]\)", re.MULTILINE),
    re.compile(r"from\s+['\"]@google/generative-ai['\"]", re.MULTILINE),
    re.compile(r"require\s*\(['\"]@google/generative-ai['\"]\)", re.MULTILINE),
    re.compile(r"from\s+['\"]langchain['\"]", re.MULTILINE),
    re.compile(r"from\s+['\"]llamaindex['\"]", re.MULTILINE),
    re.compile(r"from\s+['\"]@mistralai/mistralai['\"]", re.MULTILINE),
    re.compile(r"from\s+['\"]cohere-ai['\"]", re.MULTILINE),
    re.compile(r"from\s+['\"]@langchain/", re.MULTILINE),
    re.compile(r"require\s*\(['\"]@langchain/", re.MULTILINE),
]

# AST scan file cap (PRD §10.2)
AST_SCAN_FILE_CAP = 50


def detect_llm_components(
    changed_files: list[str],
    file_contents: dict[str, str] | None = None,
) -> LLMDetectionResult:
    """
    Run the two-stage LLM component detection pipeline.

    Args:
        changed_files: List of file paths changed in the MR.
        file_contents: Optional dict of {path: content} for AST scanning.
                       If None, Stage 2 is skipped.

    Returns:
        LLMDetectionResult with detected=True if LLM components found.
    """
    # ── Stage 1: File path heuristic ──
    stage1_result = _stage1_path_heuristic(changed_files)
    if stage1_result.detected:
        logger.info(
            "LLM detected via Stage 1 (path heuristic): %s",
            stage1_result.matched_files,
        )
        return stage1_result

    # ── Stage 2: AST import scan ──
    if file_contents is None:
        logger.debug("Stage 2 skipped: no file contents provided")
        return LLMDetectionResult(
            detected=False,
            detection_stage="none",
            reason="No LLM indicators found in file paths; AST scan not performed",
        )

    stage2_result = _stage2_ast_scan(changed_files, file_contents)
    if stage2_result.detected:
        logger.info(
            "LLM detected via Stage 2 (AST scan): imports=%s",
            stage2_result.matched_imports,
        )
    else:
        logger.debug("No LLM components detected via either stage")

    return stage2_result


def _stage1_path_heuristic(changed_files: list[str]) -> LLMDetectionResult:
    """Stage 1: Check file paths against LLM directory and filename patterns."""
    matched_files: list[str] = []

    for filepath in changed_files:
        filepath_lower = filepath.lower()
        path = Path(filepath_lower)

        # Check directory membership
        for llm_dir in LLM_DIRECTORIES:
            if llm_dir in filepath_lower:
                matched_files.append(filepath)
                break
        else:
            # Check filename patterns
            filename = path.name
            for pattern in LLM_FILENAME_PATTERNS:
                if pattern.match(filename):
                    matched_files.append(filepath)
                    break

    if matched_files:
        return LLMDetectionResult(
            detected=True,
            detection_stage="path_heuristic",
            matched_files=matched_files,
            reason=(
                f"LLM-related file paths detected: "
                f"{', '.join(matched_files[:3])}"
                f"{'...' if len(matched_files) > 3 else ''}"
            ),
        )

    return LLMDetectionResult(detected=False)


def _stage2_ast_scan(
    changed_files: list[str],
    file_contents: dict[str, str],
) -> LLMDetectionResult:
    """
    Stage 2: AST-based import scan on Python and JS/TS files.
    Capped at AST_SCAN_FILE_CAP files for performance.
    """
    # Filter to Python and JS/TS files only
    scannable_files = [
        f for f in changed_files
        if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"))
    ]

    # Apply cap
    if len(scannable_files) > AST_SCAN_FILE_CAP:
        logger.info(
            "AST scan capped at %d files (MR touches %d scannable files)",
            AST_SCAN_FILE_CAP,
            len(scannable_files),
        )
        scannable_files = scannable_files[:AST_SCAN_FILE_CAP]

    matched_files: list[str] = []
    matched_imports: list[str] = []

    for filepath in scannable_files:
        content = file_contents.get(filepath, "")
        if not content:
            continue

        if filepath.endswith(".py"):
            imports = _scan_python_imports(content)
        else:
            imports = _scan_js_imports(content)

        if imports:
            matched_files.append(filepath)
            matched_imports.extend(imports)

    if matched_files:
        return LLMDetectionResult(
            detected=True,
            detection_stage="ast_scan",
            matched_files=matched_files,
            matched_imports=list(set(matched_imports)),
            reason=(
                f"LLM SDK imports found in {len(matched_files)} file(s): "
                f"{', '.join(list(set(matched_imports))[:5])}"
            ),
        )

    return LLMDetectionResult(
        detected=False,
        detection_stage="none",
        reason="No LLM SDK imports found in changed Python/JS files (AST scan complete)",
    )


def _scan_python_imports(content: str) -> list[str]:
    """Extract LLM-related imports from Python source using AST parsing."""
    found: list[str] = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if alias.name in PYTHON_LLM_IMPORTS or top in PYTHON_LLM_IMPORTS:
                        found.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    full = node.module
                    if full in PYTHON_LLM_IMPORTS or top in PYTHON_LLM_IMPORTS:
                        found.append(full)
    except SyntaxError:
        # File may be partially valid Python (e.g., diff fragment) — skip
        pass
    return found


def _scan_js_imports(content: str) -> list[str]:
    """Extract LLM-related imports from JS/TS source using regex patterns."""
    found: list[str] = []
    for pattern in JS_LLM_IMPORT_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            found.extend(matches)
    return found
