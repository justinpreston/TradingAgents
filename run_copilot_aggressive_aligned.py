"""Run TradingAgents with an aggressive-growth persona alignment.

Inverts the routing in ``run_copilot_persona_aligned.py`` to dial up the
bull/aggressive voice and damp the synthesizer's Buy→Hold drift. Built
in response to the question: *the persona-aligned config tilts toward
Hold/Underweight on early-cycle screener picks (e.g. DAR), can we run
with a more aggressive risk tolerance?*

What changed vs ``run_copilot_persona_aligned.py``:

  * ``bull_researcher``      GPT-5.5 → **Opus 4.7 xhigh**
        Give the bull side the deeper synthesizer voice — Opus's
        natural strength is multi-paragraph thesis-building, which
        the persona-aligned config was using to support skepticism.
  * ``aggressive_analyst``   GPT-5.5 → **Opus 4.7 xhigh**
        Same: deeper synthesis under aggressive frame.
  * ``bear_researcher``      Opus → **GPT-5.5**
        Bull-with-discipline disposition gives a shorter, more
        rebut-able bear case.
  * ``research_manager``     Opus → **GPT-5.5**
        On the 2024-05-10 A/B, Opus stepped Buy→Hold on AAPL when
        the bear had merit; GPT-5.5 stayed Overweight. Here we
        want the rubric-style verdict, not the cautious synthesis.
  * ``neutral_analyst``      Opus → **GPT-5.5**
        GPT-5.5 crowns the Aggressive analyst 3/4 times (per A/B).
  * ``conservative_analyst`` Opus → **GPT-5.5**
        Keeps the capital-preservation voice but in the faster,
        more rebuttable model.

What did NOT change:

  * ``portfolio_manager`` stays GPT-5.5 — clean rubric output is the
    operational requirement (Rating, Stop Loss, Position Sizing).
  * Data-layer analysts (market/social/news/fundamentals/trader)
    stay GPT-5.5 — these produce the structured fields the rest of
    the graph consumes.

Trade-off vs persona-aligned:

  + Surfaces Overweight/Buy on early-cycle setups where multi-quarter
    fundamental trends are strong but latest-Q fundamentals haven't
    fully confirmed yet (the screener's bread and butter).
  + Stop-loss / sizing fields still produced by GPT-5.5 PM, so
    operational discipline is preserved.
  - Higher risk of false-positive Overweights on fundamentally
    impaired names. **The mitigation is bet sizing, not bet
    selection** — pair this runner with the ScreenerResult
    composite score and only act on names where tech ≥ 50 AND
    fund ≥ 65, capped at 2-3% per name.

Output goes to ``runs/aggressive_aligned_<date>/`` so it doesn't
overwrite the conservative-leaning ``persona_aligned_<date>`` runs
— the two are directly comparable on the same tickers / same date.

Usage:
    python run_copilot_aggressive_aligned.py
    python run_copilot_aggressive_aligned.py --date 2026-04-30 ABNB TVTX DAR APD ADI
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
DEFAULT_DATE: str | None = None

OPUS = "claude-opus-4.7-xhigh"
GPT55 = "gpt-5.5"

PERSONA_MODELS = {
    "market_analyst":         GPT55,
    "social_analyst":         GPT55,
    "news_analyst":            GPT55,
    "fundamentals_analyst":   GPT55,
    "bull_researcher":        OPUS,    # ← flipped from GPT55: deeper bull synthesis
    "bear_researcher":        GPT55,   # ← flipped from OPUS: shorter, more rebuttable bear
    "research_manager":       GPT55,   # ← flipped from OPUS: rubric verdict, no Buy→Hold drift
    "trader":                 GPT55,
    "aggressive_analyst":     OPUS,    # ← flipped from GPT55: deepest aggressive synthesis
    "neutral_analyst":        GPT55,   # ← flipped from OPUS: aggressive-crowner disposition
    "conservative_analyst":   GPT55,   # ← flipped from OPUS: capital pres but rebuttable
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
    re-running the upstream debate (~3 min vs ~46 min full rerun).
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
             "investor's risk tolerance. 'aggressive' biases the synthesizer "
             "toward Buy/Overweight when growth signals + stops are intact; "
             "'conservative' biases toward Hold/Underweight on any bear merit. "
             "Default: no addendum (neutral synthesis).",
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
    run_id = args.run_id or f"aggressive_aligned_{trade_date}"

    os.environ["GITHUB_TOKEN"] = _resolve_github_token()
    config = _build_config(risk_profile=args.risk_profile)

    runs_dir = Path("runs") / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    use_dashboard = not args.no_dashboard and sys.stderr.isatty()

    manifest = {
        "run_id": run_id,
        "alignment": "aggressive",
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

    real_stdout.write(f"System date : {system_date}\n")
    real_stdout.write(f"Trade date  : {trade_date}  ({date_label})\n")
    real_stdout.flush()

    if not use_dashboard:
        real_stdout.write(f"┌─ Aggressive-aligned multi-ticker batch ───────────────────\n")
        real_stdout.write(f"│ Tickers   : {', '.join(tickers)}\n")
        real_stdout.write(f"│ Date      : {trade_date}\n")
        real_stdout.write(f"│ Alignment : AGGRESSIVE — bull/aggressive on Opus xhigh,\n")
        real_stdout.write(f"│             bear/conservative/neutral/RM on GPT-5.5\n")
        real_stdout.write(f"│ PM model  : GPT-5.5  (operational rubric output)\n")
        real_stdout.write(f"│ Output    : {runs_dir.resolve()}\n")
        real_stdout.write(f"└────────────────────────────────────────────────────────────\n")
        for role, model in PERSONA_MODELS.items():
            tag = "GPT-5.5" if model == GPT55 else "Opus 4.7"
            real_stdout.write(f"   {role:<22} → {tag}\n")
        real_stdout.write("\n")

    dashboard = (
        RunDashboard(
            tickers=tickers,
            trade_date=trade_date,
            run_id=run_id,
            title="TradingAgents · aggressive-aligned",
            system_date=system_date,
        )
        if use_dashboard
        else None
    )
    callback_handler = TokenToolCallbackHandler(listener=dashboard)

    ta = TradingAgentsGraph(
        debug=not use_dashboard,
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
            {"run_id": run_id, "alignment": "aggressive",
             "trade_date": trade_date,
             "trade_date_label": date_label,
             "system_date_at_run": system_date,
             "persona_models": PERSONA_MODELS, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )

    md_lines = [
        f"# TradingAgents aggressive-aligned batch — {run_id}",
        "",
        f"- **Date:** {trade_date}  ({date_label})",
        f"- **System date at run:** {system_date}",
        f"- **Provider:** copilot",
        f"- **Alignment:** AGGRESSIVE (bull/aggressive on Opus xhigh; bear/conservative/neutral/RM on GPT-5.5)",
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
