#!/usr/bin/env python3
"""Run the weekly cadence across all market-cap tiers (mid + large + mega).

Each tier is a separate ``weekly_workflow.py`` invocation with its own
mcap band, ADV floor, and tier-tagged screener/matrix run_ids. Tiers run
sequentially (NOT in parallel) because Polygon's free-tier REST endpoints
are rate-limited at 5 req/min, and three concurrent screeners would just
serialize on the limiter anyway.

Default behavior is screen-only across all three tiers (no chaining), which
lets you review NEW lists across all caps before spending LLM cycles on
matrix runs. Pass ``--chain`` to auto-launch matrix on the top-N NEW tickers
in each tier.

────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────

  # Standard weekly Friday EOD: screen all 3 tiers, print next-step commands
  .venv/bin/python scripts/run_weekly_all_tiers.py --top 25

  # Auto-launch matrix on the top-3 NEW tickers in each tier (~2-3 hours)
  .venv/bin/python scripts/run_weekly_all_tiers.py --top 25 --chain --chain-top 3

  # Only run two of the three tiers
  .venv/bin/python scripts/run_weekly_all_tiers.py --tiers mid mega

  # Dry run — show what would happen without spending API calls
  .venv/bin/python scripts/run_weekly_all_tiers.py --dry-run

────────────────────────────────────────────────────────────────────────
Why a wrapper instead of a single weekly_workflow flag
────────────────────────────────────────────────────────────────────────

Each tier produces an independent screener output, matrix run, and diff
context. Bundling them into one weekly_workflow invocation would force the
diff to interleave tiers (a mid-cap diff vs the last mega-cap matrix is
useless). Keeping them as N independent invocations preserves the
single-tier semantics of weekly_workflow.py and makes each tier's output
fully reproducible on its own.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEEKLY = REPO_ROOT / "scripts" / "weekly_workflow.py"

ALL_TIERS = ("mid", "large", "mega")


def _hr(label: str = "") -> None:
    width = 76
    if label:
        print(f"\n╭{'─' * (width - 2)}╮", flush=True)
        print(f"│ {label}".ljust(width - 1) + "│", flush=True)
        print(f"╰{'─' * (width - 2)}╯", flush=True)
    else:
        print("─" * width, flush=True)


def _run_tier(tier: str, args: argparse.Namespace) -> int:
    """Invoke weekly_workflow.py for a single tier; return its exit code."""
    py = sys.executable
    cmd: list[str] = [py, str(WEEKLY), "--tier", tier, "--top", str(args.top)]
    if args.target_date:
        cmd += ["--target-date", args.target_date]
    if args.chain:
        cmd += ["--chain", "--chain-top", str(args.chain_top),
                "--chain-max-parallel", str(args.chain_max_parallel)]
    if args.min_request_interval is not None:
        cmd += ["--min-request-interval", str(args.min_request_interval)]
    if args.dry_run:
        cmd += ["--dry-run"]

    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL).returncode


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tiers", nargs="+", choices=ALL_TIERS, default=list(ALL_TIERS),
                   help="Tiers to run (default: mid large mega)")
    p.add_argument("--top", type=int, default=25,
                   help="Top-N from each tier's screener (default 25)")
    p.add_argument("--target-date", type=str, default=None,
                   help="Trading date for screeners (YYYY-MM-DD); defaults to today")
    p.add_argument("--chain", action="store_true",
                   help="Auto-launch matrix on top-N NEW tickers in each tier")
    p.add_argument("--chain-top", type=int, default=5,
                   help="When chaining, how many NEW tickers to feed per tier (default 5)")
    p.add_argument("--chain-max-parallel", type=int, default=5,
                   help="Max parallel matrix cells per chained tier (default 5)")
    p.add_argument("--min-request-interval", type=float, default=None,
                   help="Min seconds between Polygon REST calls per tier "
                        "(default: screener decides). Use 0.38 if you hit "
                        "rate-limit warnings during screener phase 2.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing")
    args = p.parse_args()

    started = datetime.now()
    chain_label = f" · chain top-{args.chain_top}" if args.chain else " · screen-only"
    header_l1 = f"Weekly TradingAgents cadence · ALL TIERS · {started.strftime('%Y-%m-%d %H:%M %a')}"
    header_l2 = f"Tiers: {', '.join(args.tiers)}{chain_label}"
    width = 74
    print("╔" + "═" * (width + 2) + "╗", flush=True)
    print(f"║ {header_l1.ljust(width)} ║", flush=True)
    print(f"║ {header_l2.ljust(width)} ║", flush=True)
    print("╚" + "═" * (width + 2) + "╝", flush=True)

    failures: list[str] = []
    for tier in args.tiers:
        _hr(f"Tier · {tier.upper()}")
        rc = _run_tier(tier, args)
        if rc != 0:
            failures.append(tier)
            print(f"  ⚠ {tier} exited with code {rc} — continuing with remaining tiers")

    elapsed = datetime.now() - started
    _hr("All tiers complete")
    print(f"  Total elapsed: {elapsed.total_seconds():.0f}s "
          f"({elapsed.total_seconds() / 60:.1f} min)")
    if failures:
        print(f"  ⚠ Failed tiers: {', '.join(failures)}")
        return 1

    print()
    print("  Next steps:")
    print("    1. Review NEW ticker lists in each tier's Phase 5 output above")
    print("    2. Selectively chain matrix on whichever tier(s) have interesting NEW names:")
    print("         .venv/bin/python run_copilot_matrix.py \\")
    print("             --tickers <NEW1> <NEW2> ... \\")
    print("             --screener-run runs/screener_<tier>_<ts> \\")
    print("             --run-id matrix_<tier>_weekly_<ts>_chain")
    print("    3. After matrix runs, build cross-tier HTML comparison:")
    print("         .venv/bin/python scripts/build_html_report.py \\")
    print("             --runs runs/matrix_mid_*:mid \\")
    print("                    runs/matrix_large_*:large \\")
    print("                    runs/matrix_mega_*:mega \\")
    print("             --output runs/cross_run_$(date +%Y-%m-%d)/report.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
