#!/usr/bin/env python3
"""Same-day (14:00 ET) options refresh for today's matrix runs.

Part of the Friday cadence restructure: the weekly matrix now auto-chains
at 06:30 pre-market (see scripts/launchd/com.tradingagents.weekly.plist),
and this script re-pulls the options chain mid-day so strikes/IV/net-debit
are fresh right before entry (Polygon snapshot data ages within hours
intraday — see the "same-day options refresh" cadence rule in CLAUDE.md).

Discovery is by the matrix run's ``verdict_ledger.json["snapshot_date"]``
(the trade/snapshot date the run was built for), NOT directory mtime —
mtime drifts every time a script re-touches a run directory (accounting,
options overlay, Chronos, indexing all write into the same dir), so it's
an unreliable proxy for "was this run built for today's trade date."

For each matching run, shells out to build_options_overlay.py in
long-call mode (stdin=DEVNULL per the subprocess invariants in CLAUDE.md).
Does NOT touch lean/signals.json — regenerating it would clobber the
user's approved:true flips, which is the explicit human-in-the-loop gate
for the LEAN execution integration.

If scripts/build_friday_packet.py exists at runtime, it's invoked
afterward as a best-effort step (another builder may add it in this same
restructure); its absence is not an error.

Usage:
    .venv/bin/python scripts/friday_options_refresh.py
    .venv/bin/python scripts/friday_options_refresh.py --date 2026-07-03
    .venv/bin/python scripts/friday_options_refresh.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"


def _iter_matrix_run_dirs(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(
        p for p in runs_dir.iterdir()
        if p.is_dir() and (p / "verdict_ledger.json").is_file()
    )


def find_todays_matrix_runs(runs_dir: Path, target_date: str) -> list[Path]:
    """Return matrix run dirs whose verdict_ledger.json snapshot_date == target_date.

    Deliberately keys off the ledger's recorded trade/snapshot date, not
    filesystem mtime — mtime is touched by every downstream script
    (accounting, options overlay, Chronos, indexer) that writes into the
    same run directory, so it drifts away from "when was this run built"
    almost immediately after the matrix itself finishes.
    """
    matches: list[Path] = []
    for run_dir in _iter_matrix_run_dirs(runs_dir):
        ledger_path = run_dir / "verdict_ledger.json"
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if ledger.get("snapshot_date") == target_date:
            matches.append(run_dir)
    return matches


def _refresh_one(matrix_run: Path, strategy_mode: str, dry_run: bool) -> int:
    rel = matrix_run.relative_to(REPO_ROOT) if matrix_run.is_absolute() else matrix_run
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "build_options_overlay.py"),
        "--matrix-run", str(rel),
        "--strategy-mode", strategy_mode,
    ]
    print(f"  $ {' '.join(cmd)}")
    if dry_run:
        print("    (skipped: --dry-run)")
        return 0
    # stdin=DEVNULL is a hard subprocess invariant (see CLAUDE.md) — without
    # it, children launched from a detached/headless context (e.g. launchd)
    # can crash at startup on a broken inherited stdin descriptor.
    completed = subprocess.run(
        cmd, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
    )
    return completed.returncode


def _run_friday_packet_best_effort(dry_run: bool) -> None:
    packet_script = REPO_ROOT / "scripts" / "build_friday_packet.py"
    if not packet_script.exists():
        print("  (scripts/build_friday_packet.py not present — skipping)")
        return
    cmd = [sys.executable, str(packet_script)]
    print(f"  $ {' '.join(cmd)}")
    if dry_run:
        print("    (skipped: --dry-run)")
        return
    try:
        rc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        ).returncode
        if rc != 0:
            print(f"    ⚠ build_friday_packet.py exited {rc} (best-effort; not fatal)")
    except OSError as exc:
        print(f"    ⚠ build_friday_packet.py failed to launch: {exc} (best-effort; not fatal)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", default=None,
                   help="Target trade/snapshot date YYYY-MM-DD (default: today)")
    p.add_argument("--runs-dir", default=str(RUNS_DIR),
                   help="Directory containing matrix run subdirectories (default: runs)")
    p.add_argument("--strategy-mode", choices=["tier-driven", "long-call"],
                   default="long-call",
                   help="Options overlay strategy mode (default: long-call, "
                        "matching the locked-in user preference).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without executing")
    args = p.parse_args()

    target_date = args.date or date.today().isoformat()
    runs_dir = Path(args.runs_dir)

    print(f"╭─ Friday options refresh · target date {target_date}")
    matrix_runs = find_todays_matrix_runs(runs_dir, target_date)
    if not matrix_runs:
        print(f"│ No matrix runs found with snapshot_date == {target_date} in {runs_dir}")
        print("╰────────────────────────────────────────────────────────────")
        return 1

    print(f"│ Found {len(matrix_runs)} matrix run(s) for {target_date}:")
    for r in matrix_runs:
        print(f"│   - {r.relative_to(REPO_ROOT) if r.is_absolute() else r}")
    print("╰────────────────────────────────────────────────────────────")

    failures: list[str] = []
    for run_dir in matrix_runs:
        print(f"\n→ Refreshing options overlay: {run_dir.name}")
        rc = _refresh_one(run_dir, args.strategy_mode, args.dry_run)
        if rc != 0:
            failures.append(run_dir.name)
            print(f"  ⚠ {run_dir.name} exited {rc}")
        else:
            print(f"  ✅ {run_dir.name} refreshed")

    print("\n→ Friday packet (best-effort)")
    _run_friday_packet_best_effort(args.dry_run)

    print()
    print(f"Summary: {len(matrix_runs) - len(failures)}/{len(matrix_runs)} refreshed successfully")
    if failures:
        print(f"⚠ Failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
