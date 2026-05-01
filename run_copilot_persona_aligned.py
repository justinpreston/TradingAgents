"""Run TradingAgents on multiple tickers with persona-aligned models.

Routing each agent to the model whose natural disposition fits its role,
based on the structural findings from the Opus xhigh vs GPT-5.5 xhigh A/B
on 2024-05-10 (see ``runs/comparison_2024-05-10.md``):

  * GPT-5.5 is bull-with-discipline, crowns the Aggressive risk analyst
    3/4 times, writes 8.8K-char structured market reports, produces
    rubric-style verdicts with explicit ``**Price Target**`` lines.
  * Opus 4.7 is a synthesizer, crowns the Neutral risk analyst 3/4 times,
    writes 270-char market reports but 3-4× longer narrative syntheses,
    steps Buy → Hold when the bear case has merit.

Persona alignment:

  * Data layer (analysts, trader, bull, aggressive risk, PM)  → GPT-5.5
    — fills the structured fields, produces executable directives.
  * Synthesis / "wait for evidence" roles (bear, research manager,
    neutral & conservative risk analysts)                    → Opus
    — natural skeptic voice, calibrated middle, ledger-style adjudication.

Portfolio Manager is GPT-5.5 in this configuration: the goal is to land a
clean ``**Price Target** / **Time Horizon** / **Rating**`` rubric in the
final output, not a 7K-char narrative essay. The bear/RM/neutral nodes
upstream still surface skeptic reasoning that the GPT-5.5 PM weighs.

Output goes to ``runs/persona_aligned_<date>/`` so neither prior batch
(``runs/mega_ai_2024-05-10`` Opus, ``runs/gpt55_2024-05-10`` GPT-5.5)
is overwritten — the three runs are directly comparable on the same
4 tickers / same date / same Path D PIT-corrected fundamentals.

Usage:
    python run_copilot_persona_aligned.py
    python run_copilot_persona_aligned.py --date 2024-05-10 NVDA AAPL MSFT GOOGL
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
from tradingagents.ui import RunDashboard, TokenToolCallbackHandler

load_dotenv()


DEFAULT_TICKERS = ["NVDA", "AAPL", "MSFT", "GOOGL"]
# ``--date`` defaults to today's system date. The 2024-05-10 batches in
# ``runs/mega_ai_2024-05-10`` and ``runs/gpt55_2024-05-10`` are reachable
# explicitly via ``--date 2024-05-10`` for the 3-way comparison.
DEFAULT_DATE: str | None = None

OPUS = "claude-opus-4.7-xhigh"
GPT55 = "gpt-5.5"

PERSONA_MODELS = {
    "market_analyst":         GPT55,
    "social_analyst":         GPT55,
    "news_analyst":           GPT55,
    "fundamentals_analyst":   GPT55,
    "bull_researcher":        GPT55,
    "bear_researcher":        OPUS,
    "research_manager":       OPUS,
    "trader":                 GPT55,
    "aggressive_analyst":     GPT55,
    "neutral_analyst":        OPUS,
    "conservative_analyst":   OPUS,
    "portfolio_manager":      GPT55,
}


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


def _build_config(risk_profile: str | None = None) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = "copilot"
    # quick / deep fall-throughs for any role NOT in persona_models. With the
    # current map every role is overridden, so these only matter for the
    # framework's reflector / signal_processor (utility, not persona).
    cfg["quick_think_llm"] = GPT55
    cfg["deep_think_llm"] = OPUS
    cfg["max_debate_rounds"] = 1
    cfg["checkpoint_enabled"] = True
    cfg["persona_models"] = PERSONA_MODELS
    if risk_profile:
        cfg["risk_profile"] = risk_profile
    return cfg


PM_STATE_KEYS = (
    "company_of_interest",
    "trade_date",
    "investment_plan",
    "trader_investment_plan",
    "past_context",
    "final_trade_decision",
    "risk_debate_state",
    "investment_debate_state",
)


def _persist_state_for_resynthesis(final_state, state_path: Path, ticker: str, trade_date: str) -> None:
    """Persist the slice of final_state that the PM needs for resynthesis.

    Captures investment_plan, trader_investment_plan, past_context, and the
    full risk_debate_state.history so resynthesize_pm.py can re-invoke the
    Portfolio Manager with a different risk_profile addendum without
    re-running the upstream debate (~3 min vs ~50+ min full rerun).
    """
    snapshot = {"_ticker": ticker, "_trade_date": trade_date}
    for key in PM_STATE_KEYS:
        if key in final_state:
            try:
                json.dumps(final_state[key])
                snapshot[key] = final_state[key]
            except (TypeError, ValueError):
                snapshot[key] = str(final_state[key])
    state_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickers", nargs="*", help=f"Tickers (default: {' '.join(DEFAULT_TICKERS)})")
    p.add_argument(
        "--date",
        default=DEFAULT_DATE,
        help="Trade date YYYY-MM-DD (default: today's system date). Refuses future dates.",
    )
    p.add_argument("--run-id", default=None)
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the live progress dashboard and stream raw agent output.",
    )
    p.add_argument(
        "--risk-profile",
        choices=["aggressive", "neutral", "conservative"],
        default=None,
        help="Inject a Portfolio Manager prompt addendum reflecting the "
             "investor's risk tolerance. Default: no addendum (neutral synthesis).",
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
    run_id = args.run_id or f"persona_aligned_{trade_date}"

    os.environ["GITHUB_TOKEN"] = _resolve_github_token()
    config = _build_config(risk_profile=args.risk_profile)

    runs_dir = Path("runs") / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Auto-disable the dashboard on non-TTY (CI, piped output) so logs
    # don't get garbled by ANSI escapes.
    use_dashboard = not args.no_dashboard and sys.stderr.isatty()

    # Write a manifest documenting exactly which model each persona ran on,
    # so the comparative analysis later doesn't have to grep through code.
    manifest = {
        "run_id": run_id,
        "risk_profile": args.risk_profile,
        "trade_date": trade_date,
        "trade_date_label": date_label,
        "system_date_at_run": system_date,
        "provider": "copilot",
        "max_debate_rounds": 1,
        "pit_fundamentals_fix": "Path D (yf_pit_derivations) — landed 2026-04-29",
        "persona_models": PERSONA_MODELS,
        "tickers": tickers,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "dashboard": use_dashboard,
    }
    (runs_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    real_stdout, real_stderr = sys.stdout, sys.stderr

    # Always announce the resolved date pair so backtests vs. live can never
    # be confused. Goes to stdout *before* the dashboard takes over.
    real_stdout.write(f"System date : {system_date}\n")
    real_stdout.write(f"Trade date  : {trade_date}  ({date_label})\n")
    real_stdout.flush()

    if not use_dashboard:
        # Legacy banner — only when the dashboard isn't going to draw one.
        real_stdout.write(f"┌─ Persona-aligned multi-ticker batch ──────────────────────\n")
        real_stdout.write(f"│ Tickers   : {', '.join(tickers)}\n")
        real_stdout.write(f"│ Date      : {trade_date}\n")
        real_stdout.write(f"│ PIT fix   : Path D (yf_pit_derivations) active\n")
        real_stdout.write(f"│ PM model  : GPT-5.5  (operational rubric output)\n")
        real_stdout.write(f"│ Output    : {runs_dir.resolve()}\n")
        real_stdout.write(f"└────────────────────────────────────────────────────────────\n")
        for role, model in PERSONA_MODELS.items():
            tag = "GPT-5.5" if model == GPT55 else "Opus 4.7"
            real_stdout.write(f"   {role:<22} → {tag}\n")
        real_stdout.write("\n")

    # Build the dashboard / no-op listener pair.
    dashboard = (
        RunDashboard(
            tickers=tickers,
            trade_date=trade_date,
            run_id=run_id,
            title="TradingAgents · persona-aligned",
            system_date=system_date,
        )
        if use_dashboard
        else None
    )
    callback_handler = TokenToolCallbackHandler(listener=dashboard)

    ta = TradingAgentsGraph(
        debug=not use_dashboard,  # legacy pretty-print only when no dashboard
        config=config,
        callbacks=[callback_handler],
        progress_listener=dashboard,
    )

    results = []

    def _run_loop() -> None:
        for idx, ticker in enumerate(tickers, 1):
            log_path = runs_dir / f"{ticker}.log"
            if not use_dashboard:
                banner = f"\n[{idx}/{len(tickers)}] ▶ {ticker}  ({trade_date})  → {log_path.name}"
                real_stdout.write(banner + "\n")
                real_stdout.flush()

            started = time.time()
            record: dict = {"ticker": ticker, "trade_date": trade_date, "log": str(log_path)}
            callback_handler.set_ticker(ticker)

            try:
                with open(log_path, "w", encoding="utf-8", buffering=1) as fp:
                    if use_dashboard:
                        # Dashboard owns the terminal — route everything to log.
                        out_target, err_target = fp, fp
                    else:
                        out_target = _TeeStream(fp, real_stdout)
                        err_target = _TeeStream(fp, real_stderr)
                    with redirect_stdout(out_target), redirect_stderr(err_target):
                        final_state, signal = ta.propagate(ticker, trade_date)
                record["status"] = "ok"
                record["signal"] = signal
                record["final_trade_decision"] = final_state.get("final_trade_decision", "")
                stats = callback_handler.get_stats()
                record["llm_calls"] = stats["llm_calls"]
                record["tool_calls"] = stats["tool_calls"]
                record["tokens_in"] = stats["tokens_in"]
                record["tokens_out"] = stats["tokens_out"]

                state_path = runs_dir / f"{ticker}.state.json"
                _persist_state_for_resynthesis(final_state, state_path, ticker, trade_date)
                record["state"] = str(state_path)
            except KeyboardInterrupt:
                record["status"] = "interrupted"
                record["error"] = "KeyboardInterrupt"
                record["elapsed_sec"] = round(time.time() - started, 1)
                results.append(record)
                if not use_dashboard:
                    real_stdout.write("\n⚠️  Interrupted — stopping batch.\n")
                raise
            except Exception as exc:
                record["status"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["traceback"] = traceback.format_exc()
                if not use_dashboard:
                    real_stdout.write(f"   ✗ {ticker} failed: {record['error']}\n")
            finally:
                record.setdefault("elapsed_sec", round(time.time() - started, 1))

            if record["status"] == "ok" and not use_dashboard:
                real_stdout.write(
                    f"   ✓ {ticker} → {record['signal']}  ({record['elapsed_sec']:.0f}s)\n"
                )
            results.append(record)

    interrupted = False
    try:
        if dashboard is not None:
            with dashboard:
                try:
                    _run_loop()
                except KeyboardInterrupt:
                    interrupted = True
        else:
            try:
                _run_loop()
            except KeyboardInterrupt:
                interrupted = True
    finally:
        callback_handler.set_ticker(None)

    summary_json = runs_dir / "summary.json"
    summary_md = runs_dir / "summary.md"

    summary_json.write_text(
        json.dumps(
            {"run_id": run_id, "trade_date": trade_date,
             "trade_date_label": date_label,
             "system_date_at_run": system_date,
             "persona_models": PERSONA_MODELS, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )

    md_lines = [
        f"# TradingAgents persona-aligned batch — {run_id}",
        "",
        f"- **Date:** {trade_date}  ({date_label})",
        f"- **System date at run:** {system_date}",
        f"- **Provider:** copilot",
        f"- **PIT fix:** Path D (yf_pit_derivations) active",
        f"- **PM model:** GPT-5.5",
        f"- **Tickers:** {len(results)}",
        "",
        "## Persona → model assignment",
        "",
        "| Role | Model |",
        "|---|---|",
    ]
    for role, model in PERSONA_MODELS.items():
        tag = "GPT-5.5" if model == GPT55 else "Opus 4.7 xhigh"
        md_lines.append(f"| `{role}` | {tag} |")
    md_lines += [
        "",
        "## Results",
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

    if not use_dashboard:
        real_stdout.write("\n┌─ Batch complete ──────────────────────────────────────\n")
        for r in results:
            sig = r.get("signal") or r.get("error", "—")
            real_stdout.write(f"│ {r['ticker']:<6} {sig:<14} {r['elapsed_sec']:>6.0f}s  [{r['status']}]\n")
        real_stdout.write(f"└─ Summary: {summary_md.resolve()}\n")
    else:
        real_stdout.write(f"\nSummary written to: {summary_md.resolve()}\n")

    if interrupted:
        return 130
    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
