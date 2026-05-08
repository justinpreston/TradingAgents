"""CLI runner for the early-cycle screener.

Usage:

  # Quick test run on a small slice
  python run_screener.py --universe-limit 100 --top 10

  # Full universe scan, top 25
  python run_screener.py --top 25

  # Full scan + chain top 5 into TradingAgents (overnight pipeline)
  python run_screener.py --top 25 --chain-top 5

Outputs to ``runs/screener_<YYYY-MM-DD_HHMM>/``:
  * ``screener.json`` — full ranked list with all signals
  * ``screener.md`` — human-readable Markdown report
  * ``top_tickers.txt`` — plain list of top-N tickers (for piping)
  * ``manifest.json`` — config + provenance
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.polygon_common import recommended_min_interval_for_tier  # noqa: E402
from tradingagents.screener.orchestrator import run_screener  # noqa: E402
from tradingagents.screener.output import (  # noqa: E402
    write_json,
    write_markdown,
    write_top_tickers,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Early-cycle equity screener")
    p.add_argument("--target-date", type=str, default=None,
                   help="ISO date for screener trading day (default: most recent completed trading day)")
    p.add_argument("--min-price", type=float, default=5.0)
    p.add_argument("--min-dollar-adv", type=float, default=50_000_000.0,
                   help="Minimum last-day dollar volume in $ (default 50M)")
    p.add_argument("--min-mcap", type=float, default=2_000_000_000.0,
                   help="Minimum market cap in $ (default 2B)")
    p.add_argument("--max-mcap", type=float, default=200_000_000_000.0,
                   help="Maximum market cap in $ (default 200B)")
    p.add_argument("--universe-limit", type=int, default=None,
                   help="Cap stage-2 enrichment for testing (default: no cap)")
    p.add_argument("--ticker-limit", type=int, default=None,
                   help="Cap technical+fundamental scans for testing (default: no cap)")
    p.add_argument("--min-request-interval", type=float, default=recommended_min_interval_for_tier(),
                   help="Min seconds between Polygon REST calls. Defaults to the value "
                        "recommended for $POLYGON_TIER (free→0.25s, basic/starter→0.05s); "
                        "$POLYGON_MIN_REQUEST_INTERVAL_S overrides. Adaptive throttle "
                        "ratchets up on 429. Raise if hitting rate limits; set 0 to disable.")
    p.add_argument("--top", type=int, default=25, help="Number of top names to keep")
    p.add_argument("--technical-weight", type=float, default=0.55)
    p.add_argument("--fundamental-weight", type=float, default=0.45)
    p.add_argument("--output-dir", type=str, default=None,
                   help="Override default runs/screener_<timestamp>/ output dir")
    p.add_argument("--chain-top", type=int, default=0,
                   help="If > 0, run the TradingAgents pipeline on the top N candidates")
    p.add_argument("--chain-runner", type=str, default="run_copilot_persona_aligned.py",
                   help="Runner script to invoke for --chain-top (default: persona-aligned)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _make_progress():
    state = {"last_emit": 0.0, "stage": ""}

    def cb(stage: str, i: int, total: int, ticker: str):
        now = time.time()
        if stage != state["stage"]:
            state["stage"] = stage
            state["last_emit"] = 0.0
            print(f"\n[{stage}] {total} tickers", flush=True)
        if now - state["last_emit"] >= 2.0 or i == total:
            state["last_emit"] = now
            pct = (i / total * 100) if total else 0
            print(f"  {stage} {i}/{total} ({pct:.0f}%) — last: {ticker}", flush=True)
    return cb


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    target = date.fromisoformat(args.target_date) if args.target_date else None

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir = Path(args.output_dir) if args.output_dir else (REPO_ROOT / "runs" / f"screener_{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[screener] writing to {out_dir}", flush=True)
    print(f"[screener] config: top={args.top} mcap=${args.min_mcap/1e9:.0f}B-${args.max_mcap/1e9:.0f}B "
          f"min_price=${args.min_price} min_adv=${args.min_dollar_adv/1e6:.0f}M", flush=True)

    t0 = time.time()
    result = run_screener(
        target_date=target,
        min_price=args.min_price,
        min_dollar_adv=args.min_dollar_adv,
        min_mcap=args.min_mcap,
        max_mcap=args.max_mcap,
        universe_limit=args.universe_limit,
        ticker_limit=args.ticker_limit,
        top_n=args.top,
        technical_weight=args.technical_weight,
        fundamental_weight=args.fundamental_weight,
        min_request_interval_s=args.min_request_interval,
        progress_cb=_make_progress(),
    )
    elapsed = time.time() - t0

    write_json(result, out_dir / "screener.json")
    write_markdown(result, out_dir / "screener.md")
    write_top_tickers(result, out_dir / "top_tickers.txt", top=args.top)

    manifest = {
        "screener_run_at": datetime.now().isoformat(),
        "trading_date": result.trading_date.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "universe_size": result.universe_size,
        "config": result.config,
        "is_partial": result.is_partial,
        "rate_limited_failures": result.rate_limited_failures,
        "outputs": {
            "json": "screener.json",
            "markdown": "screener.md",
            "top_tickers": "top_tickers.txt",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n[screener] done in {elapsed/60:.1f}m — top {len(result.candidates)} candidates", flush=True)
    print(f"[screener] outputs: {out_dir}", flush=True)

    if result.is_partial:
        n_fail = len(result.rate_limited_failures)
        print(
            f"\n[screener] ⚠️  PARTIAL RESULT — {n_fail} ticker stage(s) dropped to "
            f"Polygon rate limits. Top-N may be missing legitimate names.",
            flush=True,
        )
        print(
            f"[screener] mitigation: re-run with --min-request-interval {args.min_request_interval * 1.5:.2f} "
            f"(currently {args.min_request_interval}) or shrink the universe with --min-mcap/--max-mcap.",
            flush=True,
        )
        if args.chain_top > 0:
            print(
                "[screener] refusing to chain into TradingAgents on a partial result — "
                "rerun the screener clean first.",
                flush=True,
            )

    if result.candidates:
        print("\n[screener] top 10 preview:", flush=True)
        for c in result.candidates[:10]:
            mcap_b = c.market_cap / 1e9
            print(
                f"  {c.rank:2d}. {c.ticker:6s}  score {c.composite_score:5.1f}  "
                f"${mcap_b:7.1f}B  {c.summary}",
                flush=True,
            )

    if args.chain_top > 0:
        if result.is_partial:
            return 0
        chain_n = min(args.chain_top, len(result.candidates))
        chain_tickers = [c.ticker for c in result.candidates[:chain_n]]
        print(f"\n[chain] running TradingAgents pipeline on top {chain_n}: "
              f"{', '.join(chain_tickers)}", flush=True)
        runner_path = REPO_ROOT / args.chain_runner
        if not runner_path.exists():
            print(f"[chain] runner not found: {runner_path} — skipping", flush=True)
        else:
            run_id = f"screener_{timestamp}_chain"
            cmd = [
                sys.executable,
                str(runner_path),
                *chain_tickers,
                "--run-id", run_id,
            ]
            if args.target_date:
                cmd += ["--date", args.target_date]
            print(f"[chain] {' '.join(cmd)}", flush=True)
            import subprocess
            rc = subprocess.call(cmd, env=os.environ.copy())
            chain_summary = {
                "tickers": chain_tickers,
                "run_id": run_id,
                "rc": rc,
                "agent_runs_dir": str(REPO_ROOT / "runs" / run_id),
            }
            (out_dir / "chain_summary.json").write_text(json.dumps(chain_summary, indent=2))
            print(f"[chain] done (rc={rc}) — agent runs in runs/{run_id}/", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
