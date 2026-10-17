"""Tether Command Line Interface."""

import typer
from rich.console import Console

app = typer.Typer(
    name="tether",
    help="Tether: Durable, event-triggered agent runtime with human review gate and atomic rollback.",
    add_completion=False,
)
console = Console()


@app.command()
def version():
    """Print the Tether version."""
    from tether import __version__

    console.print(f"[bold green]Tether[/bold green] runtime version [cyan]{__version__}[/cyan]")


@app.command()
def worker():
    """Start the Tether background execution worker."""
    console.print("[bold yellow]Starting Tether Worker...[/bold yellow]")
    from tether.worker import run_worker

    run_worker()


@app.command()
def api(
    host: str = typer.Option("0.0.0.0", help="Host address to bind to"),
    port: int = typer.Option(8000, help="Port to bind to"),
    reload: bool = typer.Option(False, help="Enable auto-reload for local development"),
):
    """Start the Tether FastAPI service."""
    import uvicorn

    console.print(f"[bold green]Starting Tether API on {host}:{port}...[/bold green]")
    uvicorn.run("tether.api:app", host=host, port=port, reload=reload)


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run ID to reconstruct from the BigQuery warehouse"),
    diff: str = typer.Option(None, "--diff", help="Second Run ID to diff against"),
):
    """Reconstruct an agent run timeline strictly from warehouse logs."""
    from tether.replay import run_replay

    run_replay(run_id=run_id, diff_run_id=diff)


if __name__ == "__main__":
    app()
