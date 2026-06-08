"""
Tests for Bulwark Webhook Receiver (v3.0).

Tests cover:
  - Valid webhook with correct token → 200 + queued
  - Invalid/missing token → 400
  - Non-MR event → 422
  - Non-tracked MR action (close, merge) → skipped
  - Doc-only MR detection
  - Deterministic simulation ID generation
  - v3: Version 3.0.0 in health check
  - v3: Simulation cancellation on new push (FR-023)
  - v3: Idempotency by mr_id + head_sha
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

# Set environment variables for tests (fallback)
os.environ["GITLAB_WEBHOOK_TOKEN"] = "test-webhook-secret"
os.environ["BULWARK_MODE"] = "development"
os.environ["MONGODB_URI"] = "mongodb://localhost:27017"

# Also directly override settings to ensure it works regardless of import order
from shared.config import settings
settings.gitlab_webhook_token = "test-webhook-secret"
settings.bulwark_mode = "development"
settings.mongodb_uri = "mongodb://localhost:27017"

from webhook_receiver.main import (
    _has_override_label,
    _generate_simulation_id,
    _is_doc_only_mr,
    app,
)

# Mock MongoDB calls so tests don't hang if Mongo isn't running
from unittest.mock import AsyncMock, MagicMock, patch

mock_collection = MagicMock()
mock_collection.insert_one = AsyncMock()
mock_db = MagicMock()
mock_db.__getitem__.return_value = mock_collection

patch("webhook_receiver.main.get_database", new_callable=AsyncMock, return_value=mock_db).start()
# v3: patch updated idempotency function
patch("webhook_receiver.main.simulation_exists_by_sha", new_callable=AsyncMock, return_value=False).start()
# v3: patch cancellation function
patch("webhook_receiver.main.cancel_in_progress_simulation", new_callable=AsyncMock, return_value=0).start()

client = TestClient(app)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

VALID_TOKEN = "test-webhook-secret"

SAMPLE_MR_WEBHOOK = {
    "object_kind": "merge_request",
    "event_type": "merge_request",
    "project": {
        "id": 1,
        "path_with_namespace": "testgroup/testrepo",
    },
    "object_attributes": {
        "iid": 42,
        "title": "Add new authentication flow",
        "action": "open",
        "state": "opened",
        "source_branch": "feature/new-auth",
        "target_branch": "main",
        "url": "https://gitlab.com/testgroup/testrepo/-/merge_requests/42",
        "last_commit": {
            "id": "abc123def456",
        },
    },
    "changes": {},
}


# ──────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────


def test_health_check():
    """GET /health should return 200 with status ok and version 3.0.0."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "3.0.0"  # v3 version bump


# ──────────────────────────────────────────────
# Token validation
# ──────────────────────────────────────────────


def test_valid_token_accepted():
    """Valid X-Gitlab-Token should be accepted."""
    response = client.post(
        "/webhook/gitlab",
        headers={"X-Gitlab-Token": VALID_TOKEN},
        json=SAMPLE_MR_WEBHOOK,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("queued", "duplicate")


def test_missing_token_rejected():
    """Missing X-Gitlab-Token should return 400."""
    response = client.post(
        "/webhook/gitlab",
        json=SAMPLE_MR_WEBHOOK,
    )
    assert response.status_code == 400


def test_invalid_token_rejected():
    """Invalid X-Gitlab-Token should return 400."""
    response = client.post(
        "/webhook/gitlab",
        headers={"X-Gitlab-Token": "wrong-token"},
        json=SAMPLE_MR_WEBHOOK,
    )
    assert response.status_code == 400


def test_override_label_detected_from_gitlab_label_change():
    """bulwark-override labels should be detected from GitLab update payloads."""
    body = {
        "changes": {
            "labels": {
                "current": [{"title": "bulwark-override"}],
                "previous": [],
            }
        }
    }
    assert _has_override_label(body, {}) is True


# ──────────────────────────────────────────────
# Event filtering
# ──────────────────────────────────────────────


def test_non_mr_event_rejected():
    """Non-merge_request events should return 422."""
    payload = {
        "object_kind": "push",
        "event_type": "push",
    }
    response = client.post(
        "/webhook/gitlab",
        headers={"X-Gitlab-Token": VALID_TOKEN},
        json=payload,
    )
    assert response.status_code == 422


def test_non_tracked_action_skipped():
    """MR actions like 'close' and 'merge' should be skipped (not queued)."""
    for action in ["close", "merge", "approved"]:
        payload = {**SAMPLE_MR_WEBHOOK}
        payload["object_attributes"] = {
            **SAMPLE_MR_WEBHOOK["object_attributes"],
            "action": action,
        }
        response = client.post(
            "/webhook/gitlab",
            headers={"X-Gitlab-Token": VALID_TOKEN},
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped", f"Action '{action}' should be skipped"


def test_valid_actions_accepted():
    """MR actions open/update/reopen should be accepted."""
    for action in ["open", "update", "reopen"]:
        payload = {
            **SAMPLE_MR_WEBHOOK,
            "object_attributes": {
                **SAMPLE_MR_WEBHOOK["object_attributes"],
                "action": action,
                "last_commit": {"id": f"sha_{action}"},
            },
        }
        response = client.post(
            "/webhook/gitlab",
            headers={"X-Gitlab-Token": VALID_TOKEN},
            json=payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("queued", "duplicate"), (
            f"Action '{action}' should be queued"
        )


# ──────────────────────────────────────────────
# Doc-only MR detection
# ──────────────────────────────────────────────


def test_doc_only_mr_detection():
    """MRs that only modify docs should be detected."""
    assert _is_doc_only_mr(["README.md"]) is True
    assert _is_doc_only_mr(["docs/guide.md", "docs/api.txt"]) is True
    assert _is_doc_only_mr(["CHANGELOG.md", "LICENSE.txt"]) is True
    assert _is_doc_only_mr(["doc/setup.rst"]) is True


def test_non_doc_mr_not_skipped():
    """MRs with code changes should NOT be detected as doc-only."""
    assert _is_doc_only_mr(["src/main.py"]) is False
    assert _is_doc_only_mr(["README.md", "src/main.py"]) is False
    assert _is_doc_only_mr(["Dockerfile"]) is False


def test_empty_changelist_not_doc_only():
    """Empty change lists should not be treated as doc-only."""
    assert _is_doc_only_mr([]) is False


# ──────────────────────────────────────────────
# Simulation ID generation
# ──────────────────────────────────────────────


def test_deterministic_simulation_id():
    """Same inputs should produce the same simulation ID (idempotency)."""
    id1 = _generate_simulation_id(42, "feature/test", "sha123")
    id2 = _generate_simulation_id(42, "feature/test", "sha123")
    assert id1 == id2
    assert id1.startswith("sim_")


def test_different_inputs_different_ids():
    """Different inputs should produce different simulation IDs."""
    id1 = _generate_simulation_id(42, "feature/test", "sha123")
    id2 = _generate_simulation_id(42, "feature/test", "sha456")
    id3 = _generate_simulation_id(43, "feature/test", "sha123")
    assert id1 != id2
    assert id1 != id3


# ──────────────────────────────────────────────
# v3: Cancellation on new push (FR-023)
# ──────────────────────────────────────────────


def test_cancellation_called_on_new_push():
    """
    v3 (FR-023): When a new push arrives on an existing MR, cancel_in_progress_simulation
    should be called to cancel any running simulations for the same MR.
    """
    from unittest.mock import AsyncMock, patch as mock_patch

    with mock_patch(
        "webhook_receiver.main.cancel_in_progress_simulation",
        new_callable=AsyncMock,
        return_value=1,  # Simulate 1 cancelled simulation
    ) as mock_cancel:
        response = client.post(
            "/webhook/gitlab",
            headers={"X-Gitlab-Token": VALID_TOKEN},
            json=SAMPLE_MR_WEBHOOK,
        )
        assert response.status_code == 200
        # cancel_in_progress_simulation should have been called with the MR id and new SHA
        assert mock_cancel.called
        call_args = mock_cancel.call_args
        mr_id_arg = call_args[0][0] if call_args[0] else call_args[1].get("mr_id")
        assert mr_id_arg == 42  # From SAMPLE_MR_WEBHOOK object_attributes.iid


def test_idempotency_by_sha_prevents_duplicate():
    """
    v3: Duplicate check uses mr_id + head_sha. Duplicate should return status=duplicate.
    """
    from unittest.mock import AsyncMock, patch as mock_patch

    with mock_patch(
        "webhook_receiver.main.simulation_exists_by_sha",
        new_callable=AsyncMock,
        return_value=True,  # Simulate existing simulation for this SHA
    ):
        response = client.post(
            "/webhook/gitlab",
            headers={"X-Gitlab-Token": VALID_TOKEN},
            json=SAMPLE_MR_WEBHOOK,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "duplicate"
