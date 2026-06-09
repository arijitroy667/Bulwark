"""
Tests for Phase 5 bounded MCP retries.
"""
from unittest.mock import AsyncMock, patch
import asyncio
import pytest

from shared.mcp_client import _do_call_with_retry, MCPCallError, MCP_MAX_RETRIES

@pytest.fixture
def mock_sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock) as m_sleep:
        yield m_sleep

@pytest.mark.asyncio
async def test_transient_error_retried_3_times(mock_sleep):
    mock_do_call = AsyncMock(side_effect=ConnectionError("Connection refused"))
    
    with patch("shared.mcp_client._do_call", mock_do_call):
        with pytest.raises(MCPCallError, match="failed after 4 attempts"):
            await _do_call_with_retry("url", "tool", {}, 30.0, "server", max_retries=3)
            
    assert mock_do_call.call_count == 4  # 1 initial + 3 retries
    assert mock_sleep.call_count == 3
    # Check backoff multiplier
    assert mock_sleep.call_args_list[0].args[0] == 1  # 2^0
    assert mock_sleep.call_args_list[1].args[0] == 2  # 2^1
    assert mock_sleep.call_args_list[2].args[0] == 4  # 2^2

@pytest.mark.asyncio
async def test_timeout_error_retried(mock_sleep):
    mock_do_call = AsyncMock(side_effect=asyncio.TimeoutError("Timeout"))
    
    with patch("shared.mcp_client._do_call", mock_do_call):
        with pytest.raises(MCPCallError, match="failed after 4 attempts"):
            await _do_call_with_retry("url", "tool", {}, 30.0, "server", max_retries=3)
            
    assert mock_do_call.call_count == 4

@pytest.mark.asyncio
async def test_success_on_second_attempt(mock_sleep):
    # Fail first, succeed second
    mock_do_call = AsyncMock(side_effect=[ConnectionError("Temporary"), {"status": "ok"}])
    
    with patch("shared.mcp_client._do_call", mock_do_call):
        result = await _do_call_with_retry("url", "tool", {}, 30.0, "server", max_retries=3)
            
    assert mock_do_call.call_count == 2
    assert mock_sleep.call_count == 1
    assert result == {"status": "ok"}

@pytest.mark.asyncio
async def test_tool_level_error_not_retried(mock_sleep):
    # If the MCPCallError was raised natively without being caught as a transient exception,
    # it means it was a tool-level error (isError=True) or explicit failure.
    mock_do_call = AsyncMock(side_effect=MCPCallError("Server returned tool error"))
    
    with patch("shared.mcp_client._do_call", mock_do_call):
        with pytest.raises(MCPCallError, match="Server returned tool error"):
            await _do_call_with_retry("url", "tool", {}, 30.0, "server", max_retries=3)
            
    # Should fail immediately on the first attempt without retrying
    assert mock_do_call.call_count == 1
    assert mock_sleep.call_count == 0
