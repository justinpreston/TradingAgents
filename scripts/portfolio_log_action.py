"""Append a single action to runs/portfolio/trades_log.jsonl.

Use this immediately after executing a trade in your broker — it gives the
portfolio advisor skill a record of what you actually did so future
conversations can follow up ("did you trim VIRT?") and avoid re-suggesting
trades you've already made.

This script does NOT modify positions.json. You still need to update your
holdings ledger separately. The log captures *intent + execution context*;
positions.json captures *current state*.

Examples::

    # Closed a third of VIRT calls
    .venv/bin/python scripts/portfolio_log_action.py \\
        --ticker VIRT --action TRIM --qty 2 \\
        --premium 5.65 --underlying 51.40 \\
        --notes "Took half profits, kept 3 contracts running"

    # Opened a new starter long-call on CTRE
    .venv/bin/python scripts/portfolio_log_action.py \\
        --ticker CTRE --action OPEN --qty 17 \\
        --strike 40 --expiry 2026-10-16 --premium 2.30 \\
        --underlying 41.60 --notes "Tier A starter from 5/8 matrix"

    # Just a freeform note (no qty)
    .venv/bin/python scripts/portfolio_log_action.py \\
        --ticker INTC --action NOTE \\
        --notes "Earnings 7/24; will reassess after"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.portfolio_load_context import DEFAULT_TRADES_LOG  # noqa: E402

VALID_ACTIONS = {"OPEN", "CLOSE", "TRIM", "ADD", "ROLL", "EXIT", "NOTE"}


def append_entry(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ticker", required=True)
    p.add_argument("--action", required=True, choices=sorted(VALID_ACTIONS))
    p.add_argument("--id", default=None, help="Position id from positions.json (recommended)")
    p.add_argument("--qty", type=float, default=None)
    p.add_argument("--premium", type=float, default=None,
                   help="Per-share premium for options trades")
    p.add_argument("--underlying", type=float, default=None,
                   help="Underlying price at time of trade")
    p.add_argument("--strike", type=float, default=None)
    p.add_argument("--expiry", default=None, help="ISO date (YYYY-MM-DD) for the option leg")
    p.add_argument("--notes", default=None)
    p.add_argument("--source", default="manual",
                   help="Where this action came from (e.g. manual, broker_csv)")
    p.add_argument("--ts", default=None,
                   help="Override timestamp (ISO 8601); defaults to now in UTC")
    p.add_argument("--log-file", type=Path, default=DEFAULT_TRADES_LOG)
    args = p.parse_args(argv)

    ts = args.ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = {
        "ts": ts,
        "ticker": args.ticker.upper(),
        "action": args.action.upper(),
        "id": args.id,
        "qty": args.qty,
        "premium_per_share": args.premium,
        "underlying": args.underlying,
        "strike": args.strike,
        "expiry": args.expiry,
        "notes": args.notes,
        "source": args.source,
    }
    entry = {k: v for k, v in entry.items() if v is not None}

    append_entry(args.log_file, entry)
    print(f"✅ Logged {entry['action']} {entry['ticker']} → {args.log_file}")
    print(json.dumps(entry, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
