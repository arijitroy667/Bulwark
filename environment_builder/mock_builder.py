"""
Bulwark — WireMock Stub Generator.

Phase 1: Generates static WireMock stubs for each dependency endpoint.
Dependencies are identified from the Dynatrace service topology.

Each stub returns a generic 200 OK with a placeholder JSON body.
In Phase 2, this will be enhanced to return production-accurate responses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.models import ServiceTopology, WireMockStub

logger = logging.getLogger("bulwark.mock_builder")


# ──────────────────────────────────────────────
# Default response templates per service type
# ──────────────────────────────────────────────

DEFAULT_RESPONSES: dict[str, dict[str, Any]] = {
    # Structurally-valid placeholder bodies for WireMock simulation stubs.
    # These are NOT real production data. They exist so that the simulated
    # service can successfully parse dependency responses during replay.
    # Values are minimal and do not contain fabricated identifiers.
    "database": {
        "status": "connected",
        "latency_ms": 5,
    },
    "cache": {
        # Null value is intentional: the simulated service must handle cache
        # misses correctly — a null value exercises the miss-handling code path.
        "hit": True,
        "value": None,
        "ttl": 300,
    },
    "auth": {
        # Generic auth confirmation shape — no fake user IDs or tokens.
        "authenticated": True,
        "subject": "sim-user",
        "roles": ["user"],
    },
    "default": {
        "status": "ok",
    },
}


# ──────────────────────────────────────────────
# Stub generation
# ──────────────────────────────────────────────


def generate_stubs(topology: ServiceTopology) -> list[WireMockStub]:
    """
    Generate WireMock stubs for all downstream dependencies in the service topology.

    Phase 1: Returns static 200 OK responses for each dependency endpoint.
    """
    stubs = []

    for dependency in topology.dependencies:
        dep_type = _infer_dependency_type(dependency.name)
        default_body = DEFAULT_RESPONSES.get(dep_type, DEFAULT_RESPONSES["default"])

        # Create a stub for each known endpoint
        endpoints = dependency.endpoints or ["/"]
        for endpoint in endpoints:
            for method in ["GET", "POST", "PUT", "DELETE"]:
                stub = WireMockStub(
                    dependency_name=dependency.name,
                    request_method=method,
                    request_path=endpoint,
                    response_status=200,
                    response_body=default_body,
                    response_headers={"Content-Type": "application/json"},
                )
                stubs.append(stub)

        logger.info(
            "Generated %d stubs for dependency: %s (type: %s)",
            len(endpoints) * 4,
            dependency.name,
            dep_type,
        )

    logger.info(
        "Total stubs generated for %s: %d",
        topology.service_name,
        len(stubs),
    )
    return stubs


def stubs_to_wiremock_json(stubs: list[WireMockStub]) -> list[dict[str, Any]]:
    """
    Convert Bulwark WireMockStub objects to WireMock's mapping JSON format.

    Returns a list of WireMock mapping objects ready to be posted to
    the WireMock /__admin/mappings API.
    """
    mappings = []

    for stub in stubs:
        body = stub.response_body
        if isinstance(body, dict):
            body_str = json.dumps(body)
        else:
            body_str = str(body)

        mapping = {
            "request": {
                "method": stub.request_method,
                "urlPathPattern": _path_to_pattern(stub.request_path),
            },
            "response": {
                "status": stub.response_status,
                "body": body_str,
                "headers": stub.response_headers,
            },
            "metadata": {
                "bulwark_dependency": stub.dependency_name,
            },
        }
        mappings.append(mapping)

    return mappings


def _infer_dependency_type(name: str) -> str:
    """Infer the type of a dependency from its name for stub template selection."""
    name_lower = name.lower()
    if any(kw in name_lower for kw in ("db", "database", "postgres", "mysql", "mongo", "redis")):
        return "database"
    if any(kw in name_lower for kw in ("cache", "redis", "memcache")):
        return "cache"
    if any(kw in name_lower for kw in ("auth", "identity", "oauth", "keycloak")):
        return "auth"
    return "default"


def _path_to_pattern(path: str) -> str:
    """
    Convert a URL path to a WireMock URL pattern.
    Replaces path parameters with regex wildcards.
    """
    # Replace numeric segments with regex
    import re
    pattern = re.sub(r"/\d+", "/[^/]+", path)
    # Replace UUID segments with regex
    pattern = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "/[^/]+",
        pattern,
    )
    # Ensure it matches the full path
    if not pattern.endswith(".*"):
        pattern = pattern.rstrip("/") + "(/.*)?$"
    return pattern
