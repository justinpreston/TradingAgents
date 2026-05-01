"""Multi-ticker terminal dashboard built on :class:`rich.live.Live`.

Layout::

    ┌─ Persona-aligned batch · 2024-05-10 ────────────────────────┐
    │  Ticker  Phase     Current node          Elapsed   LLM  Tool│
    │   ✓ NVDA Decision  Portfolio Manager       3:24    27   12  │
    │   ● AAPL Risk      Conservative Analyst    1:47    19    8  │
    │   ⏳ MSFT  —         pending                  —       0    0  │
    │   ⏳ GOOGL —         pending                  —       0    0  │
    └──────────────────────────────────────────────────────────────┘
    AAPL  ▰▰▰▰▰▰▰▰▱▱▱▱  10/12 · Conservative Analyst   tokens 18.4k↑/3.1k↓
    ┌─ Recent ────────────────────────────────────────────────────┐
    │ 14:02:11 NVDA · Portfolio Manager → done · BUY               │
    │ 14:02:30 AAPL · Bull Researcher → done                       │
    │ 14:02:42 AAPL · uses tool: get_news                          │
    │ ...                                                          │
    └──────────────────────────────────────────────────────────────┘

Key design points
-----------------

* The Rich :class:`~rich.console.Console` is bound to the **original**
  ``stderr`` so the dashboard keeps painting even if the run wraps stdout
  with a tee. This is critical: in the runner, the per-ticker log file is
  installed as ``sys.stdout`` — if we let the dashboard write to that we'd
  see the live region inside the log file instead of the terminal.

* :meth:`__enter__` / :meth:`__exit__` own the :class:`rich.live.Live`
  lifecycle so callers can ``with dashboard: …``.

* Only one ticker is "active" at a time (this matches the sequential run
  loop). When :meth:`on_run_start` fires for a new ticker, the previous
  one's row is left on screen (status ✓ / ✗) and the progress bar follows
  the new ticker.

* All listener methods grab a single :class:`threading.Lock` so concurrent
  callback fires (rare, but possible) don't corrupt the row map.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from .node_metadata import (
    DECISION_NODES,
    NODE_DISPLAY_ORDER,
    is_hidden_node,
    is_visible_node,
    phase_for_node,
    phase_label,
)
from .progress_listener import ProgressListener


_STATUS_PENDING = "pending"
_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_ERROR = "error"

_STATUS_GLYPH = {
    _STATUS_PENDING: ("⏳", "dim"),
    _STATUS_RUNNING: ("●", "bold cyan"),
    _STATUS_DONE: ("✓", "bold green"),
    _STATUS_ERROR: ("✗", "bold red"),
}


def _format_elapsed(seconds: float) -> str:
    if seconds < 1:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 60:d}:{seconds % 60:02d}"


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


@dataclass
class _TickerState:
    ticker: str
    status: str = _STATUS_PENDING
    phase: str = ""
    current_node: str = "pending"
    nodes_completed: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    signal: Optional[str] = None
    error: Optional[str] = None
    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    completed_visible_nodes: List[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


class RunDashboard:
    """Rich-driven multi-ticker progress UI.

    Implements :class:`ProgressListener`. Use as a context manager so the
    underlying :class:`rich.live.Live` is started and torn down cleanly
    even on Ctrl-C / exception.
    """

    EVENT_TAIL_LEN = 8

    def __init__(
        self,
        tickers: List[str],
        trade_date: str,
        run_id: str,
        title: str = "TradingAgents",
        *,
        system_date: Optional[str] = None,
        console: Optional[Console] = None,
        refresh_per_second: int = 4,
    ) -> None:
        self._tickers = list(tickers)
        self._trade_date = trade_date
        self._system_date = system_date
        self._run_id = run_id
        self._title = title

        # Bind the console to the *real* stderr fd so the dashboard isn't
        # captured by stdout redirection inside the run loop.
        self._console = console or Console(file=sys.stderr, force_terminal=True)

        self._lock = threading.Lock()
        self._states: Dict[str, _TickerState] = {
            t: _TickerState(ticker=t) for t in self._tickers
        }
        self._active: Optional[str] = None
        self._events: Deque[Tuple[str, str]] = deque(maxlen=self.EVENT_TAIL_LEN)
        self._batch_started_at: Optional[float] = None

        # Single Rich progress bar tracking the currently-active ticker.
        self._progress = Progress(
            TextColumn("[bold]{task.fields[ticker]}[/]"),
            BarColumn(bar_width=24),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("· [dim]{task.fields[node]}[/]"),
            TextColumn(
                "· tokens [cyan]{task.fields[tin]}[/]↑/"
                "[magenta]{task.fields[tout]}[/]↓"
            ),
            TimeElapsedColumn(),
            console=self._console,
            transient=False,
        )
        self._progress_task: Optional[TaskID] = None

        self._live: Optional[Live] = None

    # ---- context manager --------------------------------------------------

    def __enter__(self) -> "RunDashboard":
        self._batch_started_at = time.time()
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Final paint so the closed state is visible.
        try:
            if self._live is not None:
                self._live.update(self._render(final=True), refresh=True)
                self._live.stop()
        finally:
            self._live = None
        # Print the closing summary as plain rows below where Live was.
        self._console.print(self._summary_table())

    # ---- ProgressListener -------------------------------------------------

    def on_run_start(self, ticker: str, trade_date: str) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))
            state.status = _STATUS_RUNNING
            state.started_at = time.time()
            state.current_node = "starting…"
            state.phase = ""
            self._active = ticker

            # (Re)bind the Rich progress bar to this ticker.
            if self._progress_task is not None:
                try:
                    self._progress.remove_task(self._progress_task)
                except Exception:
                    pass
            self._progress_task = self._progress.add_task(
                "run",
                total=len(NODE_DISPLAY_ORDER),
                completed=0,
                ticker=ticker,
                node="starting…",
                tin="0",
                tout="0",
            )

            self._push_event(ticker, "▶ starting")
        self._refresh()

    def on_node_complete(
        self,
        ticker: str,
        node_name: str,
        delta: Mapping[str, Any],
    ) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))

            if is_hidden_node(node_name):
                # Don't advance the bar, but still keep the row's "current"
                # text honest if it's a tool call (callbacks already log
                # the tool name; we just leave a hint here so the row text
                # changes between phases).
                return

            if not is_visible_node(node_name):
                # Unknown node — ignore.
                return

            # Idempotent: if checkpoint replay sends the same node twice
            # we don't double-count.
            if node_name in state.completed_visible_nodes:
                return

            state.completed_visible_nodes.append(node_name)
            state.nodes_completed = len(state.completed_visible_nodes)
            state.phase = phase_for_node(node_name)
            state.current_node = node_name

            self._push_event(ticker, f"{node_name} → done")

            if (
                self._active == ticker
                and self._progress_task is not None
            ):
                self._progress.update(
                    self._progress_task,
                    completed=state.nodes_completed,
                    node=node_name,
                    tin=_format_tokens(state.tokens_in),
                    tout=_format_tokens(state.tokens_out),
                )

            if node_name in DECISION_NODES:
                self._push_event(ticker, "decision reached")
        self._refresh()

    def on_run_complete(
        self,
        ticker: str,
        signal: str,
        elapsed_s: float,
        final_state: Mapping[str, Any],
    ) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))
            state.status = _STATUS_DONE
            state.signal = signal
            state.finished_at = time.time()
            state.current_node = signal or "done"
            self._push_event(ticker, f"✓ done · {signal}")
            if self._active == ticker and self._progress_task is not None:
                self._progress.update(
                    self._progress_task,
                    completed=len(NODE_DISPLAY_ORDER),
                    node=f"done · {signal}",
                )
        self._refresh()

    def on_run_error(self, ticker: str, exc: BaseException) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))
            state.status = _STATUS_ERROR
            state.finished_at = time.time()
            state.error = f"{type(exc).__name__}: {exc}"
            state.current_node = state.error
            self._push_event(ticker, f"✗ {type(exc).__name__}")
        self._refresh()

    def on_tool_call(self, ticker: str, tool_name: str) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))
            state.tool_calls += 1
            self._push_event(ticker, f"uses tool · {tool_name or '(unknown)'}")
        self._refresh()

    def on_llm_tokens(
        self,
        ticker: str,
        in_tokens: int,
        out_tokens: int,
        model: Optional[str],
    ) -> None:
        with self._lock:
            state = self._states.setdefault(ticker, _TickerState(ticker=ticker))
            state.llm_calls += 1
            state.tokens_in += in_tokens
            state.tokens_out += out_tokens
            if (
                self._active == ticker
                and self._progress_task is not None
            ):
                self._progress.update(
                    self._progress_task,
                    tin=_format_tokens(state.tokens_in),
                    tout=_format_tokens(state.tokens_out),
                )
        # Token events are noisy — don't repaint just for them; the next
        # node-complete or tool event will refresh.

    # ---- internals ---------------------------------------------------------

    def _push_event(self, ticker: str, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._events.append((ts, f"{ticker} · {text}"))

    def _refresh(self) -> None:
        if self._live is None:
            return
        try:
            self._live.update(self._render(), refresh=False)
        except Exception:
            pass

    def _render(self, *, final: bool = False) -> Group:
        return Group(
            self._header_panel(),
            self._table(),
            self._progress,
            self._events_panel(),
        )

    def _header_panel(self) -> Panel:
        elapsed = (
            time.time() - self._batch_started_at
            if self._batch_started_at is not None
            else 0
        )
        # Show system date alongside trade_date when they differ so the
        # operator can never confuse a backtest for a live run.
        if self._system_date and self._system_date != self._trade_date:
            date_segment = Text.assemble(
                (f"{self._trade_date}", "magenta"),
                ("  (sys ", "dim"),
                (f"{self._system_date}", "yellow"),
                (")", "dim"),
            )
        else:
            date_segment = Text(self._trade_date, style="magenta")
        head = Text.assemble(
            (f"{self._title}", "bold"),
            "  ·  ",
            (self._run_id, "cyan"),
            "  ·  ",
            date_segment,
            "  ·  elapsed ",
            (_format_elapsed(elapsed), "yellow"),
        )
        return Panel(head, padding=(0, 1), border_style="grey50")

    def _table(self) -> Table:
        table = Table(
            show_header=True,
            header_style="bold",
            expand=True,
            pad_edge=False,
            box=None,
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("Ticker", width=7, no_wrap=True)
        table.add_column("Phase", width=10, no_wrap=True)
        table.add_column("Current", ratio=1, no_wrap=False)
        table.add_column("Elapsed", width=8, justify="right", no_wrap=True)
        table.add_column("LLM", width=4, justify="right", no_wrap=True)
        table.add_column("Tools", width=5, justify="right", no_wrap=True)
        table.add_column("Tokens", width=14, justify="right", no_wrap=True)

        for ticker in self._tickers:
            state = self._states[ticker]
            glyph, glyph_style = _STATUS_GLYPH[state.status]
            phase = phase_label(state.phase) if state.phase else "—"
            current = state.current_node or "—"
            if state.status == _STATUS_DONE and state.signal:
                current = Text(f"✓ {state.signal}", style="bold green")
            elif state.status == _STATUS_ERROR and state.error:
                current = Text(state.error, style="red")
            elif state.status == _STATUS_RUNNING:
                current = Text(current, style="cyan")
            tokens = (
                f"{_format_tokens(state.tokens_in)}↑/"
                f"{_format_tokens(state.tokens_out)}↓"
            )
            table.add_row(
                Text(glyph, style=glyph_style),
                ticker,
                phase,
                current,
                _format_elapsed(state.elapsed()),
                str(state.llm_calls),
                str(state.tool_calls),
                tokens,
            )
        return table

    def _events_panel(self) -> Panel:
        if not self._events:
            body: Any = Text("(awaiting events…)", style="dim")
        else:
            lines = []
            for ts, text in self._events:
                lines.append(Text.assemble((f"{ts} ", "dim"), text))
            body = Group(*lines)
        return Panel(
            body,
            title="Recent",
            title_align="left",
            border_style="grey50",
            padding=(0, 1),
        )

    def _summary_table(self) -> Table:
        table = Table(
            title=f"Batch summary · {self._run_id}",
            title_style="bold",
            header_style="bold",
            box=None,
            pad_edge=False,
        )
        table.add_column("Ticker")
        table.add_column("Status")
        table.add_column("Signal")
        table.add_column("Elapsed", justify="right")
        table.add_column("LLM", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("Tokens", justify="right")

        for ticker in self._tickers:
            state = self._states[ticker]
            glyph, _ = _STATUS_GLYPH[state.status]
            tokens = (
                f"{_format_tokens(state.tokens_in)}↑/"
                f"{_format_tokens(state.tokens_out)}↓"
            )
            sig = state.signal or state.error or "—"
            table.add_row(
                ticker,
                f"{glyph} {state.status}",
                sig,
                _format_elapsed(state.elapsed()),
                str(state.llm_calls),
                str(state.tool_calls),
                tokens,
            )
        return table
