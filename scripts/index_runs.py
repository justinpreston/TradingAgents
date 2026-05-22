"""Index TradingAgents run artifacts into a SQLite catalog for cross-run queries.

This script walks `runs/` and indexes screener runs, matrix runs, and options
overlays into a single SQLite database (`runs/index.db` by default). The raw
files (verdict_ledger.json, exec summaries, options_overlay.{md,json}) remain
the source of truth — the SQLite catalog is purely derived/regenerable.

Idempotent: re-running on the same data UPDATEs in place. Re-running on a
file with unchanged mtime is skipped unless --force is passed.

Schema:
- runs:               one row per indexed run directory (any type)
- screener_picks:     one row per ranked ticker per screener run
- matrix_picks:       one row per ranked ticker per matrix run
- options_legs:       one row per option leg per matrix run
- chronos_forecasts:  one row per ticker per matrix run (zero-shot model
                      forecast cross-check on the persona PTs)

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

  -- Tier A picks where Chronos disagrees with the personas:
  SELECT run_id, ticker, current_price, agent_aggressive_pt,
         forecast_p50, agent_pt_vs_median_pct, model_view
  FROM chronos_forecasts
  WHERE tier='A' AND classification='PICK'
    AND model_view IN ('high', 'above cone')
  ORDER BY agent_pt_vs_median_pct DESC;
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

CREATE TABLE IF NOT EXISTS chronos_forecasts (
    run_id                          TEXT NOT NULL,
    ticker                          TEXT NOT NULL,
    tier                            TEXT,
    classification                  TEXT,
    current_price                   REAL,
    agent_aggressive_pt             REAL,
    agent_conservative_pt           REAL,
    agent_horizon                   TEXT,
    prediction_length_days          INTEGER,
    context_bars_used               INTEGER,
    forecast_p10                    REAL,
    forecast_p50                    REAL,
    forecast_p90                    REAL,
    forecast_pct_p10                REAL,
    forecast_pct_p50                REAL,
    forecast_pct_p90                REAL,
    agent_pt_quantile               REAL,
    agent_pt_vs_median_pct          REAL,
    model_view                      TEXT,
    agent_inside_cone               INTEGER,
    PRIMARY KEY (run_id, ticker),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chronos_ticker ON chronos_forecasts(ticker);
CREATE INDEX IF NOT EXISTS idx_chronos_view ON chronos_forecasts(model_view);
CREATE INDEX IF NOT EXISTS idx_chronos_tier ON chronos_forecasts(tier);

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
    """Return run_type or None if unindexable.

    Detection prefers content-based markers over name prefixes so user-defined
    run-ids (e.g. ``weekly_<ts>_chain`` or ``matrix_pipeline_test_<ts>``) are
    indexed correctly.
    """
    if (run_dir / "screener.json").exists():
        return "screener"
    if (run_dir / "verdict_ledger.json").exists():
        return "matrix"
    if run_dir.name.startswith("cross_run_"):
        return None  # Synthesized from other runs; nothing new to index here
    return None


def _classification_to_tier(row: dict) -> str | None:
    """Derive A/B/C tier from a verdict_ledger row, matching the same
    convention used by build_options_overlay._tier().

    Grounded-pipeline guardrail: a PICK with any ``pt_quality_flags`` is
    capped at Tier B regardless of compression — a SUSPECT PT (>50% from
    current price, likely hallucinated) cannot drive a Tier A allocation.
    """
    cls = (row.get("classification") or "").strip()
    if cls == "VETOED":
        return "VETO"
    if cls != "PICK":
        return None
    comp = row.get("pt_compression_pct")
    if row.get("conservative_pt") is None:
        return "C"
    suspect_flags = row.get("pt_quality_flags") or []
    if comp is not None and comp < 5.0 and not suspect_flags:
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
                     ledger_path: Path, force: bool) -> tuple[bool, bool, bool]:
    """Returns (matrix_indexed, options_indexed, chronos_indexed)."""
    mtime = ledger_path.stat().st_mtime
    options_path = run_dir / "options_overlay.json"
    options_mtime = options_path.stat().st_mtime if options_path.exists() else None
    chronos_path = run_dir / "chronos_overlay.json"
    chronos_mtime = chronos_path.stat().st_mtime if chronos_path.exists() else None
    candidates = [m for m in (mtime, options_mtime, chronos_mtime) if m is not None]
    combined_mtime = max(candidates) if candidates else mtime

    cur = conn.execute("SELECT source_mtime FROM runs WHERE run_id=?", (run_dir.name,))
    row = cur.fetchone()
    if row and row[0] and row[0] >= combined_mtime and not force:
        return (False, False, False)

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

    chronos_indexed = False
    if chronos_path.exists():
        index_chronos_overlay(conn, run_dir.name, chronos_path)
        chronos_indexed = True

    return (True, options_indexed, chronos_indexed)


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


def index_chronos_overlay(conn: sqlite3.Connection, run_id: str, overlay_path: Path) -> int:
    """Index a chronos_overlay.json file produced by build_chronos_overlay.py.

    Each row mirrors the JSON schema documented in build_chronos_overlay.py:
    one row per ticker per run, capturing the model's quantile forecast at
    the configured horizon plus the agent PT's quantile within that
    distribution (the "agreement" signal).
    """
    data = json.loads(overlay_path.read_text())
    overlays = data.get("overlays") or []

    conn.execute("DELETE FROM chronos_forecasts WHERE run_id=?", (run_id,))
    rows = []
    for o in overlays:
        if "error" in o:
            continue
        forecast = o.get("forecast_at_horizon") or {}
        forecast_pct = o.get("forecast_pct_change_at_horizon") or {}
        rows.append((
            run_id,
            o.get("ticker"),
            o.get("tier"),
            o.get("classification"),
            o.get("current_price"),
            o.get("agent_aggressive_pt"),
            o.get("agent_conservative_pt"),
            o.get("agent_horizon"),
            o.get("prediction_length_days"),
            o.get("context_bars_used"),
            forecast.get("p10"),
            forecast.get("p50"),
            forecast.get("p90"),
            forecast_pct.get("p10_pct"),
            forecast_pct.get("p50_pct"),
            forecast_pct.get("p90_pct"),
            o.get("agent_pt_quantile"),
            o.get("agent_pt_vs_median_pct"),
            o.get("model_view"),
            1 if o.get("agent_inside_cone") else 0,
        ))
    if rows:
        conn.executemany(
            """INSERT INTO chronos_forecasts
               (run_id, ticker, tier, classification, current_price,
                agent_aggressive_pt, agent_conservative_pt, agent_horizon,
                prediction_length_days, context_bars_used,
                forecast_p10, forecast_p50, forecast_p90,
                forecast_pct_p10, forecast_pct_p50, forecast_pct_p90,
                agent_pt_quantile, agent_pt_vs_median_pct, model_view,
                agent_inside_cone)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return len(rows)


def print_summary(conn: sqlite3.Connection) -> None:
    print("\n=== runs/index.db summary ===\n")
    for row in conn.execute(
        "SELECT run_type, COUNT(*) FROM runs GROUP BY run_type ORDER BY run_type"
    ):
        print(f"  {row[0]:<10} {row[1]} run(s)")

    n_screener = conn.execute("SELECT COUNT(*) FROM screener_picks").fetchone()[0]
    n_matrix = conn.execute("SELECT COUNT(*) FROM matrix_picks").fetchone()[0]
    n_options = conn.execute("SELECT COUNT(*) FROM options_legs").fetchone()[0]
    n_chronos = conn.execute("SELECT COUNT(*) FROM chronos_forecasts").fetchone()[0]
    print(f"\n  screener_picks:    {n_screener:>5} rows")
    print(f"  matrix_picks:      {n_matrix:>5} rows")
    print(f"  options_legs:      {n_options:>5} rows")
    print(f"  chronos_forecasts: {n_chronos:>5} rows")

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

    if n_chronos:
        print("\n--- Agent PT vs Chronos forecast (across all indexed matrix runs) ---")
        # By tier: how often does the agent's aggressive PT land inside the
        # model's p10-p90 cone, and how does the median agent vs model
        # disagreement skew?
        for row in conn.execute(
            """SELECT tier,
                      COUNT(*) n,
                      ROUND(100.0 * AVG(agent_inside_cone), 0) inside_pct,
                      ROUND(AVG(agent_pt_quantile), 2) avg_quantile,
                      ROUND(AVG(agent_pt_vs_median_pct), 1) avg_vs_median_pct,
                      SUM(CASE WHEN model_view='above cone' THEN 1 ELSE 0 END) above,
                      SUM(CASE WHEN model_view='high'       THEN 1 ELSE 0 END) high_n,
                      SUM(CASE WHEN model_view='inside'     THEN 1 ELSE 0 END) inside_n,
                      SUM(CASE WHEN model_view='low'        THEN 1 ELSE 0 END) low_n,
                      SUM(CASE WHEN model_view='below cone' THEN 1 ELSE 0 END) below
               FROM chronos_forecasts
               WHERE classification='PICK' AND tier IN ('A','B','C')
               GROUP BY tier
               ORDER BY tier"""
        ):
            tier, n, inside_pct, avg_q, avg_v, above, high_n, inside_n, low_n, below = row
            avg_q_str = f"p{int(round(avg_q*100)):>2}" if avg_q is not None else " —"
            print(f"  Tier {tier}: n={n:>2}  inside-cone {inside_pct or 0:>3.0f}%  "
                  f"avg agent PT @ {avg_q_str}  ({avg_v or 0:+.1f}% vs model p50)")
            print(f"          views: above={above} high={high_n} inside={inside_n} "
                  f"low={low_n} below={below}")


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

    indexed = {"screener": 0, "matrix": 0, "options": 0, "chronos": 0, "skipped": 0}
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
                m_done, o_done, c_done = index_matrix_run(
                    conn, run_dir, run_dir / "verdict_ledger.json", args.force
                )
                if m_done:
                    indexed["matrix"] += 1
                if o_done:
                    indexed["options"] += 1
                if c_done:
                    indexed["chronos"] += 1
                if not (m_done or o_done or c_done):
                    indexed["skipped"] += 1
        except Exception as e:
            print(f"⚠ {run_dir.name}: {e}", file=sys.stderr)
            continue

    conn.commit()

    print(f"✅ Indexed: {indexed['screener']} screener · {indexed['matrix']} matrix "
          f"· {indexed['options']} options · {indexed['chronos']} chronos "
          f"· {indexed['skipped']} unchanged")
    print(f"   → {db_path}")

    if args.query:
        print_summary(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
