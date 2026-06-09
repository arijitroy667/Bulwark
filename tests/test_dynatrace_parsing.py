"""
Tests for Phase 4 Dynatrace Multi-format Log Parsing.
"""
import pytest
from orchestrator.tools.dynatrace_tool import _parse_log_to_request

def test_otel_span_format_parsed():
    record = {
        "attributes": {
            "http.method": "POST",
            "http.url": "https://api.example.com/v1/data",
            "http.status_code": 201,
            "http.request.headers": {"Authorization": "Bearer real-token"},
            "http.request.body": '{"key": "value"}'
        }
    }
    req = _parse_log_to_request(record)
    assert req is not None
    assert req.method == "POST"
    assert req.path == "/v1/data"  # Extracted from URL
    assert req.expected_status == 201
    assert "Authorization" in req.headers
    assert "mock_token" in req.headers["Authorization"]  # Validates anonymizer ran
    assert isinstance(req.body, dict)
    assert req.body["key"] == "value"

def test_dynatrace_v2_log_body_parsed():
    record = {
        "log.body": {
            "content": "HTTP DELETE /api/users/99 204"
        }
    }
    req = _parse_log_to_request(record)
    assert req is not None
    assert req.method == "DELETE"
    assert req.expected_status == 204
    assert req.path == "/api/users/1001"  # Anonymizer replaces /99 with sequential 1001
    
def test_full_url_path_extracted():
    record = {
        "method": "PUT",
        "url": "http://internal-svc:8080/path/to/resource?query=1",
        "status": 200
    }
    req = _parse_log_to_request(record)
    assert req is not None
    assert req.method == "PUT"
    assert req.path == "/path/to/resource"  # Extracted without query string/host
    
def test_invalid_method_discarded():
    record = {
        "method": "INVALID",
        "path": "/api",
        "status": 200
    }
    req = _parse_log_to_request(record)
    assert req is None
    
def test_empty_record_discarded():
    req = _parse_log_to_request({})
    assert req is None
