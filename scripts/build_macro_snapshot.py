#!/usr/bin/env python3
"""Build a macro regime snapshot.

Pulls VIX, SPX 5d return, and the 10y/3m yield curve via yfinance and
classifies the regime as ``normal`` / ``defensive`` / ``halt``.

The snapshot is **advisory by default** — this script only writes a
JSON file. The weekly workflow chooses (via ``--macro-gate``) whether
a non-normal regime should warn, throttle, or block the cadence.

Usage::

    python scripts/build_macro_snapshot.py
    python scripts/build_macro_snapshot.py --output runs/macro_2026-05-08.json
    python scripts/build_macro_snapshot.py --vix-defensive 22 --vix-halt 32

Schema and design notes are in
``tradingagents/dataflows/macro_snapshot.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tradingagents.dataflows.macro_snapshot import (  # noqa: E402
    DEFAULTS, build_snapshot, render_banner_text,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--output", type=str, default=None,
        help="Output JSON path. Defaults to runs/macro_<DATE>.json.",
    )
    p.add_argument(
        "--vix-defensive", type=float, default=DEFAULTS["defensive_vix"],
        help=f"VIX threshold for defensive regime (default {DEFAULTS['defensive_vix']}).",
    )
    p.add_argument(
        "--vix-halt", type=float, default=DEFAULTS["halt_vix"],
        help=f"VIX threshold for halt regime (default {DEFAULTS['halt_vix']}).",
    )
    p.add_argument(
        "--spx-defensive-pct", type=float,
        default=DEFAULTS["defensive_spx_5d_pct"],
        help=f"SPX 5d return % for defensive (default {DEFAULTS['defensive_spx_5d_pct']}).",
    )
    p.add_argument(
        "--spx-halt-pct", type=float, default=DEFAULTS["halt_spx_5d_pct"],
        help=f"SPX 5d return % for halt (default {DEFAULTS['halt_spx_5d_pct']}).",
    )
    p.add_argument(
        "--yc-inversion-bps", type=float,
        default=DEFAULTS["yield_curve_inversion_threshold_bps"],
        help="10y/3m yield-curve threshold in bps below which is 'inverted' (default 0).",
    )
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the one-line stdout banner.")
    return p.parse_args()


def _run(args: argparse.Namespace) -> int:
    thresholds = {
        "defensive_vix": args.vix_defensive,
        "halt_vix": args.vix_halt,
        "defensive_spx_5d_pct": args.spx_defensive_pct,
        "halt_spx_5d_pct": args.spx_halt_pct,
        "yield_curve_inversion_threshold_bps": args.yc_inversion_bps,
    }
    today = date.today()
    snapshot = build_snapshot(today=today, thresholds=thresholds)

    if args.output:
        out_path = Path(args.output).resolve()
    else:
        out_path = REPO_ROOT / "runs" / f"macro_{today.isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2))

    if not args.quiet:
        print(render_banner_text(snapshot))
        print(f"Wrote {out_path}")
    return 0


def main() -> int:
    return _run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
