#!/usr/bin/env python3
"""
Thaqip Fleet CLI Client
Command-line interface for triggering and monitoring fleet operations.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich.progress import Progress, SpinnerColumn, TextColumn

app = typer.Typer(help="Thaqip Agent Fleet CLI")
console = Console()

# Default API URL
DEFAULT_API_URL = "http://localhost:8766"
API_URL = os.environ.get("THAQIP_FLEET_API", DEFAULT_API_URL)


class FleetClient:
    def __init__(self, base_url: str = API_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self.client.aclose()

    async def health(self) -> dict:
        resp = await self.client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()

    async def list_templates(self) -> dict:
        resp = await self.client.get(f"{self.base_url}/templates")
        resp.raise_for_status()
        return resp.json()

    async def list_profiles(self) -> dict:
        resp = await self.client.get(f"{self.base_url}/profiles")
        resp.raise_for_status()
        return resp.json()

    async def start_operation(self, template_id: str, trigger_data: dict, background: bool = True) -> dict:
        resp = await self.client.post(
            f"{self.base_url}/operations",
            json={"template_id": template_id, "trigger_data": trigger_data, "background": background},
        )
        resp.raise_for_status()
        return resp.json()

    async def list_operations(self, limit: int = 100, status: str | None = None) -> list:
        params = {"limit": limit}
        if status:
            params["status"] = status
        resp = await self.client.get(f"{self.base_url}/operations", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_operation(self, op_id: str) -> dict:
        resp = await self.client.get(f"{self.base_url}/operations/{op_id}")
        resp.raise_for_status()
        return resp.json()

    async def cancel_operation(self, op_id: str) -> dict:
        resp = await self.client.post(f"{self.base_url}/operations/{op_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    async def wait_for_operation(self, op_id: str, poll_interval: int = 5, timeout: int = 3600) -> dict:
        """Wait for an operation to complete."""
        start = datetime.now(UTC)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Waiting for operation {op_id}...", total=None)
            while True:
                op = await self.get_operation(op_id)
                status = op["status"]
                progress.update(task, description=f"Operation {op_id}: {status}")
                
                if status in ("completed", "failed", "cancelled"):
                    return op
                
                if (datetime.now(UTC) - start).total_seconds() > timeout:
                    raise TimeoutError(f"Operation {op_id} timed out after {timeout}s")
                
                await asyncio.sleep(poll_interval)


async def get_client() -> FleetClient:
    return FleetClient()


@app.command()
def health():
    """Check fleet manager health."""
    async def _health():
        client = await get_client()
        try:
            result = await client.health()
            console.print(Panel.fit(
                f"[green]Status:[/green] {result['status']}\n"
                f"[green]Running Operations:[/green] {result['running_operations']}/{result['max_concurrent']}\n"
                f"[green]Profiles:[/green] {result['profiles_loaded']}\n"
                f"[green]Operations:[/green] {result['operations_loaded']}\n"
                f"[green]Time:[/green] {result['timestamp']}",
                title="Fleet Health",
                border_style="green",
            ))
        except Exception as e:
            console.print(f"[red]Health check failed: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_health())


@app.command()
def templates():
    """List available operation templates."""
    async def _templates():
        client = await get_client()
        try:
            result = await client.list_templates()
            table = Table(title="Operation Templates")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="green")
            table.add_column("Description")
            table.add_column("Trigger", style="yellow")
            table.add_column("Schedule")
            table.add_column("Agents")
            table.add_column("Timeout (s)", justify="right")
            
            for tid, t in result.items():
                table.add_row(
                    tid,
                    t["name"],
                    t["description"][:60] + "..." if len(t["description"]) > 60 else t["description"],
                    t["trigger"],
                    t["schedule"] or "-",
                    ", ".join(t["agents"]),
                    str(t["timeout_seconds"]),
                )
            console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to list templates: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_templates())


@app.command()
def profiles():
    """List available agent profiles."""
    async def _profiles():
        client = await get_client()
        try:
            result = await client.list_profiles()
            table = Table(title="Agent Profiles")
            table.add_column("Name", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Model", style="yellow")
            table.add_column("Description")
            table.add_column("Skills")
            
            for name, p in result.items():
                table.add_row(
                    name,
                    p["title"],
                    p["model"],
                    p["description"][:50] + "..." if len(p["description"]) > 50 else p["description"],
                    ", ".join(p["skills"][:3]) + ("..." if len(p["skills"]) > 3 else ""),
                )
            console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to list profiles: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_profiles())


@app.command()
def run(
    template_id: str = typer.Argument(..., help="Operation template ID"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for completion"),
    **trigger_data: str,
):
    """Start an operation (trigger_data as key=value pairs)."""
    async def _run():
        client = await get_client()
        try:
            # Parse trigger_data from CLI
            parsed_data = {}
            for k, v in trigger_data.items():
                # Try to parse as JSON, fallback to string
                try:
                    parsed_data[k] = json.loads(v)
                except json.JSONDecodeError:
                    parsed_data[k] = v
            
            console.print(f"[cyan]Starting operation:[/cyan] {template_id}")
            console.print(f"[cyan]Trigger data:[/cyan] {json.dumps(parsed_data, indent=2)}")
            
            result = await client.start_operation(template_id, parsed_data)
            op_id = result["id"]
            console.print(f"[green]Operation started:[/green] {op_id} (status: {result['status']})")
            
            if wait:
                console.print("[yellow]Waiting for completion...[/yellow]")
                final = await client.wait_for_operation(op_id)
                console.print(f"\n[bold]Final Status:[/bold] {final['status']}")
                if final.get("final_result"):
                    console.print(Panel(
                        Syntax(json.dumps(final["final_result"], indent=2), "json"),
                        title="Final Result",
                        border_style="green" if final["status"] == "completed" else "red",
                    ))
                if final.get("error"):
                    console.print(f"[red]Error:[/red] {final['error']}")
        except Exception as e:
            console.print(f"[red]Operation failed: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_run())


@app.command()
def ls(
    limit: int = typer.Option(20, "--limit", "-n"),
    status: str = typer.Option(None, "--status", "-s", help="Filter by status"),
):
    """List recent operations."""
    async def _ls():
        client = await get_client()
        try:
            ops = await client.list_operations(limit, status)
            if not ops:
                console.print("[yellow]No operations found[/yellow]")
                return
            
            table = Table(title=f"Recent Operations (limit={limit})")
            table.add_column("ID", style="cyan")
            table.add_column("Template", style="green")
            table.add_column("Status", style="yellow")
            table.add_column("Created", style="dim")
            table.add_column("Started")
            table.add_column("Completed")
            table.add_column("Error")
            
            for op in ops:
                status_style = {
                    "pending": "yellow",
                    "running": "blue",
                    "completed": "green",
                    "failed": "red",
                    "cancelled": "dim",
                }.get(op["status"], "white")
                
                table.add_row(
                    op["id"],
                    op["template_id"],
                    f"[{status_style}]{op['status']}[/{status_style}]",
                    op["created_at"][:19].replace("T", " "),
                    op["started_at"][:19].replace("T", " ") if op["started_at"] else "-",
                    op["completed_at"][:19].replace("T", " ") if op["completed_at"] else "-",
                    op["error"][:40] + "..." if op.get("error") and len(op["error"]) > 40 else op.get("error", "-"),
                )
            console.print(table)
        except Exception as e:
            console.print(f"[red]Failed to list operations: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_ls())


@app.command()
def show(op_id: str):
    """Show operation details."""
    async def _show():
        client = await get_client()
        try:
            op = await client.get_operation(op_id)
            console.print(Panel(
                f"[cyan]ID:[/cyan] {op['id']}\n"
                f"[cyan]Template:[/cyan] {op['template_id']}\n"
                f"[cyan]Status:[/cyan] {op['status']}\n"
                f"[cyan]Created:[/cyan] {op['created_at']}\n"
                f"[cyan]Started:[/cyan] {op.get('started_at', '-')}\n"
                f"[cyan]Completed:[/cyan] {op.get('completed_at', '-')}\n"
                f"[cyan]Trigger Data:[/cyan] {json.dumps(op['trigger_data'], indent=2)}",
                title=f"Operation {op_id}",
                border_style="blue",
            ))
            
            if op.get("final_result"):
                console.print(Panel(
                    Syntax(json.dumps(op["final_result"], indent=2), "json"),
                    title="Final Result",
                    border_style="green",
                ))
            
            if op.get("error"):
                console.print(Panel(
                    op["error"],
                    title="Error",
                    border_style="red",
                ))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                console.print(f"[red]Operation {op_id} not found[/red]")
            else:
                console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]Failed to get operation: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_show())


@app.command()
def cancel(op_id: str):
    """Cancel a running operation."""
    async def _cancel():
        client = await get_client()
        try:
            result = await client.cancel_operation(op_id)
            console.print(f"[green]{result['status']}[/green]")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                console.print(f"[red]Operation {op_id} not found or not running[/red]")
            else:
                console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"[red]Failed to cancel: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_cancel())


@app.command()
def tail(op_id: str, interval: int = typer.Option(3, "--interval", "-i")):
    """Tail operation status until completion."""
    async def _tail():
        client = await get_client()
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task(f"Tailing {op_id}...", total=None)
                while True:
                    op = await client.get_operation(op_id)
                    status = op["status"]
                    progress.update(task, description=f"{op_id}: {status}")
                    
                    if status in ("completed", "failed", "cancelled"):
                        break
                    await asyncio.sleep(interval)
                
                # Show final result
                op = await client.get_operation(op_id)
                console.print(f"\n[bold]Final Status:[/bold] {op['status']}")
                if op.get("final_result"):
                    console.print(Syntax(json.dumps(op["final_result"], indent=2), "json"))
                if op.get("error"):
                    console.print(f"[red]Error:[/red] {op['error']}")
        except Exception as e:
            console.print(f"[red]Tail failed: {e}[/red]")
            raise typer.Exit(1)
        finally:
            await client.close()
    
    asyncio.run(_tail())


if __name__ == "__main__":
    app()