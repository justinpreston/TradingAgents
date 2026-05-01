"""Weekly cadence orchestrator — locks in the recommended workflow.

For long-call / multi-month-tenor trading, the recommended rhythm is:

  • Friday EOD weekly: run the screener
  • On-demand: matrix-run only on NEW tickers (not already in the catalog)
  • Same-day before entry: refresh the options overlay on the relevant matrix
    run (Polygon snapshot data ages within hours intraday)

This script automates phases 1–2 and reports phase 3 as next-step commands.
The SQLite catalog at runs/index.db (built by scripts/index_runs.py) is the
source of truth for "have I already matrix-ran this ticker this cycle?"

────────────────────────────────────────────────────────────────────────
Phases
────────────────────────────────────────────────────────────────────────

  1. SCREEN     — run the screener (or reuse one with --use-screener-run)
  2. INDEX      — re-build runs/index.db
  3. DIFF       — compare this week's top-N against tickers already
                  matrix-ran in the catalog. Categorize:
                    NEW      → recommend matrix run (concrete command)
                    REPEAT   → matrix can be skipped; verdict still valid
                                unless earnings happened this week
                    DROPPED  → previously screener-promoted, now off the list
  4. CHAIN      — (optional, with --chain) launch matrix on NEW tickers via
                  run_screener.py's existing --chain-top flag (re-uses
                  whichever runner is configured for chained matrix runs)
  5. REPORT     — print copy-paste commands for everything that wasn't
                  auto-launched (matrix, options overlay, HTML rebuild)

────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────

  # Standard weekly Friday EOD run (no chaining):
  .venv/bin/python scripts/weekly_workflow.py --top 25

  # Weekly + auto-launch matrix on the top-5 NEW tickers:
  .venv/bin/python scripts/weekly_workflow.py --top 25 --chain --chain-top 5

  # Use an existing screener run (e.g. you already ran it this morning):
  .venv/bin/python scripts/weekly_workflow.py \\
      --use-screener-run runs/screener_2026-05-01_0750 --top 25

  # Show what *would* happen without launching anything:
  .venv/bin/python scripts/weekly_workflow.py --dry-run

────────────────────────────────────────────────────────────────────────
Triggers that should override the schedule (run NOW, not on cadence)
────────────────────────────────────────────────────────────────────────

  • An existing pick reports earnings    → re-matrix that ticker
  • VIX spike >25 or SPX −5% in a week   → re-screen (regime shift breaks
                                            momentum signals)
  • Quarter-end (Apr/Jul/Oct/Jan)        → full screener refresh + accept
                                            new fundamental data lands
"""
from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
CATALOG_DB = RUNS_DIR / "index.db"


def _hr(label: str = "") -> None:
    width = 72
    if label:
        print(f"\n── {label} " + "─" * (width - len(label) - 4))
    else:
        print("─" * width)


def _run(cmd: list[str], dry_run: bool = False) -> int:
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    if dry_run:
        print("    (skipped: --dry-run)")
        return 0
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def phase_screen(args: argparse.Namespace) -> Path:
    """Run the screener (or return an existing run path)."""
    _hr("Phase 1 · SCREEN")

    if args.use_screener_run:
        run_path = (REPO_ROOT / args.use_screener_run).resolve()
        if not run_path.is_dir() or not (run_path / "screener.json").exists():
            print(f"  ❌ Not a valid screener run: {run_path}", file=sys.stderr)
            sys.exit(1)
        print(f"  Reusing existing screener run: {run_path.relative_to(REPO_ROOT)}")
        return run_path

    cmd = [".venv/bin/python", "run_screener.py", "--top", str(args.top)]
    if args.target_date:
        cmd += ["--target-date", args.target_date]
    rc = _run(cmd, args.dry_run)
    if rc != 0:
        print(f"  ❌ Screener exited with code {rc}", file=sys.stderr)
        sys.exit(rc)

    if args.dry_run:
        return RUNS_DIR / "<dry-run-screener>"

    # Find the screener run that was just created (newest screener_* dir)
    screener_runs = sorted(RUNS_DIR.glob("screener_*"), key=lambda p: p.stat().st_mtime)
    if not screener_runs:
        print("  ❌ Could not locate the screener output directory", file=sys.stderr)
        sys.exit(1)
    latest = screener_runs[-1]
    print(f"  → Screener output: {latest.relative_to(REPO_ROOT)}")
    return latest


def phase_index(dry_run: bool) -> None:
    _hr("Phase 2 · INDEX")
    rc = _run([".venv/bin/python", "scripts/index_runs.py"], dry_run)
    if rc != 0:
        print(f"  ⚠ Indexer exited {rc}; continuing anyway", file=sys.stderr)


def _read_top_tickers(screener_run: Path, top_n: int) -> list[str]:
    """Read top-N tickers from a screener run."""
    top_path = screener_run / "top_tickers.txt"
    if top_path.exists():
        tickers = [t.strip() for t in top_path.read_text().splitlines() if t.strip()]
        return tickers[:top_n]

    json_path = screener_run / "screener.json"
    if json_path.exists():
        data = json.loads(json_path.read_text())
        candidates = data.get("candidates") or []
        return [c["ticker"] for c in candidates[:top_n]]

    return []


def phase_diff(screener_run: Path, top_n: int, dry_run: bool) -> dict[str, list[str]]:
    """Compare current top-N against tickers already matrix-ran in the catalog.

    Returns dict with NEW / REPEAT / DROPPED ticker lists.
    """
    _hr("Phase 3 · DIFF (this week vs catalog)")

    if dry_run and not screener_run.exists():
        print("  (dry-run: would diff against runs/index.db)")
        return {"NEW": [], "REPEAT": [], "DROPPED": []}

    top_now = _read_top_tickers(screener_run, top_n)
    if not top_now:
        print("  ⚠ No tickers found in screener run; nothing to diff", file=sys.stderr)
        return {"NEW": [], "REPEAT": [], "DROPPED": []}

    last_matrix_picks: set[str] = set()
    last_run_id: str | None = None
    if CATALOG_DB.exists():
        conn = sqlite3.connect(CATALOG_DB)
        try:
            cur = conn.execute(
                """SELECT run_id FROM runs WHERE run_type='matrix'
                   ORDER BY snapshot_date DESC, run_id DESC LIMIT 1"""
            )
            row = cur.fetchone()
            if row:
                last_run_id = row[0]
                last_matrix_picks = {
                    r[0]
                    for r in conn.execute(
                        "SELECT ticker FROM matrix_picks WHERE run_id=? AND classification='PICK'",
                        (last_run_id,),
                    )
                }
        finally:
            conn.close()

    top_set = set(top_now)
    new = [t for t in top_now if t not in last_matrix_picks]
    repeat = [t for t in top_now if t in last_matrix_picks]
    dropped = sorted(last_matrix_picks - top_set)

    print(f"  This week's top-{top_n}: {len(top_now)} tickers")
    if last_run_id:
        print(f"  Compared against: {last_run_id} ({len(last_matrix_picks)} prior PICKs)")
    else:
        print("  No prior matrix runs in catalog → all tickers are NEW")

    print()
    print(f"  🆕 NEW     ({len(new):>2}): {', '.join(new) if new else '—'}")
    print(f"  ♻  REPEAT  ({len(repeat):>2}): {', '.join(repeat) if repeat else '—'}")
    print(f"  ⊘  DROPPED ({len(dropped):>2}): {', '.join(dropped) if dropped else '—'}")

    return {"NEW": new, "REPEAT": repeat, "DROPPED": dropped, "last_run_id": last_run_id or ""}


def phase_chain(screener_run: Path, args: argparse.Namespace, diff: dict) -> None:
    """Optionally launch the matrix runner directly on NEW tickers."""
    _hr("Phase 4 · CHAIN")

    new_tickers = diff.get("NEW") or []
    if not args.chain:
        print("  (skipped: --chain not set; commands printed in Phase 5)")
        return

    if not new_tickers:
        print("  No NEW tickers — nothing to matrix-run.")
        return

    chain_n = min(args.chain_top, len(new_tickers))
    chain_tickers = new_tickers[:chain_n]
    runner_path = REPO_ROOT / args.chain_runner
    if not runner_path.exists():
        print(f"  ❌ Runner not found: {runner_path}")
        print(f"     Pass --chain-runner with a valid runner script.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    run_id = f"weekly_{timestamp}_chain"

    print(f"  Matrix-running top-{chain_n} NEW tickers via {runner_path.name}:")
    print(f"    {', '.join(chain_tickers)}")
    print(f"    → run_id = {run_id}")

    cmd = [".venv/bin/python", str(runner_path.relative_to(REPO_ROOT)),
           *chain_tickers, "--run-id", run_id]
    if args.target_date:
        cmd += ["--date", args.target_date]

    rc = _run(cmd, args.dry_run)
    if rc == 0 and not args.dry_run:
        print(f"  ✅ Matrix complete. Re-index with: .venv/bin/python scripts/index_runs.py")
    elif rc != 0:
        print(f"  ⚠ Runner exited {rc}", file=sys.stderr)


def phase_report(screener_run: Path, args: argparse.Namespace, diff: dict) -> None:
    _hr("Phase 5 · NEXT STEPS")

    new_tickers = diff.get("NEW") or []
    chain_n = min(args.chain_top, len(new_tickers)) if args.chain_top else 5

    rel_screener = screener_run.relative_to(REPO_ROOT) if screener_run.exists() else Path("runs/<screener>")

    if new_tickers and not args.chain:
        target_list = " ".join(new_tickers[:chain_n])
        ts = datetime.now().strftime("%Y-%m-%d_%H%M")
        print(f"\n  🎯 MATRIX (run on {chain_n} NEW tickers — bypasses re-screening):")
        print(
            f"     .venv/bin/python {args.chain_runner} \\\n"
            f"         {target_list} \\\n"
            f"         --run-id weekly_{ts}_chain"
        )
    elif new_tickers and args.chain:
        print("\n  🎯 MATRIX: launched in Phase 4 (above).")
    else:
        print("\n  🎯 MATRIX: nothing new to run — last week's verdicts still apply.")

    print("\n  📈 OPTIONS REFRESH (same-day before entry, on whichever matrix run):")
    print(
        "     .venv/bin/python scripts/build_options_overlay.py \\\n"
        "         --matrix-run runs/<matrix_id> --strategy-mode long-call"
    )

    print("\n  📊 RE-INDEX after any new matrix or options overlay:")
    print("     .venv/bin/python scripts/index_runs.py --query")

    print("\n  🌐 CROSS-RUN HTML (rebuild whenever the runs you're tracking change):")
    print(
        "     .venv/bin/python scripts/build_html_report.py \\\n"
        "         --runs runs/<matrix_id_1>:large runs/<matrix_id_2>:mid \\\n"
        "         --output runs/cross_run_$(date +%Y-%m-%d)/report.html"
    )

    print(f"\n  Screener output for this cycle: {rel_screener}")

    if diff.get("DROPPED"):
        print(f"\n  ⊘  Note: previously promoted tickers no longer in screener: "
              f"{', '.join(diff['DROPPED'])}")
        print("     Consider whether to close existing positions in those names.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--top", type=int, default=25,
                   help="Top-N from screener to consider (default 25)")
    p.add_argument("--target-date", type=str, default=None,
                   help="Trading date for screener (YYYY-MM-DD); defaults to today")
    p.add_argument("--use-screener-run", type=str, default=None,
                   help="Skip phase 1 and reuse this screener run (e.g. runs/screener_2026-05-01_0750)")
    p.add_argument("--chain", action="store_true",
                   help="Auto-launch matrix on NEW tickers using --chain-runner")
    p.add_argument("--chain-top", type=int, default=5,
                   help="When chaining matrix, how many NEW tickers to feed (default 5)")
    p.add_argument("--chain-runner", type=str, default="run_copilot_persona_aligned.py",
                   help="Runner script to invoke for chained matrix runs "
                        "(default run_copilot_persona_aligned.py)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing")
    args = p.parse_args()

    started = datetime.now()
    print(f"╔══════════════════════════════════════════════════════════════════════╗")
    print(f"║ Weekly TradingAgents cadence · {started.strftime('%Y-%m-%d %H:%M %a')}                       ║")
    print(f"╚══════════════════════════════════════════════════════════════════════╝")

    screener_run = phase_screen(args)
    phase_index(args.dry_run)
    diff = phase_diff(screener_run, args.top, args.dry_run)
    phase_chain(screener_run, args, diff)
    phase_report(screener_run, args, diff)

    elapsed = datetime.now() - started
    _hr()
    print(f"  Done in {elapsed.total_seconds():.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
