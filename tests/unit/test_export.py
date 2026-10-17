"""Unit tests for PII/secret redaction and BigQuery outbox export."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from tether.events import sanitize_payload, sanitize_string
from tether.export import BigQueryExporter


def test_secret_redaction_sanitizer():
    """Verify secrets, tokens, connection string passwords, and PII are redacted."""
    # OpenAI key
    raw_text = "Calling model with key sk-1234567890abcdef1234567890 for prompt."
    cleaned = sanitize_string(raw_text)
    assert "sk-" not in cleaned
    assert "[REDACTED_API_KEY]" in cleaned

    # Bearer token
    bearer_text = "Authorization: Bearer my-secret-jwt-token-value-12345"
    cleaned_bearer = sanitize_string(bearer_text)
    assert "my-secret-jwt" not in cleaned_bearer
    assert "[REDACTED_BEARER_TOKEN]" in cleaned_bearer

    # PostgreSQL password
    conn_str = "postgresql://postgres:mySuperSecretPassword123@aws.pooler.supabase.com:6543/postgres"
    cleaned_conn = sanitize_string(conn_str)
    assert "mySuperSecretPassword123" not in cleaned_conn
    assert "[REDACTED_PASSWORD]" in cleaned_conn

    # Nested dictionary with sensitive keys
    payload = {
        "user_email": "john@example.com",
        "api_key": "raw-key-123",
        "headers": {
            "Authorization": "Bearer super-secret-token-abcdef123456789",
        },
        "safe_number": 42,
    }
    sanitized = sanitize_payload(payload)
    assert sanitized["api_key"] == "[REDACTED_SECRET]"
    assert sanitized["headers"]["Authorization"] == "[REDACTED_SECRET]"
    assert sanitized["safe_number"] == 42


@pytest.mark.asyncio
async def test_bigquery_export_batch_success():
    """Verify outbox events are selected, sanitized, and streamed into BigQuery."""
    event_id = uuid4()
    run_id = uuid4()

    mock_row = {
        "id": event_id,
        "run_id": run_id,
        "agent_id": "digest_agent",
        "step_id": None,
        "ts": datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc),
        "type": "model_call",
        "payload": {"api_key": "secret-key", "text": "Model answer"},
        "tokens_in": 150,
        "tokens_out": 40,
        "cost_usd": 0.00045,
        "model": "gpt-4o-mini",
    }

    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [mock_row]
    mock_conn = AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_cursor():
        yield mock_cursor

    mock_conn.cursor = fake_cursor

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    mock_bq_client = MagicMock()
    mock_bq_client.insert_rows_json.return_value = []  # No errors

    exporter = BigQueryExporter(
        project_id="test-proj",
        dataset_id="test_ds",
        table_id="events",
        client=mock_bq_client,
    )

    with patch("tether.export.get_connection", side_effect=fake_get_connection):
        count = await exporter.export_batch(batch_size=100)

        assert count == 1
        mock_bq_client.insert_rows_json.assert_called_once()
        call_args = mock_bq_client.insert_rows_json.call_args[0]
        assert call_args[0] == "test-proj.test_ds.events"
        exported_row = call_args[1][0]
        # Verify secret was sanitized
        assert "secret-key" not in exported_row["payload"]
        assert "[REDACTED_SECRET]" in exported_row["payload"]


@pytest.mark.asyncio
async def test_bigquery_export_batch_error():
    """Verify RuntimeError is raised and transaction rolled back on BigQuery insert errors."""
    mock_row = {
        "id": uuid4(),
        "run_id": uuid4(),
        "agent_id": "digest_agent",
        "step_id": None,
        "ts": datetime.now(timezone.utc),
        "type": "run_started",
        "payload": {},
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "model": None,
    }

    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [mock_row]
    mock_conn = AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_cursor():
        yield mock_cursor

    mock_conn.cursor = fake_cursor

    @asynccontextmanager
    async def fake_get_connection():
        yield mock_conn

    mock_bq_client = MagicMock()
    mock_bq_client.insert_rows_json.return_value = [{"index": 0, "errors": ["Schema mismatch"]}]

    exporter = BigQueryExporter(client=mock_bq_client)

    with patch("tether.export.get_connection", side_effect=fake_get_connection):
        with pytest.raises(RuntimeError) as exc_info:
            await exporter.export_batch()

        assert "BigQuery streaming insert errors" in str(exc_info.value)
        mock_conn.rollback.assert_awaited_once()
