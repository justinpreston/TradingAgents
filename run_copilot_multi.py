"""Run TradingAgents on multiple tickers sequentially via Copilot Chat API.

Generalized version — works with any Copilot-served model (Opus 4.7, GPT-5.5,
Sonnet 4.6, etc.). For the Opus-only legacy launcher see run_copilot_opus_multi.py.

Why sequential and not parallel:
  - Copilot Chat API will rate-limit aggressive parallel reasoning calls.
  - Parallel runs would interleave stdout into an unreadable mess.
  - Each ticker takes ~5-15 min on a high-reasoning model; an unattended batch is fine.

Per-ticker output is captured to ``runs/<run_id>/<TICKER>.log``; a combined
summary lands in ``runs/<run_id>/summary.{md,json}`` at the end. Crashes on
one ticker do NOT stop the batch — failures are recorded in the summary.

Usage:
    python run_copilot_multi.py NVDA AAPL MSFT
    python run_copilot_multi.py --model gpt-5.5 NVDA AAPL MSFT
    python run_copilot_multi.py --model claude-opus-4.7-xhigh --date 2024-05-10 NVDA AAPL
    python run_copilot_multi.py --quick gpt-5.4-mini --deep gpt-5.5 NVDA   # asymmetric
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

from typing import Optional

from dotenv import load_dotenv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.utils import resolve_trade_date

load_dotenv()


DEFAULT_TICKERS = ["NVDA", "AAPL", "MSFT"]
# ``--date`` defaults to today's system date — see ``resolve_trade_date``.
DEFAULT_DATE: str | None = None
DEFAULT_MODEL = "claude-opus-4.7-xhigh"

# Suffixes that indicate the effort is already encoded in the model id
# (Anthropic-on-Copilot convention). When the model id ends with one of these,
# we don't pass reasoning_effort separately.
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh")


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


def _build_config(
    quick_model: str,
    deep_model: str,
    reasoning_effort: Optional[str],
    runs_dir: Path,
    share_memory: bool,
) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = "copilot"
    cfg["quick_think_llm"] = quick_model
    cfg["deep_think_llm"] = deep_model
    cfg["max_debate_rounds"] = 1
    # Resume any single ticker from last successful node if interrupted.
    cfg["checkpoint_enabled"] = True
    if reasoning_effort:
        # Routes to ChatOpenAI's reasoning_effort kwarg via the Responses API
        # for Copilot GPT-5 family. Anthropic-on-Copilot encodes effort in the
        # model id and ignores this.
        cfg["openai_reasoning_effort"] = reasoning_effort

    # Isolate per-run framework outputs so multi-model A/B comparisons can't
    # overwrite each other. The framework's _log_state writes to
    # results_dir/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json
    # — without this, running NVDA on date D twice (once Opus, once GPT-5.5)
    # destroys the first run's state dump.
    framework_dumps = runs_dir / "framework_dumps"
    framework_dumps.mkdir(parents=True, exist_ok=True)
    cfg["results_dir"] = str(framework_dumps)

    # Memory log accumulates lessons-learned across runs. Sharing it lets a
    # later model see an earlier model's reflections — that bleeds the A/B.
    # Default: per-run private memory; pass --share-memory to opt into the
    # shared global memory (useful for sequential learning of one model).
    if not share_memory:
        cfg["memory_log_path"] = str(runs_dir / "trading_memory.md")
    return cfg


def _safe_slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"Copilot model id used for BOTH quick + deep slots (default: {DEFAULT_MODEL}). "
            "Examples: gpt-5.5, claude-opus-4.7-xhigh, claude-sonnet-4.6, gpt-5.4."
        ),
    )
    p.add_argument(
        "--quick",
        default=None,
        help="Override quick_think_llm only. Useful for asymmetric setups (cheap analysts, expensive deep thinkers).",
    )
    p.add_argument(
        "--deep",
        default=None,
        help="Override deep_think_llm only. Useful for asymmetric setups.",
    )
    p.add_argument(
        "--reasoning-effort",
        default=None,
        choices=["none", "low", "medium", "high", "xhigh"],
        help=(
            "Override reasoning effort for OpenAI-family models (gpt-5*). "
            "Auto-defaults to 'xhigh' when neither --quick nor --deep model id "
            "carries an effort suffix (e.g. claude-opus-4.7-xhigh already encodes it)."
        ),
    )
    p.add_argument(
        "--share-memory",
        action="store_true",
        help=(
            "Share the global memory log (~/.tradingagents/memory/trading_memory.md) "
            "instead of using a private per-run memory. Default is private to keep "
            "model-vs-model A/B comparisons clean."
        ),
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Subdir under runs/ (default: <model>_<timestamp>). Reuse to share output dir.",
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
    quick_model = args.quick or args.model
    deep_model = args.deep or args.model
    model_label = args.model if quick_model == deep_model else f"{quick_model}+{deep_model}"

    # Resolve reasoning effort. If explicit, use it. Otherwise default to xhigh
    # only when the model id does NOT already encode effort (Anthropic naming).
    if args.reasoning_effort:
        reasoning_effort = args.reasoning_effort
    elif quick_model.endswith(_EFFORT_SUFFIXES) and deep_model.endswith(_EFFORT_SUFFIXES):
        reasoning_effort = None  # Encoded in model id
    else:
        reasoning_effort = "xhigh"

    run_id = args.run_id or f"{_safe_slug(model_label)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    runs_dir = Path("runs") / run_id
    if runs_dir.exists() and any(runs_dir.iterdir()):
        # Refuse to clobber a previous run unless the user passed an explicit
        # --run-id (signalling intent to resume / append).
        if not args.run_id:
            sys.exit(
                f"runs/{run_id} already has output. Pass --run-id <name> to be explicit "
                "about resuming/appending, or remove the directory."
            )
    runs_dir.mkdir(parents=True, exist_ok=True)

    os.environ["GITHUB_TOKEN"] = _resolve_github_token()
    config = _build_config(
        quick_model, deep_model, reasoning_effort, runs_dir, args.share_memory
    )

    # runs_dir already created above

    print(f"┌─ Multi-ticker batch ──────────────────────────────────")
    print(f"│ Tickers : {', '.join(tickers)}")
    print(f"│ System  : {system_date}")
    print(f"│ Date    : {trade_date}  ({date_label})")
    print(f"│ Provider: copilot")
    if quick_model == deep_model:
        print(f"│ Model   : {quick_model}")
    else:
        print(f"│ Quick   : {quick_model}")
        print(f"│ Deep    : {deep_model}")
    if reasoning_effort:
        print(f"│ Effort  : {reasoning_effort}")
    print(f"│ Memory  : {'shared global' if args.share_memory else 'per-run (private)'}")
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
        record: dict = {
            "ticker": ticker,
            "trade_date": trade_date,
            "log": str(log_path),
            "model": {"quick": quick_model, "deep": deep_model},
        }

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
            record["elapsed_sec"] = round(time.time() - started, 1)
            results.append(record)
            real_stdout.write("\n⚠️  Interrupted — stopping batch.\n")
            break
        except Exception as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()
            real_stdout.write(f"   ✗ {ticker} failed: {record['error']}\n")
        finally:
            record.setdefault("elapsed_sec", round(time.time() - started, 1))

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
                "model": {"quick": quick_model, "deep": deep_model},
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
        f"- **Provider:** copilot",
        f"- **Quick model:** `{quick_model}`",
        f"- **Deep model:** `{deep_model}`",
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
    real_stdout.write(f"│ Model   : {model_label}\n")
    for r in results:
        sig = r.get("signal") or r.get("error", "—")
        real_stdout.write(f"│ {r['ticker']:<6} {sig:<14} {r['elapsed_sec']:>6.0f}s  [{r['status']}]\n")
    real_stdout.write(f"└─ Summary: {summary_md.resolve()}\n")

    return 0 if all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
