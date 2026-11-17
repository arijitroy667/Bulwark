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


def test_traces_record_ticket_summaries(monkeypatch):
    """Each /tickets/{id}/summary call records a {prompt, response, context} trace."""
    monkeypatch.setattr(main, "TRACE_BUFFER", main.deque(maxlen=10))
    assert client.get("/traces").json()["traces"] == []

    assert client.get("/tickets/2/summary").status_code == 200
    (trace,) = client.get("/traces").json()["traces"]
    assert "order A-1002" in trace["context"]
    assert "Use only facts" in trace["prompt"]          # production prompt file
    assert "charged twice" in trace["response"]


def test_edited_prompt_changes_traces(monkeypatch):
    """The llm-regression build uses the edited prompt and over-promises."""
    monkeypatch.setattr(main, "TRACE_BUFFER", main.deque(maxlen=10))
    monkeypatch.setattr(main, "DEMO_VERSION", "llm-regression")
    client.get("/tickets/2/summary")
    (trace,) = client.get("/traces").json()["traces"]
    assert "Reassure the customer" in trace["prompt"]
    assert "refund" in trace["response"] and "charged twice" not in trace["response"]


def test_unknown_ticket_404():
    assert client.get("/tickets/99/summary").status_code == 404
