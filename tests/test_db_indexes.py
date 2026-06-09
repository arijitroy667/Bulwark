"""
Tests for MongoDB indexes (Phase 5 90-day TTL index).
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from pymongo import ASCENDING

from shared.db import _ensure_indexes

@pytest.mark.asyncio
async def test_ttl_index_configured_correctly():
    mock_db = MagicMock()
    mock_sims = AsyncMock()
    mock_db.__getitem__.side_effect = lambda name: mock_sims if name == "simulations" else AsyncMock()
    
    await _ensure_indexes(mock_db)
    
    # Check that create_indexes was called on simulations
    mock_sims.create_indexes.assert_called_once()
    
    # Find the TTL index in the call args
    indexes = mock_sims.create_indexes.call_args[0][0]
    
    ttl_index = None
    for idx in indexes:
        # Pymongo IndexModel kwargs are in document property
        if getattr(idx, "document", {}).get("expireAfterSeconds"):
            ttl_index = idx
            break
            
    assert ttl_index is not None, "TTL index not found"
    assert ttl_index.document["expireAfterSeconds"] == 7776000
    assert ttl_index.document["key"] == {"created_at": 1}
