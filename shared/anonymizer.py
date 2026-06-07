"""
Bulwark — PII Anonymization module.

Applies all anonymization rules from the PRD (Section 10.4) to production
request logs before they are stored or replayed. Rules:
  - Email addresses → user_<hash>@example.com
  - Bearer tokens → Bearer mock_token_<hash>
  - UUIDs in paths → 00000000-0000-0000-0000-<sequential_number>
  - Phone numbers → +1555000<sequential>
  - Numeric IDs in paths → sequential integers starting from 1001
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


# ──────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────

_EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)
_BEARER_PATTERN = re.compile(
    r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
    re.IGNORECASE,
)
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_PHONE_PATTERN = re.compile(
    r"\+?1?\s*\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
)
_PATH_NUMERIC_ID_PATTERN = re.compile(
    r"(?<=/)\d{2,}(?=/|$)"
)


# ──────────────────────────────────────────────
# Counters for sequential replacements
# ──────────────────────────────────────────────


class _AnonymizationState:
    """Tracks sequential counters for deterministic anonymization within a session."""

    def __init__(self) -> None:
        self.uuid_counter = 1
        self.phone_counter = 1
        self.id_counter = 1001
        self._uuid_map: dict[str, str] = {}
        self._id_map: dict[str, int] = {}

    def reset(self) -> None:
        """Reset all counters for a new anonymization session."""
        self.uuid_counter = 1
        self.phone_counter = 1
        self.id_counter = 1001
        self._uuid_map.clear()
        self._id_map.clear()


_state = _AnonymizationState()


# ──────────────────────────────────────────────
# Individual anonymization functions
# ──────────────────────────────────────────────


def _hash_short(value: str, length: int = 8) -> str:
    """Generate a short deterministic hash of a value."""
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def anonymize_email(text: str) -> str:
    """Replace email addresses with user_<hash>@example.com."""
    def _replace(match: re.Match[str]) -> str:
        return f"user_{_hash_short(match.group(0))}@example.com"
    return _EMAIL_PATTERN.sub(_replace, text)


def anonymize_bearer_token(text: str) -> str:
    """Replace Bearer tokens with Bearer mock_token_<hash>."""
    def _replace(match: re.Match[str]) -> str:
        return f"Bearer mock_token_{_hash_short(match.group(0))}"
    return _BEARER_PATTERN.sub(_replace, text)


def anonymize_uuids(text: str) -> str:
    """Replace UUIDs with sequential anonymized UUIDs."""
    def _replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if original not in _state._uuid_map:
            seq = str(_state.uuid_counter).zfill(12)
            _state._uuid_map[original] = f"00000000-0000-0000-0000-{seq}"
            _state.uuid_counter += 1
        return _state._uuid_map[original]
    return _UUID_PATTERN.sub(_replace, text)


def anonymize_phone_numbers(text: str) -> str:
    """Replace phone numbers with +1555000<sequential>."""
    def _replace(match: re.Match[str]) -> str:
        seq = str(_state.phone_counter).zfill(4)
        _state.phone_counter += 1
        return f"+1555000{seq}"
    return _PHONE_PATTERN.sub(_replace, text)


def anonymize_path_ids(path: str) -> str:
    """Replace numeric IDs in URL paths with sequential integers starting at 1001."""
    def _replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if original not in _state._id_map:
            _state._id_map[original] = _state.id_counter
            _state.id_counter += 1
        return str(_state._id_map[original])
    return _PATH_NUMERIC_ID_PATTERN.sub(_replace, path)


# ──────────────────────────────────────────────
# Full anonymization pipeline
# ──────────────────────────────────────────────


def anonymize_text(text: str) -> str:
    """Apply all text-based anonymization rules to a string."""
    text = anonymize_email(text)
    text = anonymize_bearer_token(text)
    text = anonymize_uuids(text)
    text = anonymize_phone_numbers(text)
    return text


def anonymize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Anonymize sensitive header values. Strips Authorization entirely."""
    anonymized = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower == "authorization":
            anonymized[key] = anonymize_bearer_token(value)
        elif key_lower in ("cookie", "set-cookie", "x-api-key"):
            anonymized[key] = f"mock_{key_lower}_{_hash_short(value)}"
        else:
            anonymized[key] = anonymize_text(value)
    return anonymized


def anonymize_body(body: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
    """Recursively anonymize PII in request/response bodies."""
    if body is None:
        return None
    if isinstance(body, str):
        return anonymize_text(body)
    if isinstance(body, dict):
        return {key: _anonymize_value(key, value) for key, value in body.items()}
    return body


def _anonymize_value(key: str, value: Any) -> Any:
    """Anonymize a single value based on its key name and content."""
    if isinstance(value, str):
        # Check key name for hints
        key_lower = key.lower()
        if "email" in key_lower:
            return anonymize_email(value)
        if "phone" in key_lower or "mobile" in key_lower:
            return anonymize_phone_numbers(value)
        if "token" in key_lower or "auth" in key_lower or "password" in key_lower:
            return f"mock_{key_lower}_{_hash_short(value)}"
        # General text anonymization
        return anonymize_text(value)
    if isinstance(value, dict):
        return anonymize_body(value)
    if isinstance(value, list):
        return [_anonymize_value(key, item) for item in value]
    return value


def anonymize_production_request(
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any] | str | None,
) -> tuple[str, dict[str, str], dict[str, Any] | str | None]:
    """
    Full anonymization of a single production request.

    Returns:
        Tuple of (anonymized_path, anonymized_headers, anonymized_body)
    """
    anon_path = anonymize_path_ids(path)
    anon_path = anonymize_uuids(anon_path)
    anon_headers = anonymize_headers(headers)
    anon_body = anonymize_body(body)
    return anon_path, anon_headers, anon_body


def reset_anonymization_state() -> None:
    """Reset the anonymization counters. Call at the start of each simulation."""
    _state.reset()
