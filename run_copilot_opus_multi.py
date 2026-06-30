"""Run TradingAgents on multiple tickers sequentially via Copilot Opus 4.7 xhigh.

Why sequential and not parallel:
  - Copilot Chat API will rate-limit aggressive parallel reasoning calls.
  - Parallel runs would interleave stdout into an unreadable mess.
  - Each ticker takes ~5-15 min on Opus xhigh; an unattended batch is fine.

Per-ticker output is captured to ``runs/<run_id>/<TICKER>.log``; a combined
summary lands in ``runs/<run_id>/summary.{md,json}`` at the end. Crashes on
one ticker do NOT stop the batch — failures are recorded in the summary.

The TradingAgentsGraph instance is reused across tickers so the memory log
accumulates lessons learned (this is the framework's intended use). The
``checkpoint_enabled`` config makes any single ticker resumable from its
last successful node if interrupted mid-run.

Usage:
    python run_copilot_opus_multi.py NVDA AAPL MSFT
    python run_copilot_opus_multi.py --date 2024-05-10 NVDA AAPL MSFT GOOGL META
    python run_copilot_opus_multi.py             # uses DEFAULT_TICKERS
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.utils import resolve_trade_date

load_dotenv()


DEFAULT_TICKERS = ["NVDA", "AAPL", "MSFT"]
# ``--date`` defaults to today's system date — see ``resolve_trade_date``.
DEFAULT_DATE: str | None = None


def _resolve_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            token = subprocess.check_output(
                ["gh", "auth", "token"], stderr=subprocess.DEVNULL, text=True
            ).strip()
            if token:
                return token
        except subprocess.CalledProcessError:
            pass
    raise SystemExit(
        "No GitHub token available. Run `gh auth login` (Copilot subscription required)."
    )


class _TeeStream(io.TextIOBase):
    """Write everything to both a per-ticker log file and the real terminal."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):  # type: ignore[override]
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):  # type: ignore[override]
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def _build_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = "copilot"
    cfg["quick_think_llm"] = "claude-opus-4.8"
    cfg["deep_think_llm"] = "claude-opus-4.8"
    cfg["max_debate_rounds"] = 1
    # Resume any single ticker from last successful node if a run is interrupted.
    cfg["checkpoint_enabled"] = True
    return cfg


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "tickers",
        nargs="*",
        help=f"One or more tickers (default: {' '.join(DEFAULT_TICKERS)})",
    )
    p.add_argument(
        "--date",
        default=DEFAULT_DATE,
        help="Trade date YYYY-MM-DD (default: today's system date). Refuses future dates.",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Subdir under runs/ (default: timestamp). Reuse to share output dir across invocations.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    tickers = [t.upper().strip() for t in (args.tickers or DEFAULT_TICKERS)]
    try:
        trade_date, date_label = resolve_trade_date(args.date)
    except ValueError as exc:
        sys.stderr.write(f"❌ {exc}\n")
        return 2
    system_date = datetime.now().date().isoformat()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    os.environ["GITHUB_TOKEN"] = _resolve_github_token()
    config = _build_config()

    runs_dir = Path("runs") / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    print(f"┌─ Multi-ticker batch ──────────────────────────────────")
    print(f"│ Tickers : {', '.join(tickers)}")
    print(f"│ System  : {system_date}")
    print(f"│ Date    : {trade_date}  ({date_label})")
    print(f"│ Provider: copilot / claude-opus-4.8")
    print(f"│ Output  : {runs_dir.resolve()}")
    print(f"└────────────────────────────────────────────────────────\n")

    # One graph instance — memory log accumulates across tickers (intended).
    ta = TradingAgentsGraph(debug=True, config=config)

    results = []
    real_stdout, real_stderr = sys.stdout, sys.stderr

    for idx, ticker in enumerate(tickers, 1):
        log_path = runs_dir / f"{ticker}.log"
        banner = f"\n[{idx}/{len(tickers)}] ▶ {ticker}  ({trade_date})  → {log_path.name}"
        real_stdout.write(banner + "\n")
        real_stdout.flush()

        started = time.time()
        record: dict = {"ticker": ticker, "trade_date": trade_date, "log": str(log_path)}

        try:
            with open(log_path, "w", encoding="utf-8", buffering=1) as fp:
                tee_out = _TeeStream(fp, real_stdout)
                tee_err = _TeeStream(fp, real_stderr)
                with redirect_stdout(tee_out), redirect_stderr(tee_err):
                    final_state, signal = ta.propagate(ticker, trade_date)
            record["status"] = "ok"
            record["signal"] = signal
            record["final_trade_decision"] = final_state.get("final_trade_decision", "")
        except KeyboardInterrupt:
            record["status"] = "interrupted"
            record["error"] = "KeyboardInterrupt"
            results.append(record)
            real_stdout.write("\n⚠️  Interrupted — stopping batch.\n")
            break
        except Exception as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()
            real_stdout.write(f"   ✗ {ticker} failed: {record['error']}\n")
        finally:
            record["elapsed_sec"] = round(time.time() - started, 1)

        if record["status"] == "ok":
            real_stdout.write(
                f"   ✓ {ticker} → {record['signal']}  ({record['elapsed_sec']:.0f}s)\n"
            )
        results.append(record)

    # ── Summary ─────────────────────────────────────────────────────────
    summary_json = runs_dir / "summary.json"
    summary_md = runs_dir / "summary.md"

    summary_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "trade_date": trade_date,
                "trade_date_label": date_label,
                "system_date_at_run": system_date,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    md_lines = [
        f"# TradingAgents batch — {run_id}",
        "",
        f"- **Date:** {trade_date}  ({date_label})",
        f"- **System date at run:** {system_date}",
        f"- **Provider:** copilot / claude-opus-4.8",
        f"- **Tickers:** {len(results)}",
        "",
        "| Ticker | Signal | Elapsed | Status |",
        "|---|---|---|---|",
    ]
    for r in results:
        sig = r.get("signal") or "—"
        status = r["status"] if r["status"] == "ok" else f"**{r['status']}**"
        md_lines.append(f"| {r['ticker']} | {sig} | {r['elapsed_sec']:.0f}s | {status} |")
    md_lines.append("")
    for r in results:
        if r.get("final_trade_decision"):
            md_lines.append(f"## {r['ticker']} — {r.get('signal', '')}")
            md_lines.append("")
            md_lines.append(r["final_trade_decision"].strip())
            md_lines.append("")
    summary_md.write_text("\n".join(md_lines), encoding="utf-8")

    real_stdout.write("\n┌─ Batch complete ──────────────────────────────────────\n")
    for r in results:
        sig = r.get("signal") or r.get("error", "—")
        real_stdout.write(f"│ {r['ticker']:<6} {sig:<14} {r['elapsed_sec']:>6.0f}s  [{r['status']}]\n")
    real_stdout.write(f"└─ Summary: {summary_md.resolve()}\n")

    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
