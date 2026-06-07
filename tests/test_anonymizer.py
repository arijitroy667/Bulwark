"""
Tests for Bulwark PII Anonymization module.

Tests cover all anonymization rules from PRD Section 10.4:
  - Email addresses → user_<hash>@example.com
  - Bearer tokens → Bearer mock_token_<hash>
  - UUIDs → 00000000-0000-0000-0000-<sequential>
  - Phone numbers → +1555000<sequential>
  - Numeric IDs in paths → sequential starting at 1001
"""

from __future__ import annotations

import pytest

from shared.anonymizer import (
    anonymize_bearer_token,
    anonymize_body,
    anonymize_email,
    anonymize_headers,
    anonymize_path_ids,
    anonymize_phone_numbers,
    anonymize_production_request,
    anonymize_text,
    anonymize_uuids,
    reset_anonymization_state,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset anonymization counters before each test."""
    reset_anonymization_state()
    yield
    reset_anonymization_state()


# ──────────────────────────────────────────────
# Email anonymization
# ──────────────────────────────────────────────


class TestEmailAnonymization:
    def test_simple_email(self):
        result = anonymize_email("Contact john.doe@company.com for info")
        assert "john.doe@company.com" not in result
        assert "@example.com" in result
        assert result.startswith("Contact user_")

    def test_multiple_emails(self):
        text = "From alice@test.com to bob@test.com"
        result = anonymize_email(text)
        assert "alice@test.com" not in result
        assert "bob@test.com" not in result
        assert result.count("@example.com") == 2

    def test_no_email(self):
        text = "No emails here"
        result = anonymize_email(text)
        assert result == text

    def test_deterministic(self):
        """Same email should produce the same hash."""
        r1 = anonymize_email("user@test.com")
        r2 = anonymize_email("user@test.com")
        assert r1 == r2


# ──────────────────────────────────────────────
# Bearer token anonymization
# ──────────────────────────────────────────────


class TestBearerTokenAnonymization:
    def test_bearer_token(self):
        result = anonymize_bearer_token("Bearer eyJhbGciOiJIUzI1NiJ9.token")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert result.startswith("Bearer mock_token_")

    def test_bearer_case_insensitive(self):
        result = anonymize_bearer_token("bearer abc123")
        assert "abc123" not in result
        assert "mock_token_" in result

    def test_no_bearer(self):
        text = "No tokens here"
        result = anonymize_bearer_token(text)
        assert result == text


# ──────────────────────────────────────────────
# UUID anonymization
# ──────────────────────────────────────────────


class TestUUIDAnonymization:
    def test_uuid_replacement(self):
        text = "User 550e8400-e29b-41d4-a716-446655440000 created"
        result = anonymize_uuids(text)
        assert "550e8400-e29b-41d4-a716-446655440000" not in result
        assert "00000000-0000-0000-0000-000000000001" in result

    def test_sequential_uuids(self):
        text1 = "ID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        text2 = "ID: 11111111-2222-3333-4444-555555555555"
        r1 = anonymize_uuids(text1)
        r2 = anonymize_uuids(text2)
        assert "000000000001" in r1
        assert "000000000002" in r2

    def test_same_uuid_same_replacement(self):
        """Identical UUIDs should map to the same replacement."""
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        text = f"First: {uuid}, Second: {uuid}"
        result = anonymize_uuids(text)
        parts = result.split(", ")
        assert parts[0].split(": ")[1] == parts[1].split(": ")[1]


# ──────────────────────────────────────────────
# Phone number anonymization
# ──────────────────────────────────────────────


class TestPhoneAnonymization:
    def test_us_phone(self):
        result = anonymize_phone_numbers("Call 555-123-4567")
        assert "555-123-4567" not in result
        assert "+1555000" in result

    def test_phone_with_country_code(self):
        result = anonymize_phone_numbers("Phone: +1 (555) 123-4567")
        assert "123-4567" not in result
        assert "+1555000" in result

    def test_sequential_phones(self):
        r1 = anonymize_phone_numbers("555-111-2222")
        r2 = anonymize_phone_numbers("555-333-4444")
        assert r1 != r2


# ──────────────────────────────────────────────
# Path ID anonymization
# ──────────────────────────────────────────────


class TestPathIdAnonymization:
    def test_numeric_id_in_path(self):
        result = anonymize_path_ids("/api/users/12345/posts")
        assert "12345" not in result
        assert "1001" in result

    def test_multiple_ids(self):
        result = anonymize_path_ids("/api/users/999/posts/888")
        assert "999" not in result
        assert "888" not in result
        assert "1001" in result
        assert "1002" in result

    def test_single_digit_preserved(self):
        """Single-digit path segments are NOT replaced (may be API version)."""
        result = anonymize_path_ids("/api/v2/health")
        assert "/api/v2/health" == result

    def test_same_id_same_replacement(self):
        """Same numeric ID should map to the same replacement."""
        result = anonymize_path_ids("/api/users/999/profile/999")
        parts = result.split("/")
        # The two 999s should map to the same value
        assert parts[3] == parts[5]


# ──────────────────────────────────────────────
# Full text anonymization
# ──────────────────────────────────────────────


class TestFullTextAnonymization:
    def test_combined(self):
        text = "User john@test.com with Bearer token123 logged in"
        result = anonymize_text(text)
        assert "john@test.com" not in result
        assert "token123" not in result


# ──────────────────────────────────────────────
# Header anonymization
# ──────────────────────────────────────────────


class TestHeaderAnonymization:
    def test_authorization_header(self):
        headers = {"Authorization": "Bearer secret-token-123"}
        result = anonymize_headers(headers)
        assert "secret-token-123" not in result["Authorization"]
        assert "mock_token_" in result["Authorization"]

    def test_cookie_header(self):
        headers = {"Cookie": "session=abc123"}
        result = anonymize_headers(headers)
        assert "abc123" not in result["Cookie"]

    def test_safe_headers_preserved(self):
        headers = {"Content-Type": "application/json"}
        result = anonymize_headers(headers)
        assert result["Content-Type"] == "application/json"


# ──────────────────────────────────────────────
# Body anonymization
# ──────────────────────────────────────────────


class TestBodyAnonymization:
    def test_dict_body(self):
        body = {"email": "test@example.com", "name": "John"}
        result = anonymize_body(body)
        assert isinstance(result, dict)
        assert "test@example.com" not in result["email"]
        assert "@example.com" in result["email"]

    def test_string_body(self):
        body = "Contact admin@company.com"
        result = anonymize_body(body)
        assert "admin@company.com" not in result

    def test_none_body(self):
        assert anonymize_body(None) is None

    def test_nested_dict(self):
        body = {
            "user": {
                "email": "deep@test.com",
                "phone": "555-111-2222",
            }
        }
        result = anonymize_body(body)
        assert "deep@test.com" not in str(result)


# ──────────────────────────────────────────────
# Full production request anonymization
# ──────────────────────────────────────────────


class TestProductionRequestAnonymization:
    def test_full_request(self):
        path, headers, body = anonymize_production_request(
            method="POST",
            path="/api/users/12345",
            headers={"Authorization": "Bearer secret123"},
            body={"email": "user@test.com"},
        )
        assert "12345" not in path
        assert "secret123" not in headers.get("Authorization", "")
        assert "user@test.com" not in str(body)

    def test_reset_state(self):
        """After reset, counters should restart."""
        reset_anonymization_state()
        path1 = anonymize_path_ids("/api/items/999")
        assert "1001" in path1

        reset_anonymization_state()
        path2 = anonymize_path_ids("/api/items/888")
        assert "1001" in path2  # Counter restarted
