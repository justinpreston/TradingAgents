"""Re-invoke only the Portfolio Manager against a saved final_state.

Design rationale
----------------
The expensive part of a TradingAgents run is the upstream debate (analysts,
researchers, risk debaters) — they each take 30-90s of LLM calls per ticker
and dominate the ~9-12 min/ticker total. The PM is a single-call synthesis
at the end. So when iterating on PM behaviour (e.g. changing the
``risk_profile`` addendum), re-running the entire graph is wasteful: the
debate transcripts don't change.

The persona-aligned and aggressive-aligned runners now persist the slice of
``final_state`` that the PM consumes (investment_plan, trader_investment_plan,
past_context, full risk_debate_state.history, company_of_interest) to
``<ticker>.state.json`` next to each ticker's log. This script loads those
state files, re-instantiates the PM with a chosen risk_profile, and writes
new ratings — typically ~3 minutes for 5 tickers vs ~45-55 minutes for a
full rerun.

Usage
-----
    python resynthesize_pm.py runs/aggressive_aligned_2026-04-30_top5 \\
        --risk-profile aggressive

    python resynthesize_pm.py runs/persona_aligned_2026-04-30_top5 \\
        --risk-profile aggressive \\
        --pm-model claude-opus-4.7-xhigh

    # Operate on a subset:
    python resynthesize_pm.py runs/aggressive_aligned_2026-04-30_top5 \\
        --risk-profile aggressive \\
        --tickers DAR APD ADI

By default the new PM output goes to a sibling directory
``<run-dir>__resynth_<profile>/`` so the original run's files are never
modified. Pass ``--in-place`` to overwrite ``<ticker>.final.md`` in the
original directory (still keeps a backup with ``.orig`` suffix).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.llm_clients import create_llm_client

load_dotenv()


DEFAULT_PM_MODEL = "gpt-5.5"


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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "run_dir",
        type=Path,
        help="Path to a runs/<run_id>/ directory containing <TICKER>.state.json files.",
    )
    p.add_argument(
        "--risk-profile",
        choices=["aggressive", "neutral", "conservative"],
        required=True,
        help="Risk profile addendum to inject into the PM prompt.",
    )
    p.add_argument(
        "--pm-model",
        default=DEFAULT_PM_MODEL,
        help=f"Model to use for the PM call (default: {DEFAULT_PM_MODEL}).",
    )
    p.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Subset of tickers to resynthesize (default: all *.state.json in run_dir).",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="Write output back into run_dir (with .orig backups). "
             "Default writes to <run_dir>__resynth_<profile>/.",
    )
    return p.parse_args()


def _build_pm(model: str, risk_profile: str):
    """Construct the PM node bound to the chosen model + risk profile."""
    llm_kwargs = {}
    if model.startswith("claude-opus") or model.startswith("claude-sonnet"):
        # The persona-aligned runner doesn't pass any provider-specific kwargs
        # for Copilot/Claude; mirror that here.
        pass
    client = create_llm_client(provider="copilot", model=model, **llm_kwargs)
    llm = client.get_llm()
    return create_portfolio_manager(llm, risk_profile=risk_profile)


def _extract_signal(decision_md: str) -> str:
    """Pull the rating off the rendered decision markdown.

    The PortfolioDecision schema renders as ``**Rating**: <Buy|Overweight|Hold|Underweight|Sell>``.
    """
    for line in (decision_md or "").splitlines():
        s = line.strip()
        if s.lower().startswith("**rating**:") or s.lower().startswith("rating:"):
            after = s.split(":", 1)[1].strip()
            return after.split()[0].strip("*").strip()
    return ""


def _load_state(state_path: Path) -> dict:
    with open(state_path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def main() -> int:
    args = _parse_args()

    if not args.run_dir.is_dir():
        sys.stderr.write(f"❌ Not a directory: {args.run_dir}\n")
        return 2

    state_files = sorted(args.run_dir.glob("*.state.json"))
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        state_files = [
            p for p in state_files
            if p.name.removesuffix(".state.json").upper() in wanted
        ]
    if not state_files:
        sys.stderr.write(
            f"❌ No <TICKER>.state.json files found in {args.run_dir}.\n"
            f"   Likely this run pre-dates the state-persistence change. "
            f"Re-run the original runner to capture state.\n"
        )
        return 1

    if args.in_place:
        out_dir = args.run_dir
    else:
        out_dir = args.run_dir.parent / f"{args.run_dir.name}__resynth_{args.risk_profile}"
        out_dir.mkdir(parents=True, exist_ok=True)

    os.environ["GITHUB_TOKEN"] = _resolve_github_token()

    pm = _build_pm(args.pm_model, args.risk_profile)

    print(f"┌─ PM resynthesis ─────────────────────────────────────────")
    print(f"│ Source        : {args.run_dir}")
    print(f"│ Risk profile  : {args.risk_profile}")
    print(f"│ PM model      : {args.pm_model}")
    print(f"│ Output dir    : {out_dir}")
    print(f"│ Tickers       : {len(state_files)}")
    print(f"└──────────────────────────────────────────────────────────")

    summary = {
        "source_run": str(args.run_dir),
        "risk_profile": args.risk_profile,
        "pm_model": args.pm_model,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "results": [],
    }

    for state_path in state_files:
        ticker = state_path.name.removesuffix(".state.json")
        started = time.time()
        try:
            state = _load_state(state_path)
            result = pm(state)
            decision_md = result["final_trade_decision"]
            signal = _extract_signal(decision_md)
            elapsed = round(time.time() - started, 1)

            md_path = out_dir / f"{ticker}.final.md"
            if args.in_place and md_path.exists():
                shutil.copy(md_path, md_path.with_suffix(".final.md.orig"))
            md_path.write_text(decision_md, encoding="utf-8")

            print(f"   ✓ {ticker:<6} → {signal or '?':<12} ({elapsed:.1f}s)  [{md_path.name}]")
            summary["results"].append({
                "ticker": ticker,
                "signal": signal,
                "elapsed_sec": elapsed,
                "output": str(md_path),
                "status": "ok",
            })
        except Exception as exc:
            elapsed = round(time.time() - started, 1)
            print(f"   ✗ {ticker:<6} → ERROR ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
            summary["results"].append({
                "ticker": ticker,
                "elapsed_sec": elapsed,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary_path = out_dir / f"resynth_{args.risk_profile}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n📝 {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
