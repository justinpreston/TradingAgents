#!/usr/bin/env python3
"""Build ``earnings_calendar.json`` next to a screener or matrix run.

Reads tickers from one of:
- ``--screener-run runs/screener_<id>/`` (uses ``top_tickers.txt``)
- ``--matrix-run runs/matrix_<id>/`` (uses ``verdict_ledger.json``)
- ``--tickers AAPL MSFT ...`` (explicit list)

Writes ``earnings_calendar.json`` adjacent to the run dir with one row
per ticker:

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "n_tickers": 25,
  "n_with_earnings": 22,
  "tickers": [
    {
      "ticker": "AVGO",
      "next_earnings": {"date": "...", "days_away": 14, ...},
      "history": [...],
      "beat_rate_pct": 87.5
    }
  ]
}
```

Earnings data is **opt-in enrichment**: a missing ticker just doesn't
get its ``next_earnings`` block. The downstream ``build_options_overlay``
and HTML report both fail-soft on missing rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.earnings_calendar import (  # noqa: E402
    build_summary,
    reset_cache,
)


def _resolve_tickers(args: argparse.Namespace) -> tuple[list[str], Path | None]:
    if args.tickers:
        return [t.upper() for t in args.tickers], None
    if args.screener_run:
        run = Path(args.screener_run).resolve()
        tt = run / "top_tickers.txt"
        if not tt.exists():
            raise FileNotFoundError(f"Missing {tt}")
        tickers = [
            line.strip().upper()
            for line in tt.read_text().splitlines()
            if line.strip()
        ]
        return tickers, run
    if args.matrix_run:
        run = Path(args.matrix_run).resolve()
        ledger = run / "verdict_ledger.json"
        if not ledger.exists():
            raise FileNotFoundError(f"Missing {ledger}")
        rows = json.loads(ledger.read_text())
        return [r["ticker"].upper() for r in rows if r.get("ticker")], run
    raise SystemExit("Pass exactly one of --screener-run, --matrix-run, or --tickers")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--screener-run", type=str)
    src.add_argument("--matrix-run", type=str)
    src.add_argument("--tickers", nargs="+")
    p.add_argument(
        "--output", type=str, default=None,
        help="Output path. Defaults to <run-dir>/earnings_calendar.json.",
    )
    p.add_argument(
        "--ticker-limit", type=int, default=None,
        help="Cap on tickers processed (smoke tests).",
    )
    p.add_argument(
        "--max-history-quarters", type=int, default=8,
        help="Earnings history depth (default 8 quarters).",
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    return _run(args)


def _write_calendar(*, tickers: list[str], out_path: Path,
                    max_history_quarters: int = 8,
                    verbose: bool = False,
                    today: Any = None,
                    source_run: str | None = None) -> int:
    """Build and write earnings_calendar.json. Testable seam.

    De-duplicates tickers (case-insensitive). Resets the dataflow's
    module cache up front so repeated invocations in the same process
    behave deterministically.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for t in tickers:
        u = t.upper().strip()
        if not u or u in seen:
            continue
        seen.add(u)
        deduped.append(u)

    reset_cache()
    enriched: list[dict[str, Any]] = []
    for i, ticker in enumerate(deduped, 1):
        if verbose:
            print(f"[{i:3d}/{len(deduped)}] {ticker}", flush=True)
        try:
            row = build_summary(ticker, today=today)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ {ticker}: {exc}", file=sys.stderr)
            row = None
        if row is not None:
            enriched.append(row)

    n_next = sum(1 for r in enriched if "next_earnings" in r)
    output: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "n_tickers": len(enriched),
        "n_with_next_earnings": n_next,
        "source_run": source_run,
        "tickers": enriched,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {out_path}", flush=True)
    print(f"  {len(enriched)}/{len(deduped)} tickers with any earnings data")
    print(f"  {n_next}/{len(deduped)} with a confirmed/estimated next earnings date")
    return 0


def _run(args: argparse.Namespace) -> int:
    try:
        tickers, run_dir = _resolve_tickers(args)
    except (FileNotFoundError, SystemExit) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.ticker_limit:
        tickers = tickers[: args.ticker_limit]
    if args.output:
        output_path = Path(args.output).resolve()
    elif run_dir is not None:
        output_path = run_dir / "earnings_calendar.json"
    else:
        output_path = Path("earnings_calendar.json").resolve()

    return _write_calendar(
        tickers=tickers,
        out_path=output_path,
        max_history_quarters=args.max_history_quarters,
        verbose=args.verbose,
        source_run=str(run_dir) if run_dir else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
