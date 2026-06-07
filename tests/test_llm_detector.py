"""
Tests for orchestrator.llm_detector — two-stage LLM component detection.
"""

from __future__ import annotations

import pytest

from orchestrator.llm_detector import (
    AST_SCAN_FILE_CAP,
    detect_llm_components,
    _stage1_path_heuristic,
    _stage2_ast_scan,
    _scan_python_imports,
    _scan_js_imports,
)


# ──────────────────────────────────────────────
# Stage 1: Path heuristic
# ──────────────────────────────────────────────


class TestStage1PathHeuristic:
    def test_prompts_directory_detected(self):
        files = ["services/auth/prompts/system.txt", "services/auth/main.py"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True
        assert result.detection_stage == "path_heuristic"
        assert "services/auth/prompts/system.txt" in result.matched_files

    def test_llm_directory_detected(self):
        files = ["src/llm/client.py", "src/main.py"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_ai_directory_detected(self):
        files = ["app/ai/summarizer.py"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_genai_directory_detected(self):
        files = ["backend/genai/prompts/template.jinja"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_prompt_filename_pattern_detected(self):
        files = ["handlers/user_prompt.py"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_jinja_extension_detected(self):
        files = ["templates/system.jinja"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_jinja2_extension_detected(self):
        files = ["templates/response.jinja2"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_dot_prompt_extension_detected(self):
        files = ["config/base.prompt"]
        result = _stage1_path_heuristic(files)
        assert result.detected is True

    def test_regular_python_not_detected(self):
        files = ["services/auth/main.py", "tests/test_auth.py", "README.md"]
        result = _stage1_path_heuristic(files)
        assert result.detected is False

    def test_docs_only_not_detected(self):
        files = ["docs/architecture.md", "docs/api.md"]
        result = _stage1_path_heuristic(files)
        assert result.detected is False

    def test_empty_file_list(self):
        result = _stage1_path_heuristic([])
        assert result.detected is False


# ──────────────────────────────────────────────
# Stage 2: Python AST scan
# ──────────────────────────────────────────────


class TestPythonASTScan:
    def test_openai_import_detected(self):
        content = "import openai\nclient = openai.OpenAI()"
        imports = _scan_python_imports(content)
        assert "openai" in imports

    def test_openai_from_import_detected(self):
        content = "from openai import OpenAI\nclient = OpenAI()"
        imports = _scan_python_imports(content)
        assert "openai" in imports

    def test_anthropic_import_detected(self):
        content = "import anthropic\nclient = anthropic.Anthropic()"
        imports = _scan_python_imports(content)
        assert "anthropic" in imports

    def test_google_generativeai_detected(self):
        content = "import google.generativeai as genai"
        imports = _scan_python_imports(content)
        assert any("google" in i or "generativeai" in i for i in imports)

    def test_vertexai_detected(self):
        content = "from vertexai.generative_models import GenerativeModel"
        imports = _scan_python_imports(content)
        assert any("vertexai" in i for i in imports)

    def test_langchain_detected(self):
        content = "from langchain_openai import ChatOpenAI"
        imports = _scan_python_imports(content)
        assert any("langchain" in i for i in imports)

    def test_llama_index_detected(self):
        content = "from llama_index.core import VectorStoreIndex"
        imports = _scan_python_imports(content)
        assert any("llama_index" in i for i in imports)

    def test_regular_import_not_detected(self):
        content = "import os\nimport sys\nfrom datetime import datetime"
        imports = _scan_python_imports(content)
        assert imports == []

    def test_invalid_python_does_not_crash(self):
        content = "def foo(:\n    pass"  # Intentionally invalid
        imports = _scan_python_imports(content)
        assert imports == []


# ──────────────────────────────────────────────
# Stage 2: JS/TS regex scan
# ──────────────────────────────────────────────


class TestJSScan:
    def test_openai_require_detected(self):
        content = "const openai = require('openai');"
        imports = _scan_js_imports(content)
        assert len(imports) > 0

    def test_openai_esm_import_detected(self):
        content = "import OpenAI from 'openai';"
        imports = _scan_js_imports(content)
        assert len(imports) > 0, f"ESM 'from openai' should be detected, content: {content!r}"

    def test_anthropic_sdk_detected(self):
        content = 'import Anthropic from "@anthropic-ai/sdk";'
        imports = _scan_js_imports(content)
        assert len(imports) > 0, f"ESM '@anthropic-ai/sdk' should be detected, content: {content!r}"

    def test_google_generativeai_js_detected(self):
        content = "import { GoogleGenerativeAI } from '@google/generative-ai';"
        imports = _scan_js_imports(content)
        assert len(imports) > 0, f"ESM '@google/generative-ai' should be detected, content: {content!r}"

    def test_langchain_js_detected(self):
        content = "import { ChatOpenAI } from '@langchain/openai';"
        imports = _scan_js_imports(content)
        assert len(imports) > 0

    def test_regular_js_not_detected(self):
        content = "import React from 'react';\nimport axios from 'axios';"
        imports = _scan_js_imports(content)
        assert imports == []


# ──────────────────────────────────────────────
# Full pipeline: detect_llm_components
# ──────────────────────────────────────────────


class TestDetectLLMComponents:
    def test_stage1_match_returns_early(self):
        """Stage 1 match should not trigger Stage 2 (no contents needed)."""
        files = ["app/prompts/system.txt", "app/main.py"]
        result = detect_llm_components(files, file_contents=None)
        assert result.detected is True
        assert result.detection_stage == "path_heuristic"

    def test_stage2_triggered_when_no_stage1_match(self):
        files = ["services/auth/auth.py"]
        contents = {"services/auth/auth.py": "import openai\nclient = openai.OpenAI()"}
        result = detect_llm_components(files, file_contents=contents)
        assert result.detected is True
        assert result.detection_stage == "ast_scan"
        assert "openai" in result.matched_imports

    def test_no_llm_detected_when_clean_codebase(self):
        files = ["services/auth/auth.py", "services/auth/models.py"]
        contents = {
            "services/auth/auth.py": "import os\nfrom datetime import datetime",
            "services/auth/models.py": "from pydantic import BaseModel",
        }
        result = detect_llm_components(files, file_contents=contents)
        assert result.detected is False

    def test_file_cap_respected(self):
        """AST scan should cap at AST_SCAN_FILE_CAP files."""
        # Generate many Python files
        many_files = [f"src/module_{i}.py" for i in range(AST_SCAN_FILE_CAP + 20)]
        # First file has LLM import, but it's beyond the cap in alphabetical order
        # The cap only applies to scannable files slice, not which files are selected
        contents = {f: "import os" for f in many_files}
        contents["src/module_0.py"] = "import openai"
        result = detect_llm_components(many_files, file_contents=contents)
        # module_0 is first — should be within cap
        assert result.detected is True

    def test_no_contents_provided_returns_not_detected_after_stage1_miss(self):
        files = ["src/app.py", "src/models.py"]
        result = detect_llm_components(files, file_contents=None)
        assert result.detected is False
        assert result.detection_stage == "none"

    def test_empty_files_list(self):
        result = detect_llm_components([], file_contents={})
        assert result.detected is False
