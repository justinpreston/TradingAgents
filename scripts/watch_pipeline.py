#!/usr/bin/env python3
"""Live TUI for the weekly pipeline.

Watches a log file (kickoff wrapper or pipeline) and renders a real-time
dashboard showing:

  • Pre-launch countdown (if the kickoff wrapper is still waiting)
  • Tier progress (mid → large → mega) for run_weekly_all_tiers.py
  • Phase progress within each tier (Phase 1-5 of weekly_workflow.py)
  • Screener stage progress bar (universe / scoring) with throughput + ETA
  • Tail of the most recent log lines
  • Error / drop / rate-limit counters

Usage:
  .venv/bin/python scripts/watch_pipeline.py runs/weekly_kickoff_*.log
  .venv/bin/python scripts/watch_pipeline.py --auto      # newest kickoff/weekly log

Press Ctrl+C to exit (the pipeline keeps running in the background).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"


# ---------------------------------------------------------------------------
# Log line patterns (anchored to actual stdout from run_screener.py,
# weekly_workflow.py, run_weekly_all_tiers.py).
# ---------------------------------------------------------------------------
RE_WAIT = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]\s+waiting\.\.\.\s+(\d+)s remaining")
RE_KICKOFF = re.compile(r"🚀 LAUNCHING ")
RE_TIER = re.compile(r"──\s*Tier\s+·\s+(\w+)\s+──")
RE_PHASE = re.compile(r"──\s*Phase\s+([\d.]+)\s+·\s+([A-Z][A-Z0-9 _]*)")
RE_SCREENER_CONFIG = re.compile(r"\[screener\]\s+config:\s+top=(\d+).*mcap=\$([\d.]+)B-\$([\d.]+)B")
RE_SCREENER_STAGE = re.compile(r"\[(universe|scoring)\]\s+(\d+)\s+tickers")
RE_SCREENER_PROGRESS = re.compile(r"\s*(universe|scoring)\s+(\d+)/(\d+)\s+\((\d+)%\)\s+—\s+last:\s+(\S+)")
RE_SCREENER_DONE = re.compile(r"\[screener\]\s+done in ([\d.]+)m\s+—\s+top\s+(\d+)")
RE_INDEX_DONE = re.compile(r"Indexed:.*\b(\d+)\s+screener\b")
RE_DROP = re.compile(r"(?:dropped|drop)", re.IGNORECASE)
RE_RATE_LIMIT = re.compile(r"rate.?limit|429", re.IGNORECASE)
RE_ERROR = re.compile(r"\b(?:error|❌|failed|exception)\b", re.IGNORECASE)


@dataclass
class TierState:
    name: str
    status: str = "pending"  # pending | running | done | failed
    started_at: datetime | None = None
    ended_at: datetime | None = None
    current_phase: str = ""
    screener_stage: str = ""
    screener_done: int = 0
    screener_total: int = 0
    screener_last_ticker: str = ""
    screener_eta_s: float | None = None
    screener_rate_per_s: float | None = None
    last_progress_at: datetime | None = None
    last_progress_count: int = 0


@dataclass
class WatchState:
    log_path: Path
    started_at: datetime = field(default_factory=datetime.now)
    countdown_until: datetime | None = None
    pipeline_started_at: datetime | None = None
    tiers: dict[str, TierState] = field(default_factory=dict)
    current_tier: str | None = None
    drops: int = 0
    rate_limit_hits: int = 0
    errors: int = 0
    recent_lines: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    completed_at: datetime | None = None

    def tier(self, name: str) -> TierState:
        n = name.lower()
        if n not in self.tiers:
            self.tiers[n] = TierState(name=n)
        return self.tiers[n]


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------
def process_line(line: str, state: WatchState) -> None:
    line = line.rstrip("\n")
    if not line.strip():
        return
    state.recent_lines.append(line)
    now = datetime.now()

    # Pre-launch countdown
    if m := RE_WAIT.search(line):
        seconds_left = int(m.group(2))
        state.countdown_until = now + timedelta(seconds=seconds_left)
        return

    if RE_KICKOFF.search(line):
        state.pipeline_started_at = now
        state.countdown_until = None
        return

    # Tier transitions
    if m := RE_TIER.search(line):
        tier_name = m.group(1).lower()
        # Mark previous tier done
        if state.current_tier and state.current_tier != tier_name:
            prev = state.tier(state.current_tier)
            if prev.status == "running":
                prev.status = "done"
                prev.ended_at = now
        state.current_tier = tier_name
        t = state.tier(tier_name)
        t.status = "running"
        t.started_at = now
        return

    if m := RE_PHASE.search(line):
        phase_num = m.group(1)
        phase_name = m.group(2).strip().title()
        if state.current_tier:
            state.tier(state.current_tier).current_phase = f"Phase {phase_num} · {phase_name}"
        return

    # Screener stage announcement: "[scoring] 250 tickers"
    if m := RE_SCREENER_STAGE.search(line):
        if state.current_tier:
            t = state.tier(state.current_tier)
            t.screener_stage = m.group(1)
            t.screener_total = int(m.group(2))
            t.screener_done = 0
            t.screener_last_ticker = ""
            t.last_progress_at = now
            t.last_progress_count = 0
        return

    # Screener progress: "  scoring 127/250 (51%) — last: NVDA"
    if m := RE_SCREENER_PROGRESS.search(line):
        if state.current_tier:
            t = state.tier(state.current_tier)
            t.screener_stage = m.group(1)
            t.screener_done = int(m.group(2))
            t.screener_total = int(m.group(3))
            t.screener_last_ticker = m.group(5)
            # Compute rate based on time since last progress emission
            if t.last_progress_at:
                dt = (now - t.last_progress_at).total_seconds()
                dn = t.screener_done - t.last_progress_count
                if dt > 0 and dn > 0:
                    t.screener_rate_per_s = dn / dt
                    remaining = t.screener_total - t.screener_done
                    t.screener_eta_s = remaining / t.screener_rate_per_s if t.screener_rate_per_s > 0 else None
            t.last_progress_at = now
            t.last_progress_count = t.screener_done
        return

    if RE_SCREENER_DONE.search(line):
        if state.current_tier:
            state.tier(state.current_tier).screener_stage = "done"
        return

    # Counters
    if RE_RATE_LIMIT.search(line):
        state.rate_limit_hits += 1
    elif RE_DROP.search(line):
        state.drops += 1
    if RE_ERROR.search(line) and "0 drops" not in line and "0 errors" not in line:
        state.errors += 1


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
TIER_ORDER = ["mid", "large", "mega"]


def fmt_dur(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 60:
        return f"{s}s"
    return f"{s // 60}:{s % 60:02d}"


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"~{s // 60}:{s % 60:02d}"


def render_header(state: WatchState) -> Panel:
    now = datetime.now()
    if state.countdown_until and state.countdown_until > now:
        remaining = state.countdown_until - now
        title = "[bold yellow]⏳ Waiting for 17:00 kickoff[/]"
        body = Text.assemble(
            ("Time now: ", "dim"),
            (now.strftime("%H:%M:%S"), "white"),
            ("   |   ", "dim"),
            ("Launches in: ", "dim"),
            (fmt_dur(remaining), "bold yellow"),
            ("   |   ", "dim"),
            ("Target: ", "dim"),
            (state.countdown_until.strftime("%H:%M:%S"), "bold green"),
        )
    elif state.completed_at:
        elapsed = state.completed_at - (state.pipeline_started_at or state.started_at)
        title = "[bold green]✅ Pipeline complete[/]"
        body = Text(f"Total runtime: {fmt_dur(elapsed)}", style="green")
    elif state.pipeline_started_at:
        elapsed = now - state.pipeline_started_at
        title = "[bold green]🚀 Pipeline running[/]"
        body = Text.assemble(
            ("Started: ", "dim"),
            (state.pipeline_started_at.strftime("%H:%M:%S"), "white"),
            ("   |   ", "dim"),
            ("Elapsed: ", "dim"),
            (fmt_dur(elapsed), "bold cyan"),
            ("   |   ", "dim"),
            ("Log: ", "dim"),
            (str(state.log_path.relative_to(REPO_ROOT) if state.log_path.is_absolute() else state.log_path), "blue"),
        )
    else:
        title = "[bold]TradingAgents Weekly Pipeline · Live[/]"
        body = Text("waiting for first log line…", style="dim")
    return Panel(body, title=title, border_style="cyan")


def render_tiers(state: WatchState) -> Panel:
    table = Table(show_header=True, header_style="bold magenta", expand=True, padding=(0, 1))
    table.add_column("Tier", justify="left", width=8)
    table.add_column("Status", justify="left", width=12)
    table.add_column("Phase", justify="left")
    table.add_column("Elapsed", justify="right", width=10)
    for name in TIER_ORDER:
        t = state.tiers.get(name) or TierState(name=name)
        if t.status == "pending":
            status = Text("⏸ pending", style="dim")
        elif t.status == "running":
            status = Text("🟢 running", style="bold green")
        elif t.status == "done":
            status = Text("✅ done", style="green")
        else:
            status = Text(f"❌ {t.status}", style="red")
        if t.started_at:
            end = t.ended_at or datetime.now()
            elapsed = fmt_dur(end - t.started_at)
        else:
            elapsed = "—"
        table.add_row(
            Text(name.upper(), style="bold cyan" if t.status == "running" else "white"),
            status,
            t.current_phase or "—",
            elapsed,
        )
    return Panel(table, title="[bold]Tiers[/]", border_style="magenta")


def render_screener(state: WatchState) -> Panel:
    cur = state.tiers.get(state.current_tier) if state.current_tier else None
    if not cur or not cur.screener_stage or cur.screener_stage == "done":
        body = Text("No screener stage active", style="dim")
        return Panel(body, title="[bold]Screener Progress[/]", border_style="dim")

    pct = (cur.screener_done / cur.screener_total * 100) if cur.screener_total else 0
    bar_width = 40
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    header = Text.assemble(
        ("Stage: ", "dim"),
        (cur.screener_stage, "bold yellow"),
        ("   |   ", "dim"),
        ("Tier: ", "dim"),
        ((state.current_tier or "?").upper(), "bold cyan"),
    )
    progress = Text.assemble(
        (bar, "green"),
        ("  ", ""),
        (f"{cur.screener_done}/{cur.screener_total}", "white"),
        (f" ({pct:.0f}%)", "dim"),
    )
    rate_str = f"{cur.screener_rate_per_s:.1f} tk/s" if cur.screener_rate_per_s else "—"
    footer = Text.assemble(
        ("Last ticker: ", "dim"),
        (cur.screener_last_ticker or "—", "yellow"),
        ("   |   ", "dim"),
        ("Rate: ", "dim"),
        (rate_str, "white"),
        ("   |   ", "dim"),
        ("ETA: ", "dim"),
        (fmt_eta(cur.screener_eta_s), "white"),
    )
    return Panel(
        Group(header, Text(""), progress, Text(""), footer),
        title="[bold]Screener Progress[/]",
        border_style="green",
    )


def render_log(state: WatchState) -> Panel:
    if not state.recent_lines:
        body: Text | Group = Text("waiting for log output…", style="dim")
    else:
        items = []
        for ln in list(state.recent_lines)[-10:]:
            stripped = ln.strip()
            style = "white"
            if stripped.startswith("──") or stripped.startswith("╭") or stripped.startswith("╰"):
                style = "magenta"
            elif "[screener]" in stripped:
                style = "cyan"
            elif "[universe]" in stripped or "[scoring]" in stripped:
                style = "yellow"
            elif "❌" in stripped or "error" in stripped.lower() or "failed" in stripped.lower():
                style = "red"
            elif "⚠" in stripped:
                style = "yellow"
            elif "✅" in stripped or "done" in stripped.lower():
                style = "green"
            items.append(Text(ln[:200], style=style))
        body = Group(*items)
    return Panel(body, title="[bold]Recent Output[/]", border_style="blue")


def render_stats(state: WatchState) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left", style="dim")
    table.add_column(justify="right")
    table.add_row("Tiers done", str(sum(1 for t in state.tiers.values() if t.status == "done")))
    table.add_row("Drops", Text(str(state.drops), style="yellow" if state.drops else "dim"))
    table.add_row("Rate-limit hits", Text(str(state.rate_limit_hits), style="yellow" if state.rate_limit_hits else "dim"))
    table.add_row("Errors", Text(str(state.errors), style="red bold" if state.errors else "dim"))
    return Panel(table, title="[bold]Stats[/]", border_style="white")


def build_layout(state: WatchState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(render_header(state), name="header", size=3),
        Layout(name="middle", size=9),
        Layout(render_screener(state), name="screener", size=8),
        Layout(name="bottom"),
    )
    layout["middle"].split_row(
        Layout(render_tiers(state), name="tiers", ratio=2),
        Layout(render_stats(state), name="stats", ratio=1),
    )
    layout["bottom"].update(render_log(state))
    return layout


# ---------------------------------------------------------------------------
# Tail loop
# ---------------------------------------------------------------------------
def tail_log(path: Path, state: WatchState, console: Console) -> None:
    """Follow ``path`` and process any new content forever."""
    pos = 0
    last_size = -1
    refresh_hz = 4

    with Live(build_layout(state), console=console, refresh_per_second=refresh_hz, screen=True) as live:
        while True:
            try:
                if path.exists():
                    size = path.stat().st_size
                    if size > pos:
                        with path.open("r", errors="replace") as f:
                            f.seek(pos)
                            for line in f:
                                process_line(line, state)
                        pos = size
                    last_size = size

                # Detect pipeline completion (stdout includes the next-step block)
                if state.pipeline_started_at and state.recent_lines:
                    last5 = "\n".join(list(state.recent_lines)[-5:])
                    if "Total elapsed" in last5 or "Next steps" in last5:
                        if not state.completed_at:
                            state.completed_at = datetime.now()
                            for t in state.tiers.values():
                                if t.status == "running":
                                    t.status = "done"
                                    t.ended_at = state.completed_at

                live.update(build_layout(state))
                time.sleep(1.0 / refresh_hz)
            except KeyboardInterrupt:
                break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def find_latest_log() -> Path | None:
    candidates = sorted(RUNS_DIR.glob("weekly_kickoff_*.log"), reverse=True)
    if candidates:
        return candidates[0]
    candidates = sorted(RUNS_DIR.glob("weekly_workflow*.log"), reverse=True)
    if candidates:
        return candidates[0]
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Live TUI for the weekly pipeline.")
    p.add_argument("log", nargs="?", help="Path to a kickoff or pipeline log file")
    p.add_argument("--auto", action="store_true",
                   help="Automatically pick the most recent kickoff/weekly log")
    args = p.parse_args()

    if args.log:
        log_path = Path(args.log).resolve()
    elif args.auto or True:  # default to auto if no positional given
        latest = find_latest_log()
        if latest is None:
            print("No kickoff/weekly logs found in runs/. Pass a path explicitly.", file=sys.stderr)
            return 1
        log_path = latest
    else:
        print("usage: watch_pipeline.py [LOG] [--auto]", file=sys.stderr)
        return 1

    if not log_path.exists():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1

    console = Console()
    state = WatchState(log_path=log_path)
    console.print(f"[dim]Watching {log_path}[/] [dim](Ctrl+C to exit; pipeline keeps running)[/]")
    time.sleep(0.6)
    try:
        tail_log(log_path, state, console)
    except KeyboardInterrupt:
        console.print("\n[dim]Detached. Pipeline still running in the background.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
