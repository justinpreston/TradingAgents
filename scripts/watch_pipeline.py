#!/usr/bin/env python3
"""Live TUI for the weekly pipeline (and matrix runs).

Watches a log file produced by any of:

  • scripts/_kickoff_at_1700.sh   → runs/weekly_kickoff_*.log
  • scripts/run_weekly_all_tiers.py | tee runs/weekly_workflow_*.log
  • scripts/weekly_workflow.py       | tee runs/weekly_workflow_*.log
  • launchd job (com.tradingagents.weekly.plist) → runs/weekly_workflow.log
  • run_copilot_matrix.py | tee runs/matrix_<id>_*.log

…and renders a real-time dashboard showing:

  • Pre-launch countdown (if the kickoff wrapper is still waiting)
  • Tier progress (mid → large → mega) for run_weekly_all_tiers.py
  • Phase progress within each tier (Phase 1-5 of weekly_workflow.py)
  • Screener stage progress bar (universe / scoring) with throughput + ETA
  • Matrix progress (Stage A → Stage B → auto-report cascade)
  • Tail of the most recent log lines
  • Error / drop / rate-limit counters

Usage:
  # Watch newest log (default):
  .venv/bin/python scripts/watch_pipeline.py

  # Watch a specific log:
  .venv/bin/python scripts/watch_pipeline.py runs/weekly_kickoff_*.log

  # Launch a run AND watch it (one command, fresh log):
  .venv/bin/python scripts/watch_pipeline.py --run weekly
  .venv/bin/python scripts/watch_pipeline.py --run kickoff
  .venv/bin/python scripts/watch_pipeline.py --run matrix --tickers VIRT,DAR

Press Ctrl+C to exit. The pipeline / matrix run keeps running in the
background regardless of whether the TUI is attached.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
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
# Match either the legacy hr-rule banner from weekly_workflow.py
# (── Tier · MID ──) or the boxed banner from run_weekly_all_tiers.py
# (│ Tier · MID │).
RE_TIER = re.compile(r"(?:──|│)\s*Tier\s+·\s+(\w+)\s")
RE_PHASE = re.compile(r"──\s*Phase\s+([\d.]+)\s+·\s+([A-Z][A-Z0-9 _]*)")
RE_SCREENER_CONFIG = re.compile(r"\[screener\]\s+config:\s+top=(\d+).*mcap=\$([\d.]+)B-\$([\d.]+)B")
# `[screener] writing to runs/screener_mid_2026-05-08_1636` — captures the
# tier from the output dir name. This is our fallback when the Phase banner
# gets stuck in the inner subprocess's buffered stdout.
RE_SCREENER_WRITING = re.compile(r"\[screener\]\s+writing\s+to\s+\S*screener_(\w+?)_\d")
RE_SCREENER_STAGE = re.compile(r"\[(universe|scoring)\]\s+(\d+)\s+tickers")
RE_SCREENER_PROGRESS = re.compile(r"\s*(universe|scoring)\s+(\d+)/(\d+)\s+\((\d+)%\)\s+—\s+last:\s+(\S+)")
RE_SCREENER_DONE = re.compile(r"\[screener\]\s+done in ([\d.]+)m\s+—\s+top\s+(\d+)")
RE_INDEX_DONE = re.compile(r"Indexed:.*\b(\d+)\s+screener\b")
RE_DROP = re.compile(r"(?:dropped|drop)", re.IGNORECASE)
RE_RATE_LIMIT = re.compile(r"rate.?limit|429", re.IGNORECASE)
RE_ERROR = re.compile(r"\b(?:error|❌|failed|exception)\b", re.IGNORECASE)

# Matrix-run patterns (run_copilot_matrix.py)
RE_MATRIX_STAGE = re.compile(
    r"┌─\s+Stage\s+(\w+)\s+·\s+profile=(\w+)\s+·\s+(\d+)\s+tickers\s+·\s+parallel=(\d+)"
)
RE_MATRIX_CELL = re.compile(
    r"^\│\s+([🟢🔴·])\s+(\S+)\s+(\w+)\s+→\s+([\w/]+)\s+\(([0-9.]+)s(?:\s+PT=([\d.]+))?\)"
)
RE_MATRIX_AUTOREPORT_STEP = re.compile(r"^\s+→\s+([\w\-\(\) ]+?)(?=…|\s+→|$)")
RE_MATRIX_STOP = re.compile(r"stop-on-overweight=(\d+)\s+reached")


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
class MatrixState:
    active: bool = False
    stage: str = ""           # "A" or "B"
    profile: str = ""
    total: int = 0
    done: int = 0
    parallel: int = 0
    last_ticker: str = ""
    last_rating: str = ""
    last_marker: str = ""
    promoted: int = 0          # 🟢 count
    vetoed: int = 0            # 🔴 count
    started_at: datetime | None = None
    autoreport_step: str = ""  # current cascade step (accounting / options / chronos / index)
    autoreport_history: list[str] = field(default_factory=list)


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
    matrix: MatrixState = field(default_factory=MatrixState)

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

    # Pre-launch countdown — compute target time from the LINE's HH:MM:SS,
    # not from the current wall clock. Otherwise an old waiting line in a
    # stale log produces a phantom future target when the TUI replays it.
    if m := RE_WAIT.search(line):
        line_hhmmss = m.group(1)
        seconds_left = int(m.group(2))
        try:
            h, m_, s = (int(x) for x in line_hhmmss.split(":"))
            line_dt = now.replace(hour=h, minute=m_, second=s, microsecond=0)
            target_dt = line_dt + timedelta(seconds=seconds_left)
            # If the parsed target is in the past, the log is stale — don't
            # overwrite a fresher countdown_until with junk.
            if target_dt > now:
                state.countdown_until = target_dt
        except ValueError:
            pass
        return

    if RE_KICKOFF.search(line):
        state.pipeline_started_at = now
        state.countdown_until = None
        return

    # Tier transitions (banner from weekly_workflow.py or run_weekly_all_tiers.py)
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

    # Fallback when Phase/Tier banners get stuck in the inner subprocess's
    # buffered stdout: derive tier from the screener output path and assume
    # Phase 1 SCREEN is active.
    if m := RE_SCREENER_WRITING.search(line):
        tier_name = m.group(1).lower()
        if state.current_tier != tier_name:
            if state.current_tier:
                prev = state.tier(state.current_tier)
                if prev.status == "running":
                    prev.status = "done"
                    prev.ended_at = now
            state.current_tier = tier_name
            t = state.tier(tier_name)
            t.status = "running"
            t.started_at = t.started_at or now
        # Set phase if not already set by an actual banner
        t = state.tier(state.current_tier)
        if not t.current_phase:
            t.current_phase = "Phase 1 · Screen"
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
            # Implicit phase if no banner has fired
            if not t.current_phase:
                t.current_phase = "Phase 1 · Screen"
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

    # Matrix runs: stage banner
    if m := RE_MATRIX_STAGE.search(line):
        state.matrix.active = True
        state.matrix.stage = m.group(1)
        state.matrix.profile = m.group(2)
        state.matrix.total = int(m.group(3))
        state.matrix.parallel = int(m.group(4))
        state.matrix.done = 0
        state.matrix.last_ticker = ""
        state.matrix.last_rating = ""
        state.matrix.last_marker = ""
        state.matrix.autoreport_step = ""
        if state.matrix.started_at is None:
            state.matrix.started_at = now
            state.pipeline_started_at = state.pipeline_started_at or now
        return

    # Matrix runs: per-cell completion
    if state.matrix.active and (m := RE_MATRIX_CELL.search(line)):
        state.matrix.done += 1
        state.matrix.last_marker = m.group(1)
        state.matrix.last_ticker = m.group(2)
        state.matrix.last_rating = m.group(4)
        if m.group(1) == "🟢":
            state.matrix.promoted += 1
        elif m.group(1) == "🔴":
            state.matrix.vetoed += 1
        return

    # Matrix runs: auto-report cascade step (accounting / options-overlay / chronos / index-runs)
    if state.matrix.active and (m := RE_MATRIX_AUTOREPORT_STEP.search(line)):
        step = m.group(1).strip().rstrip("…").strip()
        # Mark previous step done
        if state.matrix.autoreport_step and state.matrix.autoreport_step not in state.matrix.autoreport_history:
            state.matrix.autoreport_history.append(state.matrix.autoreport_step)
        state.matrix.autoreport_step = step
        return

    if RE_MATRIX_STOP.search(line):
        # stop-on-overweight short-circuit; treat remaining cells as cancelled
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
        target_hhmm = state.countdown_until.strftime("%H:%M")
        title = f"[bold yellow]⏳ Waiting for {target_hhmm} kickoff[/]"
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
        title = "[bold green]✅ Run complete[/]"
        body = Text(f"Total runtime: {fmt_dur(elapsed)}", style="green")
    elif state.matrix.active:
        elapsed = now - (state.matrix.started_at or state.pipeline_started_at or state.started_at)
        title = "[bold magenta]🔬 Matrix run · live[/]"
        body = Text.assemble(
            ("Stage: ", "dim"),
            (f"{state.matrix.stage} · {state.matrix.profile}", "bold magenta"),
            ("   |   ", "dim"),
            ("Elapsed: ", "dim"),
            (fmt_dur(elapsed), "bold cyan"),
            ("   |   ", "dim"),
            ("Log: ", "dim"),
            (str(state.log_path.name), "blue"),
        )
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
        title = "[bold]TradingAgents · Live[/]"
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


def render_matrix(state: WatchState) -> Panel:
    m = state.matrix
    if not m.active:
        body = Text("No matrix run active", style="dim")
        return Panel(body, title="[bold]Matrix Run[/]", border_style="dim")

    pct = (m.done / m.total * 100) if m.total else 0
    bar_width = 40
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    header = Text.assemble(
        ("Stage ", "dim"),
        (m.stage, "bold magenta"),
        ("   |   ", "dim"),
        ("profile=", "dim"),
        (m.profile, "bold cyan"),
        ("   |   ", "dim"),
        ("parallel=", "dim"),
        (str(m.parallel), "white"),
    )
    progress = Text.assemble(
        (bar, "magenta"),
        ("  ", ""),
        (f"{m.done}/{m.total}", "white"),
        (f" ({pct:.0f}%)", "dim"),
    )
    last = Text.assemble(
        ("Last: ", "dim"),
        (f"{m.last_marker} " if m.last_marker else "", ""),
        (m.last_ticker or "—", "yellow"),
        (f" → {m.last_rating}" if m.last_rating else "", "white"),
    )
    counts = Text.assemble(
        ("🟢 promoted: ", "dim"),
        (str(m.promoted), "bold green"),
        ("   |   ", "dim"),
        ("🔴 vetoed: ", "dim"),
        (str(m.vetoed), "bold red"),
    )
    cascade_lines = []
    if m.autoreport_step or m.autoreport_history:
        cascade_lines.append(Text(""))
        cascade_lines.append(Text("Auto-report cascade:", style="dim"))
        for step in m.autoreport_history:
            cascade_lines.append(Text(f"  ✓ {step}", style="green"))
        if m.autoreport_step and m.autoreport_step not in m.autoreport_history:
            cascade_lines.append(Text(f"  ⏳ {m.autoreport_step}…", style="bold yellow"))

    return Panel(
        Group(header, Text(""), progress, Text(""), last, counts, *cascade_lines),
        title="[bold]Matrix Run[/]",
        border_style="magenta",
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
    if state.matrix.active:
        layout.split_column(
            Layout(render_header(state), name="header", size=3),
            Layout(name="middle", size=14),
            Layout(name="bottom"),
        )
        layout["middle"].split_row(
            Layout(render_matrix(state), name="matrix", ratio=2),
            Layout(render_stats(state), name="stats", ratio=1),
        )
    else:
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
                    if (
                        "Total elapsed" in last5
                        or "Next steps" in last5
                        or ("✅ HTML:" in last5 and state.matrix.active)
                    ):
                        if not state.completed_at:
                            state.completed_at = datetime.now()
                            for t in state.tiers.values():
                                if t.status == "running":
                                    t.status = "done"
                                    t.ended_at = state.completed_at
                            if state.matrix.active and state.matrix.autoreport_step:
                                if state.matrix.autoreport_step not in state.matrix.autoreport_history:
                                    state.matrix.autoreport_history.append(state.matrix.autoreport_step)
                                state.matrix.autoreport_step = ""

                live.update(build_layout(state))
                time.sleep(1.0 / refresh_hz)
            except KeyboardInterrupt:
                break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def find_latest_log() -> Path | None:
    """Pick the most recently modified pipeline-related log under runs/.

    Glob across all known patterns then sort by mtime (descending) — ensures
    a fresh weekly_workflow log wins over a stale kickoff log even though
    kickoff logs sort earlier alphabetically.
    """
    patterns = ("weekly_kickoff_*.log", "weekly_workflow*.log", "matrix_*.log")
    candidates: list[Path] = []
    for pat in patterns:
        candidates.extend(RUNS_DIR.glob(pat))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Run launcher (--run): spawn pipeline/matrix as a detached subprocess and
# tee its output to a fresh log file, then watch that log.
# ---------------------------------------------------------------------------
RUN_PRESETS = {
    # name: (cmd, log_basename_template)
    "weekly":  (["{py}", "scripts/run_weekly_all_tiers.py", "--top", "25"],
                "weekly_workflow_{ts}.log"),
    "screen":  (["{py}", "scripts/weekly_workflow.py", "--top", "25"],
                "weekly_workflow_{ts}.log"),
    "kickoff": (["bash", "scripts/_kickoff_at.sh"],
                "weekly_kickoff_{ts}.log"),
}


def launch_run(preset: str, tickers: list[str] | None = None) -> Path:
    """Spawn a pipeline/matrix command in a new session, redirect both
    stdout and stderr to a fresh log file under runs/, and return the
    path. The child survives if this TUI exits.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    py = sys.executable

    if preset == "matrix":
        if not tickers:
            raise SystemExit("--run matrix requires --tickers TICKER[,TICKER...]")
        joined = ",".join(t.strip().upper() for t in tickers if t.strip())
        cmd_tmpl: list[str] = [
            py, "run_copilot_matrix.py", "--tickers", *joined.split(","),
            "--max-parallel", "2", "--stop-on-overweight", "0",
        ]
        log_name = f"matrix_{joined.replace(',', '_').lower()}_{ts}.log"
        cmd = cmd_tmpl
    elif preset in RUN_PRESETS:
        raw_cmd, log_template = RUN_PRESETS[preset]
        cmd = [seg.format(py=py) for seg in raw_cmd]
        log_name = log_template.format(ts=ts)
    else:
        raise SystemExit(f"unknown --run preset: {preset!r} (try: weekly, screen, kickoff, matrix)")

    log_path = RUNS_DIR / log_name
    RUNS_DIR.mkdir(exist_ok=True)
    log_fh = open(log_path, "w", buffering=1)  # line-buffered

    subprocess.Popen(  # noqa: S603 - cmd is constructed from presets
        cmd,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ},
    )
    log_fh.close()  # the child keeps its own fd; we hold a path
    return log_path


def main() -> int:
    p = argparse.ArgumentParser(
        description="Live TUI for the weekly pipeline and matrix runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  watch_pipeline.py                              # auto-watch newest log\n"
            "  watch_pipeline.py runs/weekly_workflow.log     # explicit log\n"
            "  watch_pipeline.py --run weekly                 # launch + watch\n"
            "  watch_pipeline.py --run kickoff                # sleep til 17:00, then run\n"
            "  watch_pipeline.py --run matrix --tickers VIRT  # single-ticker matrix\n"
        ),
    )
    p.add_argument("log", nargs="?", help="Path to a kickoff/pipeline/matrix log file")
    p.add_argument(
        "--run",
        choices=["weekly", "screen", "kickoff", "matrix"],
        help=(
            "Launch a fresh run and watch its log. 'weekly' = run_weekly_all_tiers.py, "
            "'screen' = weekly_workflow.py, 'kickoff' = _kickoff_at_1700.sh, "
            "'matrix' = run_copilot_matrix.py (requires --tickers)."
        ),
    )
    p.add_argument(
        "--tickers",
        help="Comma-separated tickers for --run matrix (e.g. VIRT,DAR).",
    )
    p.add_argument("--auto", action="store_true",
                   help="(default) Pick the newest kickoff/weekly/matrix log under runs/.")
    args = p.parse_args()

    if args.run:
        tickers = args.tickers.split(",") if args.tickers else None
        log_path = launch_run(args.run, tickers)
        print(f"[launched] {args.run} → log: {log_path}", file=sys.stderr)
        # Brief settle time so the child writes its first line(s)
        time.sleep(1.0)
    elif args.log:
        log_path = Path(args.log).resolve()
    else:
        latest = find_latest_log()
        if latest is None:
            print(
                "No kickoff/weekly/matrix logs found in runs/. "
                "Pass a path or use --run weekly|kickoff|matrix.",
                file=sys.stderr,
            )
            return 1
        log_path = latest

    if not log_path.exists():
        # If we just launched, the child may not have created the file yet;
        # spin briefly waiting for it.
        for _ in range(50):
            if log_path.exists():
                break
            time.sleep(0.1)
        if not log_path.exists():
            print(f"Log file not found: {log_path}", file=sys.stderr)
            return 1

    console = Console()
    state = WatchState(log_path=log_path)
    console.print(
        f"[dim]Watching {log_path}[/] [dim](Ctrl+C to detach; the run keeps going)[/]"
    )
    time.sleep(0.6)
    try:
        tail_log(log_path, state, console)
    except KeyboardInterrupt:
        console.print("\n[dim]Detached. Run still going in the background.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
