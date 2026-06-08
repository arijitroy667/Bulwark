"""
Tests for orchestrator LLM trace capture.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.agent import _extract_llm_outputs_from_replay
from shared.models import EphemeralEnvironment


class _FakeTraceResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeTraceClient:
    last_url: str | None = None

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeTraceClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeTraceResponse:
        self.__class__.last_url = url
        return _FakeTraceResponse(self._payload)


def _fake_client_factory(payload: Any):
    class FakeTraceClient(_FakeTraceClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(payload)

    return FakeTraceClient


def _environment() -> EphemeralEnvironment:
    return EphemeralEnvironment(
        simulation_id="sim-traces",
        base_url="https://example.run.app/",
    )


@pytest.mark.asyncio
async def test_extract_llm_outputs_fetches_traces_list(monkeypatch):
    payload = [
        {
            "prompt": "Summarize this ticket",
            "response": "The ticket is about billing.",
            "context": {"ticket_id": "T-1"},
            "model": "gpt-4.1-mini",
        }
    ]
    fake_client = _fake_client_factory(payload)
    monkeypatch.setattr("orchestrator.agent.httpx.AsyncClient", fake_client)

    traces = await _extract_llm_outputs_from_replay(_environment())

    assert fake_client.last_url == "https://example.run.app/traces"
    assert traces == payload


@pytest.mark.asyncio
async def test_extract_llm_outputs_accepts_spans_object(monkeypatch):
    payload = {
        "spans": [
            {"prompt": "Question", "response": "Answer"},
            {"prompt": "Missing response"},
        ]
    }
    monkeypatch.setattr("orchestrator.agent.httpx.AsyncClient", _fake_client_factory(payload))

    traces = await _extract_llm_outputs_from_replay(_environment())

    assert traces == [{"prompt": "Question", "response": "Answer", "context": None}]


@pytest.mark.asyncio
async def test_extract_llm_outputs_rejects_invalid_payload(monkeypatch):
    monkeypatch.setattr("orchestrator.agent.httpx.AsyncClient", _fake_client_factory({"bad": []}))

    traces = await _extract_llm_outputs_from_replay(_environment())

    assert traces == []
