"""Zero-database run reconstruction and comparative diff CLI powered exclusively by BigQuery."""

import json
import logging
import os
from typing import Any, Optional
from uuid import UUID

from google.cloud import bigquery
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

logger = logging.getLogger(__name__)


def fetch_run_events_from_warehouse(
    run_id: str | UUID,
    client: Optional[bigquery.Client] = None,
    project_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    table_name: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Retrieve all structured events for a run directly and solely from Google BigQuery."""
    proj = project_id or os.getenv("GCP_PROJECT_ID", "local-dev-project")
    ds = dataset_id or os.getenv("BIGQUERY_DATASET", "tether_analytics")
    tbl = table_name or os.getenv("BIGQUERY_EVENTS_TABLE", "events")
    full_table = f"`{proj}.{ds}.{tbl}`"

    bq_client = client or bigquery.Client(project=proj)

    query = f"""
        SELECT id, run_id, agent_id, step_id, ts, type, payload,
               tokens_in, tokens_out, cost_usd, model
        FROM {full_table}
        WHERE run_id = @target_run_id
        ORDER BY ts ASC;
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_run_id", "STRING", str(run_id)),
        ]
    )

    query_job = bq_client.query(query, job_config=job_config)
    results = query_job.result()

    events: list[dict[str, Any]] = []
    for row in results:
        raw_payload = row["payload"]
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except Exception:
                raw_payload = {"raw": raw_payload}

        events.append(
            {
                "id": str(row["id"]),
                "run_id": str(row["run_id"]),
                "agent_id": str(row["agent_id"]),
                "step_id": str(row["step_id"]) if row["step_id"] else None,
                "ts": row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else str(row["ts"]),
                "type": str(row["type"]),
                "payload": raw_payload,
                "tokens_in": int(row["tokens_in"] or 0),
                "tokens_out": int(row["tokens_out"] or 0),
                "cost_usd": float(row["cost_usd"] or 0.0),
                "model": row["model"],
            }
        )

    return events


def reconstruct_run(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse raw warehouse event stream into a complete structured timeline and summary."""
    if not events:
        return {
            "run_id": "Unknown",
            "agent_id": "Unknown",
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_cost_usd": 0.0,
            "final_status": "empty",
            "timeline": [],
        }

    run_id = events[0]["run_id"]
    agent_id = events[0]["agent_id"]
    total_tokens_in = sum(e["tokens_in"] for e in events)
    total_tokens_out = sum(e["tokens_out"] for e in events)
    total_cost_usd = round(sum(e["cost_usd"] for e in events), 6)

    final_status = "in_progress"
    timeline: list[dict[str, Any]] = []

    for e in events:
        etype = e["type"]
        payload = e["payload"]

        item: dict[str, Any] = {
            "ts": e["ts"],
            "type": etype,
            "model": e["model"],
            "tokens_in": e["tokens_in"],
            "tokens_out": e["tokens_out"],
            "cost_usd": e["cost_usd"],
            "details": payload,
        }

        if etype == "run_finished":
            final_status = payload.get("final_state", payload.get("status", "committed"))
        elif etype == "run_failed":
            final_status = "failed"
        elif etype == "run_dead_lettered":
            final_status = "dead_lettered"
        elif etype == "approval_decision":
            if payload.get("decision") == "rejected":
                final_status = "rejected"

        timeline.append(item)

    return {
        "run_id": run_id,
        "agent_id": agent_id,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_cost_usd": total_cost_usd,
        "final_status": final_status,
        "timeline": timeline,
    }


def render_timeline(reconstructed: dict[str, Any], console: Optional[Console] = None) -> None:
    """Print the reconstructed run timeline to terminal with Rich styling."""
    c = console or Console()

    status_color = {
        "committed": "bold green",
        "approved": "bold cyan",
        "rejected": "bold yellow",
        "rolled_back": "bold magenta",
        "rollback_failed": "bold red",
        "failed": "bold red",
        "dead_lettered": "bold red",
    }.get(reconstructed["final_status"], "bold white")

    # Header Summary Panel
    header_text = (
        f"[bold]Run ID:[/bold] {reconstructed['run_id']}\n"
        f"[bold]Agent:[/bold] [cyan]{reconstructed['agent_id']}[/cyan]\n"
        f"[bold]Final Status:[/bold] [{status_color}]{reconstructed['final_status'].upper()}[/{status_color}]\n"
        f"[bold]Tokens:[/bold] Prompt: {reconstructed['total_tokens_in']} | "
        f"Completion: {reconstructed['total_tokens_out']} | "
        f"Total: {reconstructed['total_tokens_in'] + reconstructed['total_tokens_out']}\n"
        f"[bold]Financial Cost:[/bold] [bold green]${reconstructed['total_cost_usd']:.6f} USD[/bold green]"
    )
    c.print(Panel(header_text, title="[bold]Tether Run Reconstruction (Warehouse Only)[/bold]", border_style="blue"))

    tree = Tree(f"[bold cyan]Timeline: {reconstructed['agent_id']} ({reconstructed['run_id']})[/bold cyan]")

    for idx, item in enumerate(reconstructed["timeline"], 1):
        ts = item["ts"]
        etype = item["type"]
        details = item["details"]

        if etype == "run_enqueued":
            node = tree.add(f"[dim]{ts}[/dim] 📥 [bold blue]Run Enqueued[/bold blue] (Trigger: {details.get('trigger')})")
        elif etype == "run_started":
            node = tree.add(f"[dim]{ts}[/dim] 🚀 [bold green]Worker Started Execution[/bold green]")
        elif etype == "model_call":
            cost_str = f" (${item['cost_usd']:.5f})" if item['cost_usd'] > 0 else ""
            node = tree.add(
                f"[dim]{ts}[/dim] 🧠 [bold yellow]Model Call ({item['model']})[/bold yellow] "
                f"[dim]Tokens: in={item['tokens_in']}, out={item['tokens_out']}{cost_str}[/dim]"
            )
            if details.get("text"):
                node.add(f"[italic]Thought:[/italic] {details['text'][:150]}...")
        elif etype == "tool_call":
            node = tree.add(
                f"[dim]{ts}[/dim] 🔍 [bold cyan]Read Tool Call: {details.get('tool_name')}[/bold cyan] "
                f"[dim]args={details.get('arguments')}[/dim]"
            )
        elif etype == "effect_staged":
            node = tree.add(
                f"[dim]{ts}[/dim] 🔒 [bold magenta]Side Effect Staged: {details.get('action')}[/bold magenta] "
                f"(Target: {details.get('target_system')})"
            )
        elif etype == "awaiting_approval":
            node = tree.add(f"[dim]{ts}[/dim] ⏳ [bold yellow]Human Gate: Awaiting Reviewer Decision[/bold yellow]")
        elif etype == "approval_decision":
            dec = details.get("decision", "").upper()
            dec_color = "green" if dec == "APPROVED" else "yellow"
            node = tree.add(
                f"[dim]{ts}[/dim] 👤 [bold {dec_color}]Human Decision: {dec}[/bold {dec_color}] "
                f"by {details.get('decided_by')} (hash: {details.get('effects_hash', '')[:12]}...)"
            )
        elif etype == "commit_started":
            node = tree.add(f"[dim]{ts}[/dim] ⚡ [bold cyan]Executing Approved Staged Effects[/bold cyan]")
        elif etype == "effect_committed":
            node = tree.add(f"[dim]{ts}[/dim] ✅ [bold green]Committed Effect: {details.get('action')}[/bold green]")
        elif etype == "rollback_started":
            node = tree.add(f"[dim]{ts}[/dim] ⚠️ [bold red]Failure Detected! Unwinding Compensations in Reverse[/bold red]")
        elif etype == "effect_compensated":
            node = tree.add(
                f"[dim]{ts}[/dim] 🔄 [bold magenta]Compensated: {details.get('action')}[/bold magenta] "
                f"via `{details.get('compensation_action')}`"
            )
        elif etype == "run_finished":
            node = tree.add(f"[dim]{ts}[/dim] 🏁 [bold green]Run Completed Successfully[/bold green]")
        elif etype == "run_failed":
            node = tree.add(f"[dim]{ts}[/dim] ❌ [bold red]Run Failed: {details.get('error')}[/bold red]")
        else:
            node = tree.add(f"[dim]{ts}[/dim] ℹ️ {etype}")

    c.print(tree)


def render_diff(
    events_a: list[dict[str, Any]],
    events_b: list[dict[str, Any]],
    console: Optional[Console] = None,
) -> None:
    """Render side-by-side comparative table between two runs."""
    c = console or Console()
    rec_a = reconstruct_run(events_a)
    rec_b = reconstruct_run(events_b)

    table = Table(title=f"Comparative Run Diff: {rec_a['run_id']} vs {rec_b['run_id']}")
    table.add_column("Dimension", style="bold cyan")
    table.add_column(f"Run A ({rec_a['run_id'][:8]}...)", style="green")
    table.add_column(f"Run B ({rec_b['run_id'][:8]}...)", style="yellow")
    table.add_column("Delta / Divergence", style="magenta")

    table.add_row("Agent ID", rec_a["agent_id"], rec_b["agent_id"], "Match" if rec_a["agent_id"] == rec_b["agent_id"] else "Different")
    table.add_row("Final Status", rec_a["final_status"], rec_b["final_status"], "Match" if rec_a["final_status"] == rec_b["final_status"] else "Diverged")
    table.add_row("Event Count", str(len(rec_a["timeline"])), str(len(rec_b["timeline"])), str(len(rec_b["timeline"]) - len(rec_a["timeline"])))
    table.add_row("Tokens In", str(rec_a["total_tokens_in"]), str(rec_b["total_tokens_in"]), str(rec_b["total_tokens_in"] - rec_a["total_tokens_in"]))
    table.add_row("Tokens Out", str(rec_a["total_tokens_out"]), str(rec_b["total_tokens_out"]), str(rec_b["total_tokens_out"] - rec_a["total_tokens_out"]))
    cost_diff = rec_b["total_cost_usd"] - rec_a["total_cost_usd"]
    table.add_row("Total Cost", f"${rec_a['total_cost_usd']:.6f}", f"${rec_b['total_cost_usd']:.6f}", f"{'+' if cost_diff >= 0 else ''}${cost_diff:.6f}")

    c.print(table)


def run_replay(run_id: str, diff_run_id: Optional[str] = None) -> None:
    """Execute the replay command from CLI."""
    console = Console()
    console.print(f"[bold blue]Fetching events from BigQuery for run {run_id}...[/bold blue]")

    try:
        events = fetch_run_events_from_warehouse(run_id)
        if not events:
            console.print(f"[bold red]No warehouse events found in BigQuery for run {run_id}.[/bold red]")
            return

        reconstructed = reconstruct_run(events)
        render_timeline(reconstructed, console=console)

        if diff_run_id:
            console.print(f"\n[bold blue]Fetching second run {diff_run_id} for diff...[/bold blue]")
            diff_events = fetch_run_events_from_warehouse(diff_run_id)
            if not diff_events:
                console.print(f"[bold red]No events found for second run {diff_run_id}.[/bold red]")
                return
            render_diff(events, diff_events, console=console)

    except Exception as e:
        console.print(f"[bold red]Replay error: {e}[/bold red]")
