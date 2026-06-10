"""
Tests for the demo-app.

Verifies the endpoints that Bulwark calls during regression testing:
  - GET /users (batch vs N+1 routing based on DEMO_VERSION)
  - GET /traces (for LLM observability)
  - GET /metrics
"""

import pytest
from fastapi.testclient import TestClient

import sys
import os
from pathlib import Path

# Add demo-app directory to sys.path so we can import main
sys.path.insert(0, str(Path(__file__).parent.parent / "demo-app"))
import main

client = TestClient(main.app)


def test_get_users_returns_list():
    """GET /users returns a list of user dicts."""
    response = client.get("/users")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "users" in data
    assert len(data["users"]) == 25
    assert "name" in data["users"][0]


def test_get_user_by_id():
    """GET /users/{user_id} returns a specific user."""
    response = client.get("/users/1")
    assert response.status_code == 200
    assert response.json()["id"] == 1


def test_get_user_not_found():
    """GET /users/{user_id} returns 404 for unknown user."""
    response = client.get("/users/999")
    assert response.status_code == 404


def test_get_metrics():
    """GET /metrics returns version and counts."""
    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "demo_version" in data
    assert "user_count" in data


def test_get_traces_empty_when_not_llm_demo(monkeypatch):
    """GET /traces returns empty list when DEMO_VERSION != 'llm-demo'."""
    import main
    monkeypatch.setattr(main, "DEMO_VERSION", "baseline")
    
    response = client.get("/traces")
    assert response.status_code == 200
    data = response.json()
    assert data["traces"] == []


def test_get_traces_returns_mock_traces_when_llm_demo(monkeypatch):
    """GET /traces returns mock traces when DEMO_VERSION == 'llm-demo'."""
    import main
    monkeypatch.setattr(main, "DEMO_VERSION", "llm-demo")
    
    response = client.get("/traces")
    assert response.status_code == 200
    data = response.json()
    assert len(data["traces"]) == 2
    assert "prompt" in data["traces"][0]
    assert "gemini" in data["traces"][0]["model"]
