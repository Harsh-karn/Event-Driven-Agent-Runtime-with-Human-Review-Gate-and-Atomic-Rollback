"""PostgreSQL Outbox to Google BigQuery batch exporter daemon with PII redaction."""

import asyncio
import json
import logging
import os
from typing import Any, Optional

from google.cloud import bigquery

from tether.db import get_connection
from tether.events import sanitize_payload

logger = logging.getLogger(__name__)


class BigQueryExporter:
    """Streams structured event telemetry from the PostgreSQL outbox table to BigQuery."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        table_id: Optional[str] = None,
        client: Optional[bigquery.Client] = None,
    ):
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "local-dev-project")
        self.dataset_id = dataset_id or os.getenv("BIGQUERY_DATASET", "tether_analytics")
        self.table_name = table_id or os.getenv("BIGQUERY_EVENTS_TABLE", "events")
        self.full_table_id = f"{self.project_id}.{self.dataset_id}.{self.table_name}"
        self._client = client

    @property
    def client(self) -> bigquery.Client:
        """Lazily initialize BigQuery client."""
        if self._client is None:
            self._client = bigquery.Client(project=self.project_id)
        return self._client

    async def export_batch(self, batch_size: int = 500) -> int:
        """Fetch a batch of unexported events, scrub secrets, and load into BigQuery.

        Returns:
            The count of exported events.
        """
        async with get_connection() as conn:
            async with conn.cursor() as cur:
                # Atomically select unexported rows locking them with SKIP LOCKED
                await cur.execute(
                    """
                    SELECT e.id, e.run_id, r.agent_id, e.step_id, e.ts, e.type,
                           e.payload, e.tokens_in, e.tokens_out, e.cost_usd, e.model
                    FROM events e
                    JOIN runs r ON e.run_id = r.id
                    WHERE e.exported_at IS NULL
                    ORDER BY e.ts ASC
                    LIMIT %s
                    FOR UPDATE OF e SKIP LOCKED;
                    """,
                    (batch_size,),
                )
                rows = await cur.fetchall()

                if not rows:
                    await conn.commit()
                    return 0

                event_ids = [r["id"] for r in rows]
                bq_rows: list[dict[str, Any]] = []

                for r in rows:
                    raw_payload = r["payload"]
                    if isinstance(raw_payload, str):
                        try:
                            raw_payload = json.loads(raw_payload)
                        except Exception:
                            raw_payload = {"raw": raw_payload}

                    # Sanitize credentials and sensitive PII
                    clean_payload = sanitize_payload(raw_payload)

                    bq_rows.append(
                        {
                            "id": str(r["id"]),
                            "run_id": str(r["run_id"]),
                            "agent_id": str(r["agent_id"]),
                            "step_id": str(r["step_id"]) if r["step_id"] else None,
                            "ts": r["ts"].isoformat(),
                            "type": r["type"],
                            "payload": json.dumps(clean_payload),
                            "tokens_in": int(r["tokens_in"]),
                            "tokens_out": int(r["tokens_out"]),
                            "cost_usd": float(r["cost_usd"]),
                            "model": r["model"],
                        }
                    )

                # Stream rows into BigQuery asynchronously without blocking event loop,
                # passing row_ids for server-side deduplication against duplicate loads
                row_ids = [str(r["id"]) for r in rows]
                errors = await asyncio.to_thread(
                    self.client.insert_rows_json,
                    self.full_table_id,
                    bq_rows,
                    row_ids=row_ids,
                )
                if errors:
                    error_msg = f"BigQuery streaming insert errors: {errors}"
                    logger.error(error_msg)
                    await conn.rollback()
                    raise RuntimeError(error_msg)

                # Mark events as successfully exported in PostgreSQL
                await cur.execute(
                    """
                    UPDATE events
                    SET exported_at = NOW()
                    WHERE id = ANY(%s);
                    """,
                    (event_ids,),
                )
                await conn.commit()

                logger.info(f"Exported {len(bq_rows)} events to BigQuery table {self.full_table_id}")
                return len(bq_rows)

    async def run_daemon(self, interval_seconds: float = 10.0, batch_size: int = 500) -> None:
        """Run continuous export loop with periodic polling."""
        logger.info(f"Starting BigQuery outbox exporter daemon (interval={interval_seconds}s)...")
        while True:
            try:
                exported_count = await self.export_batch(batch_size=batch_size)
                if exported_count < batch_size:
                    # Queue drained; wait for interval
                    await asyncio.sleep(interval_seconds)
            except Exception as e:
                logger.error(f"Error in BigQuery exporter loop: {e}", exc_info=True)
                await asyncio.sleep(interval_seconds)
