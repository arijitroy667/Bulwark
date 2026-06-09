"""
Tests for Phase 4 minimum sample enforcement in traffic replay.
"""
from unittest.mock import AsyncMock, patch
import pytest

from orchestrator.agent import run_simulation
from shared.models import ProductionRequest

@pytest.fixture
def mock_db_agent():
    with patch("shared.db.update_simulation", new_callable=AsyncMock) as mock_update, \
         patch("shared.db.get_simulation", new_callable=AsyncMock) as mock_get, \
         patch("shared.db.get_verdict", new_callable=AsyncMock) as mock_get_verdict, \
         patch("shared.db.save_verdict", new_callable=AsyncMock) as mock_save_verdict, \
         patch("shared.db.save_environment_config", new_callable=AsyncMock) as mock_save_env, \
         patch("orchestrator.agent._fail_simulation", new_callable=AsyncMock) as mock_fail, \
         patch("shared.db.log_override_event", new_callable=AsyncMock) as mock_override:
        
        mock_get.return_value = {
            "_id": "sim-123",
            "mr_id": 42,
            "project_id": "proj-1",
            "head_sha": "abc",
            "source_branch": "feature",
            "target_branch": "main",
            "status": "queued",
        }
        yield {
            "update_simulation": mock_update,
            "get_simulation": mock_get,
        }

@pytest.fixture
def mock_tools():
    # Patch all the dynamic imports used in run_simulation
    with patch("orchestrator.tools.gitlab_tool.get_mr_details", new_callable=AsyncMock) as mock_mr, \
         patch("orchestrator.tools.gitlab_tool.get_mr_diff", new_callable=AsyncMock) as mock_diff, \
         patch("orchestrator.tools.gitlab_tool.identify_services_from_diff") as mock_svc, \
         patch("orchestrator.tools.gitlab_tool.get_file_contents_batch", new_callable=AsyncMock) as mock_files, \
         patch("verdict_writer.writer.enforce_verdict", new_callable=AsyncMock) as mock_enforce, \
         patch("orchestrator.tools.dynatrace_tool.has_active_incident", new_callable=AsyncMock) as mock_inc, \
         patch("orchestrator.tools.dynatrace_tool.get_service_baseline", new_callable=AsyncMock) as mock_base, \
         patch("orchestrator.tools.dynatrace_tool.get_service_topology", new_callable=AsyncMock) as mock_topo, \
         patch("orchestrator.tools.dynatrace_tool.get_production_requests", new_callable=AsyncMock) as mock_reqs, \
         patch("orchestrator.tools.arize_tool.get_llm_baseline", new_callable=AsyncMock) as mock_arize_base, \
         patch("environment_builder.builder.provision_environment", new_callable=AsyncMock) as mock_env, \
         patch("environment_builder.builder.teardown_environment", new_callable=AsyncMock) as mock_tear, \
         patch("traffic_replay.replayer.replay_traffic", new_callable=AsyncMock) as mock_replay, \
         patch("orchestrator.reasoning.compute_scores_locally") as mock_local, \
         patch("orchestrator.reasoning.generate_verdict", new_callable=AsyncMock) as mock_verdict, \
         patch("orchestrator.agent._get_simulation", new_callable=AsyncMock) as mock_sim, \
         patch("orchestrator.agent.update_simulation", new_callable=AsyncMock) as mock_upd_sim, \
         patch("orchestrator.agent._should_abort_cancelled", new_callable=AsyncMock) as mock_abort:
        
        from shared.models import MergeRequestDetails, ServiceBaseline, EphemeralEnvironment, ServiceTopology, VerdictResult, VerdictStatus
        import datetime
        from shared.models import MREventMessage
        
        mock_mr.return_value = MergeRequestDetails(
            mr_id=42, title="Test", source_branch="a", target_branch="b", author="usr", changed_files=["app.py"],
            has_llm_components=False, head_sha="abc"
        )
        mock_svc.return_value = ["test-svc"]
        mock_diff.return_value = []
        mock_files.return_value = {}
        mock_inc.return_value = False
        mock_base.return_value = ServiceBaseline(service_name="test-svc")
        mock_topo.return_value = ServiceTopology(service_name="test-svc")
        mock_arize_base.return_value = None
        mock_abort.return_value = False
        mock_sim.return_value = {"status": "queued"}
        
        mock_env.return_value = EphemeralEnvironment(
            simulation_id="sim-123", base_url="http://a", mock_base_url="http://b", status="healthy", deployed_at=datetime.datetime.now(datetime.timezone.utc)
        )
        
        verdict = VerdictResult(verdict=VerdictStatus.APPROVED, overall_score=90)
        mock_local.return_value = verdict
        mock_verdict.return_value = verdict
        
        yield {
            "get_production_requests": mock_reqs,
            "replay_traffic": mock_replay,
        }

@pytest.mark.asyncio
async def test_fewer_than_50_samples_skips_replay_scoring(mock_db_agent, mock_tools):
    print("TEST START")
    requests = [ProductionRequest(method="GET", path="/test", headers={}, body=None, expected_status=200)] * 10
    mock_tools["get_production_requests"].return_value = requests
    
    from shared.models import MREventMessage
    msg = MREventMessage(simulation_id="sim-123", project_id="proj-1", mr_id=42, source_branch="feature", target_branch="main")
    
    # We patch update_simulation globally, so we can inspect its calls
    with patch("orchestrator.agent.update_simulation", new_callable=AsyncMock) as mock_upd:
        print("CALLING run_simulation")
        await run_simulation(msg)
        print("run_simulation FINISHED")
        
        # Assert replay_traffic was NOT called
        mock_tools["replay_traffic"].assert_not_called()
        
        # Assert skip message is in error log somewhere
        updates = mock_upd.call_args_list
        found_skip = False
        for call in updates:
            if "error_log" in call.args[1]:
                if any("insufficient real production samples" in log for log in call.args[1]["error_log"]):
                    found_skip = True
        print("FOUND SKIP:", found_skip)
        assert found_skip

@pytest.mark.asyncio
async def test_exactly_50_samples_proceeds(mock_db_agent, mock_tools):
    requests = [ProductionRequest(method="GET", path="/test", headers={}, body=None, expected_status=200)] * 50
    mock_tools["get_production_requests"].return_value = requests
    
    from shared.models import ReplayResult
    mock_tools["replay_traffic"].return_value = ReplayResult(simulation_id="sim-123", total_requests=50)
    
    from shared.models import MREventMessage
    msg = MREventMessage(simulation_id="sim-123", project_id="proj-1", mr_id=42, source_branch="feature", target_branch="main")
    await run_simulation(msg)
    
    # Assert replay_traffic WAS called
    mock_tools["replay_traffic"].assert_called_once()

