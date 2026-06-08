"""
Tests for the orchestrator worker HTTP entrypoint.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from orchestrator.main import app


client = TestClient(app)


def test_pubsub_push_accepts_valid_envelope():
    payload = {
        "simulation_id": "sim-test-worker",
        "mr_id": 42,
        "project_id": "group/project",
        "head_sha": "abc123",
        "source_branch": "feature/test",
        "target_branch": "main",
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    with patch("orchestrator.main.handle_pubsub_message", new=AsyncMock()) as handler:
        response = client.post("/pubsub/orchestrate", json={"message": {"data": encoded}})

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    handler.assert_called_once()


def test_pubsub_push_rejects_missing_message():
    response = client.post("/pubsub/orchestrate", json={})

    assert response.status_code == 400
