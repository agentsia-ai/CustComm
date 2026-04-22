"""CustComm CLI — the `custcomm` command entry point.

Mirrors LeadGen's CLI shape: a Click group with verb-commands that each
dispatch into an async service call. Rich is used for pretty output —
note `stderr=False` is safe here; this is the human CLI, not the MCP
server.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from custcomm import __version__
from custcomm.config.loader import load_api_keys, load_config
from custcomm.crm.database import ThreadDatabase
from custcomm.models import DraftStatus, ThreadStatus
from custcomm.service import (
    approve_draft,
    draft_replies,
    poll_inbox,
    send_approved,
    triage_threads,
)

console = Console()


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _boot() -> tuple:
    config = load_config()
    keys = load_api_keys()
    db = ThreadDatabase(config.database.sqlite_path)
    await db.init()
    return config, keys, db


@click.group()
@click.version_option(__version__, prog_name="custcomm")
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
def main(debug: bool) -> None:
    """CustComm — AI-powered customer communications engine."""
    _configure_logging(debug)


# ── pipeline ──────────────────────────────────────────────────────────────────


@main.command()
def pipeline() -> None:
    """Show pipeline status (thread counts by status and intent)."""

    async def _run() -> None:
        _, _, db = await _boot()
        status_counts = await db.count_threads_by_status()
        intent_counts = await db.count_threads_by_intent()

        table = Table(title="CustComm Pipeline")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="magenta")
        total = 0
        for status in ThreadStatus:
            c = status_counts.get(status.value, 0)
            if c:
                table.add_row(status.value, str(c))
                total += c
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]")
        console.print(table)

        if intent_counts:
            itable = Table(title="By intent")
            itable.add_column("Intent", style="cyan")
            itable.add_column("Count", justify="right", style="magenta")
            for intent, c in sorted(
                intent_counts.items(), key=lambda x: -x[1]
            ):
                itable.add_row(intent, str(c))
            console.print(itable)

    asyncio.run(_run())


# ── poll ──────────────────────────────────────────────────────────────────────


@main.command()
def poll() -> None:
    """Poll the inbox backend for new messages."""

    async def _run() -> None:
        config, keys, db = await _boot()
        result = await poll_inbox(config, keys, db)
        console.print(
            f"OK Polled inbox "
            f"[green]{result['messages_ingested']}[/green] new messages, "
            f"[green]{result['new_threads']}[/green] new threads, "
            f"[cyan]{result['threads_matched']}[/cyan] matched to existing, "
            f"[dim]{result['messages_skipped_duplicate']}[/dim] duplicates skipped."
        )

    asyncio.run(_run())


# ── triage ────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--thread-id", "thread_ids", multiple=True, help="Triage specific thread(s).")
def triage(thread_ids: tuple[str, ...]) -> None:
    """Classify intent for NEW threads."""

    async def _run() -> None:
        config, keys, db = await _boot()
        counts = await triage_threads(
            config, keys, db, thread_ids=list(thread_ids) or None
        )
        console.print(f"OK Triaged [green]{counts.get('triaged', 0)}[/green] thread(s).")
        for intent_name, c in counts.items():
            if intent_name in {"triaged", "_escalated"}:
                continue
            console.print(f"  - {intent_name}: {c}")
        if counts.get("_escalated"):
            console.print(
                f"  [yellow]auto-escalated: {counts['_escalated']}[/yellow]"
            )

    asyncio.run(_run())


# ── draft ────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--thread-id", "thread_ids", multiple=True, help="Draft for specific thread(s).")
@click.option("--guidance", default="", help="Per-run operator guidance passed to the drafter.")
def draft(thread_ids: tuple[str, ...], guidance: str) -> None:
    """Draft replies for TRIAGED threads with auto-draftable intents."""

    async def _run() -> None:
        config, keys, db = await _boot()
        drafts = await draft_replies(
            config, keys, db, thread_ids=list(thread_ids) or None, guidance=guidance
        )
        if not drafts:
            console.print("[yellow]No drafts generated.[/yellow]")
            return
        table = Table(title=f"Drafted {len(drafts)} reply(ies)")
        table.add_column("Thread", style="cyan")
        table.add_column("Intent", style="yellow")
        table.add_column("Subject", style="white")
        for d in drafts:
            table.add_row(
                d.thread_id[:8],
                d.intent_at_time_of_draft.value,
                d.subject[:60],
            )
        console.print(table)

    asyncio.run(_run())


# ── list ─────────────────────────────────────────────────────────────────────


@main.command(name="list")
@click.option(
    "--status",
    type=click.Choice([s.value for s in ThreadStatus], case_sensitive=False),
    default=None,
)
@click.option("--limit", default=25, type=int)
def list_threads(status: Optional[str], limit: int) -> None:
    """List threads, optionally filtered by status."""

    async def _run() -> None:
        _, _, db = await _boot()
        threads = await db.list_threads(
            status=ThreadStatus(status) if status else None, limit=limit
        )
        if not threads:
            console.print("[yellow]No threads match.[/yellow]")
            return
        table = Table(title=f"Threads ({status or 'all'})")
        table.add_column("ID", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Intent", style="green")
        table.add_column("Subject", style="white")
        table.add_column("Updated", style="dim")
        for t in threads:
            table.add_row(
                t.id[:8],
                t.status.value,
                t.intent.value if t.intent else "-",
                (t.subject or "")[:50],
                t.updated_at.isoformat(timespec="minutes"),
            )
        console.print(table)

    asyncio.run(_run())


# ── show ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("thread_id")
def show(thread_id: str) -> None:
    """Show a thread's full history + pending draft."""

    async def _run() -> None:
        _, _, db = await _boot()
        thread = await _resolve_thread(db, thread_id)
        if not thread:
            console.print(f"[red]No thread matching {thread_id!r}[/red]")
            return
        customer = await db.get_customer(thread.customer_id)
        console.print(f"[bold cyan]{thread.subject}[/bold cyan]")
        console.print(
            f"  id={thread.id}  status={thread.status.value}  "
            f"intent={thread.intent.value if thread.intent else '-'}"
        )
        if customer:
            console.print(f"  customer: {customer.display_name or ''} <{customer.email}>")
        if thread.summary:
            console.print(f"\n[dim]Summary:[/dim] {thread.summary}")

        messages = await db.get_messages(thread.id)
        console.print(f"\n[bold]Messages ({len(messages)})[/bold]")
        for m in messages:
            ts = (m.received_at or m.sent_at)
            ts_str = ts.isoformat(timespec="minutes") if ts else ""
            console.print(
                f"[{m.direction.value}] {ts_str}  "
                f"{m.from_addr}  [dim]{m.subject}[/dim]"
            )
            console.print(f"  {m.body_text.strip()[:300]}")

        draft_row = await db.get_pending_draft_for_thread(thread.id)
        if draft_row:
            console.print("\n[bold green]Pending draft:[/bold green]")
            console.print(f"  Subject: {draft_row.subject}")
            console.print(draft_row.body)

    asyncio.run(_run())


# ── approve ──────────────────────────────────────────────────────────────────


@main.command()
@click.argument("thread_id")
def approve(thread_id: str) -> None:
    """Approve a thread's pending draft."""

    async def _run() -> None:
        _, _, db = await _boot()
        thread = await _resolve_thread(db, thread_id)
        if not thread:
            console.print(f"[red]No thread matching {thread_id!r}[/red]")
            return
        draft = await approve_draft(db, thread.id, approved_by="cli")
        if not draft:
            console.print("[yellow]No pending draft to approve.[/yellow]")
            return
        console.print(
            f"OK Approved draft {draft.id[:8]} on thread {thread.id[:8]}"
        )

    asyncio.run(_run())


# ── send ─────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--thread-id", default=None, help="Send just one thread's approved draft.")
@click.option("--dry-run", is_flag=True, help="Report what would be sent without sending.")
def send(thread_id: Optional[str], dry_run: bool) -> None:
    """Send all APPROVED drafts (or one by --thread-id)."""

    async def _run() -> None:
        config, keys, db = await _boot()
        result = await send_approved(
            config, keys, db, thread_id=thread_id, dry_run=dry_run
        )
        console.print(
            f"{'[DRY-RUN] ' if dry_run else ''}"
            f"Sent: [green]{result['sent']}[/green]  "
            f"daily-limit skipped: {result['skipped_daily_limit']}  "
            f"double-send skipped: {result['skipped_double_send']}  "
            f"errors: [red]{result['errors']}[/red]"
        )

    asyncio.run(_run())


# ── schedule-followups ────────────────────────────────────────────────────────


@main.command(name="schedule-followups")
def schedule_followups_cmd() -> None:
    """Enqueue follow-up drafts for threads waiting on the customer."""

    async def _run() -> None:
        config, _, db = await _boot()
        from custcomm.scheduler.followups import FollowupScheduler

        scheduler = FollowupScheduler(config, db)
        enqueued = await scheduler.enqueue_due_followups()
        console.print(f"OK Enqueued [green]{len(enqueued)}[/green] follow-up(s).")

    asyncio.run(_run())


# ── mcp ───────────────────────────────────────────────────────────────────────


@main.command()
def mcp() -> None:
    """Start the MCP stdio server (for Claude Desktop)."""
    from custcomm.mcp_server.server import main as mcp_main

    asyncio.run(mcp_main())


# ── internal helpers ──────────────────────────────────────────────────────────


async def _resolve_thread(db: ThreadDatabase, prefix_or_id: str):
    """Accept either a full thread UUID or a short prefix (first 8 chars)."""
    thread = await db.get_thread(prefix_or_id)
    if thread:
        return thread
    # Prefix search
    candidates = await db.list_threads(limit=500)
    matches = [t for t in candidates if t.id.startswith(prefix_or_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(
            f"[yellow]Ambiguous prefix {prefix_or_id!r} — {len(matches)} threads match.[/yellow]"
        )
    return None


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)
