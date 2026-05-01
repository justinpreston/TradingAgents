"""Index TradingAgents run artifacts into a SQLite catalog for cross-run queries.

This script walks `runs/` and indexes screener runs, matrix runs, and options
overlays into a single SQLite database (`runs/index.db` by default). The raw
files (verdict_ledger.json, exec summaries, options_overlay.{md,json}) remain
the source of truth — the SQLite catalog is purely derived/regenerable.

Idempotent: re-running on the same data UPDATEs in place. Re-running on a
file with unchanged mtime is skipped unless --force is passed.

Schema:
- runs:            one row per indexed run directory (any type)
- screener_picks:  one row per ranked ticker per screener run
- matrix_picks:    one row per ranked ticker per matrix run
- options_legs:    one row per option leg per matrix run

Usage:
    .venv/bin/python scripts/index_runs.py                # index all of runs/
    .venv/bin/python scripts/index_runs.py --runs-dir runs --db runs/index.db
    .venv/bin/python scripts/index_runs.py --force        # re-index all
    .venv/bin/python scripts/index_runs.py --query        # show summary stats

Cross-run queries you can now do via `sqlite3 runs/index.db`:

  -- All Tier A picks across all matrix runs:
  SELECT run_id, ticker, current_price, aggressive_pt, comp_pct
  FROM matrix_picks WHERE classification='Tier A'
  ORDER BY run_id DESC, comp_pct;

  -- Tickers that appear in the most matrix runs:
  SELECT ticker, COUNT(DISTINCT run_id) n_runs
  FROM matrix_picks GROUP BY ticker
  ORDER BY n_runs DESC LIMIT 20;

  -- Long-call ROI distribution by tier:
  SELECT tier,
         COUNT(*) n,
         ROUND(AVG(approx_gain_at_aggr_pt_per_contract), 0) avg_gain,
         ROUND(AVG(net_debit_per_contract), 0) avg_debit
  FROM options_legs WHERE strategy_mode='long-call'
  GROUP BY tier;
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    run_type         TEXT NOT NULL,
    run_path         TEXT NOT NULL,
    snapshot_date    TEXT,
    generated_at     TEXT,
    n_candidates     INTEGER,
    n_picks          INTEGER,
    n_vetoed         INTEGER,
    indexed_at       TEXT NOT NULL,
    source_mtime     REAL
);

CREATE TABLE IF NOT EXISTS screener_picks (
    run_id              TEXT NOT NULL,
    rank                INTEGER NOT NULL,
    ticker              TEXT NOT NULL,
    name                TEXT,
    market_cap          REAL,
    sector_sic          TEXT,
    sector_desc         TEXT,
    composite_score     REAL,
    technical_score     REAL,
    fundamental_score   REAL,
    last_close          REAL,
    rsi_14              REAL,
    vol_expansion       REAL,
    notable_flags       TEXT,
    PRIMARY KEY (run_id, ticker),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_screener_ticker ON screener_picks(ticker);
CREATE INDEX IF NOT EXISTS idx_screener_score ON screener_picks(composite_score);

CREATE TABLE IF NOT EXISTS matrix_picks (
    run_id                    TEXT NOT NULL,
    rank                      INTEGER,
    ticker                    TEXT NOT NULL,
    name                      TEXT,
    sector_sic                TEXT,
    market_cap                REAL,
    composite_score           REAL,
    current_price             REAL,
    aggressive_rating         TEXT,
    aggressive_pt             REAL,
    aggressive_upside_pct     REAL,
    aggressive_horizon        TEXT,
    conservative_rating       TEXT,
    conservative_pt           REAL,
    conservative_upside_pct   REAL,
    conservative_horizon      TEXT,
    pt_compression_pct        REAL,
    classification            TEXT,
    tier                      TEXT,
    aggressive_executive_summary TEXT,
    PRIMARY KEY (run_id, ticker),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_matrix_ticker ON matrix_picks(ticker);
CREATE INDEX IF NOT EXISTS idx_matrix_classification ON matrix_picks(classification);
CREATE INDEX IF NOT EXISTS idx_matrix_tier ON matrix_picks(tier);

CREATE TABLE IF NOT EXISTS options_legs (
    run_id                          TEXT NOT NULL,
    ticker                          TEXT NOT NULL,
    strategy_mode                   TEXT,
    strategy                        TEXT,
    tier                            TEXT,
    expiration                      TEXT,
    dte                             INTEGER,
    horizon                         TEXT,
    leg_index                       INTEGER NOT NULL,
    leg_side                        TEXT,
    leg_type                        TEXT,
    leg_strike                      REAL,
    leg_open_interest               INTEGER,
    leg_iv                          REAL,
    leg_delta                       REAL,
    leg_price                       REAL,
    leg_price_source                TEXT,
    net_debit_per_share             REAL,
    net_debit_per_contract          REAL,
    breakeven_underlying            REAL,
    breakeven_pct_from_current      REAL,
    risk_reward                     REAL,
    max_profit_per_contract         REAL,
    max_loss_per_contract           REAL,
    upside_to_short_strike_pct      REAL,
    long_call_delta                 REAL,
    long_call_moneyness             TEXT,
    approx_gain_at_aggr_pt_per_contract  REAL,
    approx_gain_at_cons_pt_per_contract  REAL,
    liquidity_warnings              TEXT,
    PRIMARY KEY (run_id, ticker, leg_index),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_options_ticker ON options_legs(ticker);
CREATE INDEX IF NOT EXISTS idx_options_strategy ON options_legs(strategy);
CREATE INDEX IF NOT EXISTS idx_options_mode ON options_legs(strategy_mode);

CREATE VIEW IF NOT EXISTS ticker_history AS
SELECT
    'matrix' AS source,
    run_id,
    ticker,
    classification,
    tier,
    current_price,
    aggressive_pt,
    conservative_pt,
    pt_compression_pct
FROM matrix_picks
UNION ALL
SELECT
    'screener' AS source,
    run_id,
    ticker,
    NULL AS classification,
    NULL AS tier,
    last_close AS current_price,
    NULL AS aggressive_pt,
    NULL AS conservative_pt,
    NULL AS pt_compression_pct
FROM screener_picks;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify_run(run_dir: Path) -> str | None:
    """Return run_type or None if unindexable."""
    name = run_dir.name
    if name.startswith("screener_") and (run_dir / "screener.json").exists():
        return "screener"
    if name.startswith("matrix_") and (run_dir / "verdict_ledger.json").exists():
        return "matrix"
    if name.startswith("cross_run_"):
        return None  # Synthesized from other runs; nothing new to index here
    return None


def _classification_to_tier(row: dict) -> str | None:
    """Derive A/B/C tier from a verdict_ledger row, matching the same
    convention used by build_options_overlay._tier()."""
    cls = (row.get("classification") or "").strip()
    if cls == "VETOED":
        return "VETO"
    if cls != "PICK":
        return None
    comp = row.get("pt_compression_pct")
    if row.get("conservative_pt") is None:
        return "C"
    if comp is not None and comp < 5.0:
        return "A"
    return "B"


def index_screener_run(conn: sqlite3.Connection, run_dir: Path,
                       screener_path: Path, force: bool) -> bool:
    mtime = screener_path.stat().st_mtime
    cur = conn.execute("SELECT source_mtime FROM runs WHERE run_id=?", (run_dir.name,))
    row = cur.fetchone()
    if row and row[0] and row[0] >= mtime and not force:
        return False

    data = json.loads(screener_path.read_text())
    candidates = data.get("candidates", [])

    conn.execute(
        """INSERT INTO runs (run_id, run_type, run_path, snapshot_date, generated_at,
                             n_candidates, n_picks, n_vetoed, indexed_at, source_mtime)
           VALUES (?, 'screener', ?, ?, NULL, ?, ?, NULL, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
               run_type=excluded.run_type,
               run_path=excluded.run_path,
               snapshot_date=excluded.snapshot_date,
               n_candidates=excluded.n_candidates,
               n_picks=excluded.n_picks,
               indexed_at=excluded.indexed_at,
               source_mtime=excluded.source_mtime""",
        (run_dir.name, str(run_dir), data.get("trading_date"),
         data.get("universe_size"), len(candidates), _now(), mtime),
    )

    conn.execute("DELETE FROM screener_picks WHERE run_id=?", (run_dir.name,))
    rows = []
    for c in candidates:
        tech = c.get("technical") or {}
        rows.append((
            run_dir.name,
            c.get("rank"),
            c.get("ticker"),
            c.get("name"),
            c.get("market_cap"),
            c.get("sector_sic"),
            c.get("sector_desc"),
            c.get("composite_score"),
            c.get("technical_score"),
            c.get("fundamental_score"),
            tech.get("last_close"),
            tech.get("rsi_14"),
            tech.get("vol_expansion"),
            json.dumps(c.get("notable_flags") or []),
        ))
    conn.executemany(
        """INSERT INTO screener_picks (run_id, rank, ticker, name, market_cap, sector_sic,
                                       sector_desc, composite_score, technical_score,
                                       fundamental_score, last_close, rsi_14, vol_expansion,
                                       notable_flags)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return True


def index_matrix_run(conn: sqlite3.Connection, run_dir: Path,
                     ledger_path: Path, force: bool) -> tuple[bool, bool]:
    """Returns (matrix_indexed, options_indexed)."""
    mtime = ledger_path.stat().st_mtime
    options_path = run_dir / "options_overlay.json"
    options_mtime = options_path.stat().st_mtime if options_path.exists() else None
    combined_mtime = max(mtime, options_mtime) if options_mtime else mtime

    cur = conn.execute("SELECT source_mtime FROM runs WHERE run_id=?", (run_dir.name,))
    row = cur.fetchone()
    if row and row[0] and row[0] >= combined_mtime and not force:
        return (False, False)

    data = json.loads(ledger_path.read_text())
    rows = data.get("rows", [])

    # Tier label is derived per-row from verdict_ledger 'classification'
    n_picks = sum(1 for r in rows if "VETOED" not in (r.get("classification") or ""))
    n_vetoed = sum(1 for r in rows if "VETOED" in (r.get("classification") or ""))

    conn.execute(
        """INSERT INTO runs (run_id, run_type, run_path, snapshot_date, generated_at,
                             n_candidates, n_picks, n_vetoed, indexed_at, source_mtime)
           VALUES (?, 'matrix', ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
               run_type=excluded.run_type,
               run_path=excluded.run_path,
               snapshot_date=excluded.snapshot_date,
               generated_at=excluded.generated_at,
               n_candidates=excluded.n_candidates,
               n_picks=excluded.n_picks,
               n_vetoed=excluded.n_vetoed,
               indexed_at=excluded.indexed_at,
               source_mtime=excluded.source_mtime""",
        (run_dir.name, str(run_dir), data.get("snapshot_date"), data.get("generated_at"),
         len(rows), n_picks, n_vetoed, _now(), combined_mtime),
    )

    conn.execute("DELETE FROM matrix_picks WHERE run_id=?", (run_dir.name,))
    matrix_rows = []
    for r in rows:
        cls = r.get("classification")
        matrix_rows.append((
            run_dir.name,
            r.get("rank"),
            r.get("ticker"),
            r.get("name"),
            r.get("sector_sic"),
            r.get("market_cap_usd"),
            r.get("composite_score"),
            r.get("current_price_usd"),
            r.get("aggressive_rating"),
            r.get("aggressive_pt"),
            r.get("aggressive_upside_pct"),
            r.get("aggressive_horizon"),
            r.get("conservative_rating"),
            r.get("conservative_pt"),
            r.get("conservative_upside_pct"),
            r.get("conservative_horizon"),
            r.get("pt_compression_pct"),
            cls,
            _classification_to_tier(r),
            r.get("aggressive_executive_summary"),
        ))
    conn.executemany(
        """INSERT INTO matrix_picks
           (run_id, rank, ticker, name, sector_sic, market_cap, composite_score,
            current_price, aggressive_rating, aggressive_pt, aggressive_upside_pct,
            aggressive_horizon, conservative_rating, conservative_pt,
            conservative_upside_pct, conservative_horizon, pt_compression_pct,
            classification, tier, aggressive_executive_summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        matrix_rows,
    )

    options_indexed = False
    if options_path.exists():
        index_options_overlay(conn, run_dir.name, options_path)
        options_indexed = True

    return (True, options_indexed)


def index_options_overlay(conn: sqlite3.Connection, run_id: str, overlay_path: Path) -> int:
    data = json.loads(overlay_path.read_text())
    overlays = data.get("overlays") or []
    strategy_mode = data.get("strategy_mode") or "tier-driven"

    conn.execute("DELETE FROM options_legs WHERE run_id=?", (run_id,))
    leg_rows = []
    for o in overlays:
        if "strategy" not in o or "error" in o:
            continue
        legs = o.get("legs") or []
        warns = json.dumps(o.get("liquidity_warnings") or [])
        for i, leg in enumerate(legs):
            leg_rows.append((
                run_id,
                o.get("ticker"),
                strategy_mode,
                o.get("strategy"),
                o.get("tier"),
                o.get("expiration"),
                o.get("dte"),
                o.get("horizon"),
                i,
                leg.get("side"),
                leg.get("type"),
                leg.get("strike"),
                leg.get("open_interest"),
                leg.get("iv"),
                leg.get("delta"),
                leg.get("price"),
                leg.get("price_source"),
                o.get("net_debit_per_share"),
                o.get("net_debit_per_contract"),
                o.get("breakeven_underlying"),
                o.get("breakeven_pct_from_current"),
                o.get("risk_reward"),
                o.get("max_profit_per_contract"),
                o.get("max_loss_per_contract"),
                o.get("upside_to_short_strike_pct"),
                o.get("long_call_delta"),
                o.get("long_call_moneyness"),
                o.get("approx_gain_at_aggr_pt_per_contract"),
                o.get("approx_gain_at_cons_pt_per_contract"),
                warns,
            ))
    if leg_rows:
        conn.executemany(
            """INSERT INTO options_legs
               (run_id, ticker, strategy_mode, strategy, tier, expiration, dte, horizon,
                leg_index, leg_side, leg_type, leg_strike, leg_open_interest, leg_iv,
                leg_delta, leg_price, leg_price_source, net_debit_per_share,
                net_debit_per_contract, breakeven_underlying, breakeven_pct_from_current,
                risk_reward, max_profit_per_contract, max_loss_per_contract,
                upside_to_short_strike_pct, long_call_delta, long_call_moneyness,
                approx_gain_at_aggr_pt_per_contract, approx_gain_at_cons_pt_per_contract,
                liquidity_warnings)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            leg_rows,
        )
    return len(leg_rows)


def print_summary(conn: sqlite3.Connection) -> None:
    print("\n=== runs/index.db summary ===\n")
    for row in conn.execute(
        "SELECT run_type, COUNT(*) FROM runs GROUP BY run_type ORDER BY run_type"
    ):
        print(f"  {row[0]:<10} {row[1]} run(s)")

    n_screener = conn.execute("SELECT COUNT(*) FROM screener_picks").fetchone()[0]
    n_matrix = conn.execute("SELECT COUNT(*) FROM matrix_picks").fetchone()[0]
    n_options = conn.execute("SELECT COUNT(*) FROM options_legs").fetchone()[0]
    print(f"\n  screener_picks: {n_screener:>5} rows")
    print(f"  matrix_picks:   {n_matrix:>5} rows")
    print(f"  options_legs:   {n_options:>5} rows")

    print("\n--- Tickers appearing in most matrix runs (PICKs only) ---")
    for row in conn.execute(
        """SELECT ticker, COUNT(DISTINCT run_id) n_runs,
                  GROUP_CONCAT(DISTINCT tier) tiers
           FROM matrix_picks
           WHERE classification = 'PICK'
           GROUP BY ticker
           HAVING n_runs > 1
           ORDER BY n_runs DESC, ticker
           LIMIT 10"""
    ):
        print(f"  {row[0]:<6}  {row[1]} runs  (tiers: {row[2]})")

    n_lc = conn.execute(
        "SELECT COUNT(*) FROM options_legs WHERE strategy_mode='long-call'"
    ).fetchone()[0]
    if n_lc:
        print("\n--- Long-call ROI distribution by tier (across all indexed runs) ---")
        for row in conn.execute(
            """SELECT tier, COUNT(*) n,
                      ROUND(AVG(approx_gain_at_aggr_pt_per_contract),0) avg_gain,
                      ROUND(AVG(net_debit_per_contract),0) avg_debit
               FROM options_legs
               WHERE strategy_mode='long-call' AND leg_index=0
               GROUP BY tier
               ORDER BY tier"""
        ):
            tier, n, gain, debit = row
            roi = (gain / debit * 100) if debit else None
            roi_str = f"{roi:+.0f}%" if roi is not None else "—"
            print(f"  Tier {tier or '—'}: n={n:>2}  avg gain @ aggr PT ${gain:>+7.0f}  "
                  f"avg debit ${debit:>6.0f}/ct  ROI {roi_str}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs-dir", default="runs",
                   help="Directory containing run subdirectories (default: runs)")
    p.add_argument("--db", default="runs/index.db",
                   help="SQLite catalog path (default: runs/index.db)")
    p.add_argument("--force", action="store_true",
                   help="Re-index even if source mtime is unchanged")
    p.add_argument("--query", action="store_true",
                   help="Print summary stats after indexing")
    args = p.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_dir():
        print(f"❌ runs dir not found: {runs_dir}", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")

    indexed = {"screener": 0, "matrix": 0, "options": 0, "skipped": 0}
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        run_type = _classify_run(run_dir)
        if run_type is None:
            continue
        try:
            if run_type == "screener":
                if index_screener_run(conn, run_dir, run_dir / "screener.json", args.force):
                    indexed["screener"] += 1
                else:
                    indexed["skipped"] += 1
            elif run_type == "matrix":
                m_done, o_done = index_matrix_run(
                    conn, run_dir, run_dir / "verdict_ledger.json", args.force
                )
                if m_done:
                    indexed["matrix"] += 1
                if o_done:
                    indexed["options"] += 1
                if not m_done and not o_done:
                    indexed["skipped"] += 1
        except Exception as e:
            print(f"⚠ {run_dir.name}: {e}", file=sys.stderr)
            continue

    conn.commit()

    print(f"✅ Indexed: {indexed['screener']} screener · {indexed['matrix']} matrix "
          f"· {indexed['options']} options-overlay · {indexed['skipped']} unchanged")
    print(f"   → {db_path}")

    if args.query:
        print_summary(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
