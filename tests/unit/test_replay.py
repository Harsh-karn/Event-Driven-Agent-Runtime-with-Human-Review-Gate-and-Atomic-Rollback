"""Unit tests for BigQuery run reconstruction and timeline replay."""

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from rich.console import Console

from tether.replay import (
    fetch_run_events_from_warehouse,
    reconstruct_run,
    render_diff,
    render_timeline,
)


def test_reconstruct_run_timeline():
    """Verify raw warehouse events are parsed into structured timeline and metrics."""
    run_id = str(uuid4())
    events = [
        {
            "id": "e-1",
            "run_id": run_id,
            "agent_id": "digest_agent",
            "step_id": None,
            "ts": datetime(2026, 10, 17, 10, 0, 0, tzinfo=timezone.utc),
            "type": "run_enqueued",
            "payload": {"trigger": "webhook"},
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "model": None,
        },
        {
            "id": "e-2",
            "run_id": run_id,
            "agent_id": "digest_agent",
            "step_id": "s-1",
            "ts": datetime(2026, 10, 17, 10, 0, 1, tzinfo=timezone.utc),
            "type": "model_call",
            "payload": {"model": "gpt-4o-mini", "text": "Analyzing system health"},
            "tokens_in": 300,
            "tokens_out": 80,
            "cost_usd": 0.000093,
            "model": "gpt-4o-mini",
        },
        {
            "id": "e-3",
            "run_id": run_id,
            "agent_id": "digest_agent",
            "step_id": "s-2",
            "ts": datetime(2026, 10, 17, 10, 0, 3, tzinfo=timezone.utc),
            "type": "effect_staged",
            "payload": {"action": "post_slack", "target_system": "slack"},
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "model": None,
        },
        {
            "id": "e-4",
            "run_id": run_id,
            "agent_id": "digest_agent",
            "step_id": None,
            "ts": datetime(2026, 10, 17, 10, 1, 0, tzinfo=timezone.utc),
            "type": "approval_decision",
            "payload": {"decision": "approved", "decided_by": "ops@tether.dev"},
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "model": None,
        },
        {
            "id": "e-5",
            "run_id": run_id,
            "agent_id": "digest_agent",
            "step_id": None,
            "ts": datetime(2026, 10, 17, 10, 1, 5, tzinfo=timezone.utc),
            "type": "run_finished",
            "payload": {"final_state": "committed"},
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "model": None,
        },
    ]

    reconstructed = reconstruct_run(events)

    assert reconstructed["run_id"] == run_id
    assert reconstructed["agent_id"] == "digest_agent"
    assert reconstructed["total_tokens_in"] == 300
    assert reconstructed["total_tokens_out"] == 80
    assert reconstructed["total_cost_usd"] == 0.000093
    assert reconstructed["final_status"] == "committed"
    assert len(reconstructed["timeline"]) == 5

    # Test rendering to virtual console
    test_console = Console(record=True, width=120)
    render_timeline(reconstructed, console=test_console)
    output = test_console.export_text()
    assert "Tether Run Reconstruction" in output
    assert "digest_agent" in output
    assert "COMMITTED" in output


def test_render_diff():
    """Verify comparative diff between two runs renders table correctly."""
    run_a_id = "run-a-111"
    run_b_id = "run-b-222"

    events_a = [
        {"run_id": run_a_id, "agent_id": "digest", "ts": "2026-10-17T10:00:00Z", "type": "run_finished", "payload": {"final_state": "committed"}, "tokens_in": 500, "tokens_out": 100, "cost_usd": 0.001, "model": "gpt-4o"},
    ]
    events_b = [
        {"run_id": run_b_id, "agent_id": "digest", "ts": "2026-10-17T11:00:00Z", "type": "run_finished", "payload": {"final_state": "committed"}, "tokens_in": 800, "tokens_out": 200, "cost_usd": 0.002, "model": "gpt-4o"},
    ]

    test_console = Console(record=True, width=120)
    render_diff(events_a, events_b, console=test_console)
    output = test_console.export_text()

    assert "Comparative Run Diff" in output
    assert "run-a-11" in output
    assert "run-b-22" in output


def test_fetch_run_events_from_warehouse_calls_bq():
    """Verify fetch_run_events_from_warehouse executes BigQuery query with parameters."""
    mock_row = {
        "id": "e-1",
        "run_id": "run-xyz",
        "agent_id": "digest",
        "step_id": None,
        "ts": datetime.now(timezone.utc),
        "type": "run_started",
        "payload": '{"trigger": "api"}',
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "model": None,
    }

    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = [mock_row]
    mock_client.query.return_value = mock_query_job

    events = fetch_run_events_from_warehouse("run-xyz", client=mock_client)

    assert len(events) == 1
    assert events[0]["run_id"] == "run-xyz"
    mock_client.query.assert_called_once()
    query_str = mock_client.query.call_args[0][0]
    assert "FROM `local-dev-project.tether_analytics.events`" in query_str
    assert "WHERE run_id = @target_run_id" in query_str
