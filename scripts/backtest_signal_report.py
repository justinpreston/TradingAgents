#!/usr/bin/env python3
"""Build a signal-vs-outcome correlation report from a realized backtest.

Pure file transform, no network. Reads a realized-backtest JSON (e.g.
``runs/backtest_exits_2026-06-26.json``) plus the derived SQLite catalog
(``runs/index.db``), joins backtest rows to ``matrix_picks`` and
``chronos_forecasts`` on ``(run_id, ticker)``, and emits a markdown report
plus a machine-readable JSON twin summarizing:

1. Pearson/Spearman correlations of pre-trade signals vs realized ROI.
2. Bucket cross-tabs (market cap, composite-score terciles, Chronos
   quantile, Chronos model view, sector, tenor, recurrence).
3. A veto-rate-by-week table sourced from ``runs`` (run_type='matrix').

``runs/index.db`` is opened **read-only** (``mode=ro`` URI) — this script
never writes to the catalog. See CLAUDE.md's "Don'ts" for why.

Example::

    .venv/bin/python scripts/backtest_signal_report.py \\
        --backtest runs/backtest_exits_2026-06-26.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DB = REPO_ROOT / "runs" / "index.db"
SCHEMA_VERSION = 1

# Signals correlated against roi_hold / roi_exit_aggr.
# Each entry: (label, source) where source is "matrix" or "chronos" or "backtest".
CORRELATION_SIGNALS: list[tuple[str, str]] = [
    ("aggressive_upside_pct", "matrix"),
    ("pt_compression_pct", "matrix"),
    ("composite_score", "matrix"),
    ("agent_pt_quantile", "chronos"),
    ("forecast_pct_p50", "chronos"),
    ("market_cap", "matrix"),
]

OUTCOME_FIELDS = ["roi_hold", "roi_exit_aggr"]

MIN_CORRELATION_N = 8
MIN_BUCKET_N = 4
MIN_SECTOR_N = 5

CAVEATS = [
    "Single measurement date — all realized ROI figures are snapshotted "
    "as of the backtest's `measure_date`, not tracked to each option's "
    "actual expiration or a rolling exit.",
    "Small bucket sizes — several cross-tabs rest on n well under 30; "
    "treat splits as directional, not statistically confirmed.",
    "Options ROI medians are noisy — single-name option premiums swing "
    "on IV crush/expansion independent of the underlying thesis playing out.",
    "index.db signal columns (aggressive_upside_pct, pt_compression_pct, "
    "agent_pt_quantile, forecast_pct_p50, composite_score) are modeled "
    "estimates from the matrix/Chronos run, not realized outcomes.",
]


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _read_backtest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    if "results" not in data:
        raise ValueError(f"{path} is missing a 'results' key")
    return data


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite catalog read-only. Never writes to index.db."""
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Statistics helpers (stdlib only)
# ---------------------------------------------------------------------------


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _rank(vals: list[float]) -> list[float]:
    """Average (fractional) ranks, ties get the mean rank."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return _pearson(_rank(xs), _rank(ys))


def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _rate_pct(flags: list[bool]) -> float | None:
    if not flags:
        return None
    return 100.0 * sum(1 for f in flags if f) / len(flags)


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


def _join_rows(
    results: list[dict[str, Any]], con: sqlite3.Connection
) -> list[dict[str, Any]]:
    """LEFT-join each backtest row (with non-null roi_hold) to matrix_picks
    and chronos_forecasts on (run_id, ticker). Rows without a catalog match
    are kept (matrix/chronos fields are None)."""
    joined = []
    for row in results:
        if row.get("roi_hold") is None:
            continue
        run_id = row.get("run_id")
        ticker = row.get("ticker")
        mp = con.execute(
            "SELECT * FROM matrix_picks WHERE run_id = ? AND ticker = ?",
            (run_id, ticker),
        ).fetchone()
        cf = con.execute(
            "SELECT * FROM chronos_forecasts WHERE run_id = ? AND ticker = ?",
            (run_id, ticker),
        ).fetchone()
        joined.append(
            {
                "backtest": row,
                "matrix": dict(mp) if mp else None,
                "chronos": dict(cf) if cf else None,
            }
        )
    return joined


def _signal_value(rec: dict[str, Any], signal: str, source: str) -> float | None:
    catalog = rec.get(source)
    if not catalog:
        return None
    val = catalog.get(signal)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _outcome_value(rec: dict[str, Any], field: str) -> float | None:
    val = rec["backtest"].get(field)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Correlations
# ---------------------------------------------------------------------------


def compute_correlations(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for signal, source in CORRELATION_SIGNALS:
        for outcome in OUTCOME_FIELDS:
            xs: list[float] = []
            ys: list[float] = []
            for rec in joined:
                sv = _signal_value(rec, signal, source)
                ov = _outcome_value(rec, outcome)
                if sv is None or ov is None:
                    continue
                xs.append(sv)
                ys.append(ov)
            n = len(xs)
            if n < MIN_CORRELATION_N or len(set(xs)) < 2 or len(set(ys)) < 2:
                cells.append(
                    {
                        "signal": signal,
                        "outcome": outcome,
                        "n": n,
                        "pearson": None,
                        "spearman": None,
                        "skipped": True,
                    }
                )
                continue
            cells.append(
                {
                    "signal": signal,
                    "outcome": outcome,
                    "n": n,
                    "pearson": _pearson(xs, ys),
                    "spearman": _spearman(xs, ys),
                    "skipped": False,
                }
            )
    return cells


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def _bucket_stats(label: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    n = len(rows)
    if n < MIN_BUCKET_N:
        return None
    holds = [
        _outcome_value(r, "roi_hold")
        for r in rows
        if _outcome_value(r, "roi_hold") is not None
    ]
    exits = [
        _outcome_value(r, "roi_exit_aggr")
        for r in rows
        if _outcome_value(r, "roi_exit_aggr") is not None
    ]
    hits = [
        r["backtest"].get("aggr_pt_hit")
        for r in rows
        if r["backtest"].get("aggr_pt_hit") is not None
    ]
    return {
        "bucket": label,
        "n": n,
        "median_roi_hold": _median(holds),
        "median_roi_exit_aggr": _median(exits),
        "win_rate_pct": (100.0 * sum(1 for v in holds if v > 0) / len(holds))
        if holds
        else None,
        "aggr_pt_hit_rate_pct": _rate_pct(hits),
    }


def _cap_bucket(mcap: float | None) -> str | None:
    if mcap is None:
        return None
    if mcap < 10e9:
        return "mid (<10B)"
    if mcap <= 200e9:
        return "large (10-200B)"
    return "mega (>200B)"


def bucket_market_cap(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ["mid (<10B)", "large (10-200B)", "mega (>200B)"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        mcap = _signal_value(rec, "market_cap", "matrix")
        b = _cap_bucket(mcap)
        if b is None:
            continue
        groups.setdefault(b, []).append(rec)
    out = []
    for label in order:
        stats = _bucket_stats(label, groups.get(label, []))
        if stats:
            out.append(stats)
    return out


def bucket_composite_score_terciles(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [
        (rec, _signal_value(rec, "composite_score", "matrix"))
        for rec in joined
    ]
    scored = [(rec, v) for rec, v in scored if v is not None]
    if len(scored) < 3:
        return []
    scored.sort(key=lambda pair: pair[1])
    n = len(scored)
    third = n / 3.0
    groups: dict[str, list[dict[str, Any]]] = {
        "low": [],
        "mid": [],
        "high": [],
    }
    for idx, (rec, _val) in enumerate(scored):
        if idx < third:
            groups["low"].append(rec)
        elif idx < 2 * third:
            groups["mid"].append(rec)
        else:
            groups["high"].append(rec)
    out = []
    for label in ["low", "mid", "high"]:
        stats = _bucket_stats(f"composite_score tercile: {label}", groups[label])
        if stats:
            out.append(stats)
    return out


def _quantile_bucket(q: float | None) -> str | None:
    if q is None:
        return None
    if q < 0.6:
        return "q<0.6"
    if q <= 0.8:
        return "q0.6-0.8"
    return "q>0.8"


def bucket_chronos_quantile(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ["q<0.6", "q0.6-0.8", "q>0.8"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        q = _signal_value(rec, "agent_pt_quantile", "chronos")
        b = _quantile_bucket(q)
        if b is None:
            continue
        groups.setdefault(b, []).append(rec)
    out = []
    for label in order:
        stats = _bucket_stats(label, groups.get(label, []))
        if stats:
            out.append(stats)
    return out


def bucket_chronos_model_view(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        chronos = rec.get("chronos")
        view = chronos.get("model_view") if chronos else None
        if not view:
            continue
        groups.setdefault(str(view), []).append(rec)
    out = []
    for label in sorted(groups):
        stats = _bucket_stats(label, groups[label])
        if stats:
            out.append(stats)
    return out


def bucket_sector(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        matrix = rec.get("matrix")
        sector = matrix.get("sector_sic") if matrix else None
        if not sector:
            continue
        groups.setdefault(str(sector), []).append(rec)
    out = []
    for label in sorted(groups):
        rows = groups[label]
        if len(rows) < MIN_SECTOR_N:
            continue
        stats = _bucket_stats(label, rows)
        if stats:
            out.append(stats)
    return out


def _parse_date(s: str) -> date | None:
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _tenor_bucket(rec: dict[str, Any]) -> str | None:
    backtest = rec["backtest"]
    exp = backtest.get("expiration")
    rec_date = backtest.get("rec_date")
    if not exp or not rec_date:
        return None
    exp_d = _parse_date(exp)
    rec_d = _parse_date(rec_date)
    if exp_d is None or rec_d is None:
        return None
    dte = (exp_d - rec_d).days
    if dte < 90:
        return "<90d"
    if dte <= 180:
        return "90-180d"
    return ">180d"


def bucket_tenor(joined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ["<90d", "90-180d", ">180d"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        b = _tenor_bucket(rec)
        if b is None:
            continue
        groups.setdefault(b, []).append(rec)
    out = []
    for label in order:
        stats = _bucket_stats(label, groups.get(label, []))
        if stats:
            out.append(stats)
    return out


def _recurrence_counts(con: sqlite3.Connection, tickers: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ticker in tickers:
        row = con.execute(
            "SELECT COUNT(DISTINCT run_id) AS c FROM matrix_picks "
            "WHERE ticker = ? AND classification = 'PICK'",
            (ticker,),
        ).fetchone()
        counts[ticker] = row["c"] if row else 0
    return counts


def _recurrence_bucket(count: int) -> str:
    if count <= 1:
        return "seen once"
    if count == 2:
        return "seen 2x"
    return "seen 3+"


def bucket_recurrence(
    joined: list[dict[str, Any]], con: sqlite3.Connection
) -> list[dict[str, Any]]:
    tickers = {rec["backtest"]["ticker"] for rec in joined}
    counts = _recurrence_counts(con, tickers)
    order = ["seen once", "seen 2x", "seen 3+"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in joined:
        ticker = rec["backtest"]["ticker"]
        b = _recurrence_bucket(counts.get(ticker, 0))
        groups.setdefault(b, []).append(rec)
    out = []
    for label in order:
        stats = _bucket_stats(label, groups.get(label, []))
        if stats:
            out.append(stats)
    return out


# ---------------------------------------------------------------------------
# Veto rate by week
# ---------------------------------------------------------------------------


def veto_rate_by_week(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT COALESCE(snapshot_date, substr(generated_at, 1, 10)) AS week,
               SUM(n_picks) AS n_picks,
               SUM(n_picks + n_vetoed) AS n_total
        FROM runs
        WHERE run_type = 'matrix'
        GROUP BY week
        ORDER BY week
        """
    ).fetchall()
    out = []
    for row in rows:
        n_picks = row["n_picks"] or 0
        n_total = row["n_total"] or 0
        n_vetoed = n_total - n_picks
        veto_pct = (100.0 * n_vetoed / n_total) if n_total else None
        out.append(
            {
                "week": row["week"],
                "n_picks": n_picks,
                "n_vetoed": n_vetoed,
                "n_total": n_total,
                "veto_pct": veto_pct,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_report(
    backtest_path: Path,
    db_path: Path,
) -> dict[str, Any]:
    backtest_doc = _read_backtest(backtest_path)
    results = backtest_doc.get("results") or []
    non_null = [r for r in results if r.get("roi_hold") is not None]

    con = _connect_readonly(db_path)
    try:
        joined = _join_rows(results, con)
        correlations = compute_correlations(joined)
        buckets = {
            "market_cap": bucket_market_cap(joined),
            "composite_score_tercile": bucket_composite_score_terciles(joined),
            "chronos_quantile": bucket_chronos_quantile(joined),
            "chronos_model_view": bucket_chronos_model_view(joined),
            "sector": bucket_sector(joined),
            "tenor": bucket_tenor(joined),
            "recurrence": bucket_recurrence(joined, con),
        }
        veto_by_week = veto_rate_by_week(con)
    finally:
        con.close()

    n_matrix_matched = sum(1 for rec in joined if rec["matrix"] is not None)
    n_chronos_matched = sum(1 for rec in joined if rec["chronos"] is not None)

    return {
        "schema_version": SCHEMA_VERSION,
        "source_backtest": str(backtest_path),
        "source_db": str(db_path),
        "measure_date": backtest_doc.get("measure_date"),
        "n_backtest_rows": len(results),
        "n_rows_with_roi_hold": len(non_null),
        "n_matrix_matched": n_matrix_matched,
        "n_chronos_matched": n_chronos_matched,
        "correlations": correlations,
        "buckets": buckets,
        "veto_by_week": veto_by_week,
        "caveats": CAVEATS,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_pct(val: float | None) -> str:
    """Render a raw ROI value already in percent units (13.8 -> "+13.8%",
    not "1380%") -- the backtest JSON's roi_* fields are percent points,
    so no /100 division happens here."""
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"


def _fmt_ratio_pct(val: float | None) -> str:
    """Render a 0-100 percentage value (win rate, hit rate, veto rate)."""
    if val is None:
        return "—"
    return f"{val:.0f}%"


def _fmt_corr(val: float | None) -> str:
    if val is None:
        return "—"
    return f"{val:+.2f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Backtest Signal Report")
    lines.append("")
    lines.append(f"- Source backtest: `{report['source_backtest']}`")
    lines.append(f"- Source catalog: `{report['source_db']}`")
    lines.append(f"- Measure date: `{report['measure_date']}`")
    lines.append(
        f"- Rows with realized `roi_hold`: {report['n_rows_with_roi_hold']} "
        f"(of {report['n_backtest_rows']} total)"
    )
    lines.append(
        f"- Matched to `matrix_picks`: {report['n_matrix_matched']}; "
        f"matched to `chronos_forecasts`: {report['n_chronos_matched']}"
    )
    lines.append("")

    lines.append("## Signal correlations")
    lines.append("")
    lines.append("Pearson / Spearman correlation of each pre-trade signal vs")
    lines.append("realized ROI. Cells with n < 8 or zero variance are skipped.")
    lines.append("")
    lines.append("| Signal | Outcome | n | Pearson | Spearman |")
    lines.append("|---|---|---:|---:|---:|")
    for cell in report["correlations"]:
        if cell.get("skipped"):
            continue
        lines.append(
            f"| {cell['signal']} | {cell['outcome']} | {cell['n']} | "
            f"{_fmt_corr(cell['pearson'])} | {_fmt_corr(cell['spearman'])} |"
        )
    skipped = [c for c in report["correlations"] if c.get("skipped")]
    if skipped:
        lines.append("")
        lines.append(
            "Skipped (n < 8 or zero variance): "
            + ", ".join(f"{c['signal']} vs {c['outcome']} (n={c['n']})" for c in skipped)
        )
    lines.append("")

    bucket_titles = {
        "market_cap": "Market cap",
        "composite_score_tercile": "Screener composite score terciles",
        "chronos_quantile": "Chronos agent PT quantile",
        "chronos_model_view": "Chronos model view",
        "sector": "Sector (sector_sic)",
        "tenor": "Tenor (DTE at rec_date)",
        "recurrence": "Recurrence (distinct PICK appearances)",
    }
    for key, title in bucket_titles.items():
        rows = report["buckets"].get(key) or []
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("_No buckets met the minimum-n threshold._")
            lines.append("")
            continue
        lines.append(
            "| Bucket | n | Median roi_hold | Median roi_exit_aggr | Win rate | Aggr PT hit rate |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                f"| {row['bucket']} | {row['n']} | "
                f"{_fmt_pct(row['median_roi_hold'])} | "
                f"{_fmt_pct(row['median_roi_exit_aggr'])} | "
                f"{_fmt_ratio_pct(row['win_rate_pct'])} | "
                f"{_fmt_ratio_pct(row['aggr_pt_hit_rate_pct'])} |"
            )
        lines.append("")

    lines.append("## Veto rate by week")
    lines.append("")
    lines.append("| Week | Picks | Vetoed | Total | Veto % |")
    lines.append("|---|---:|---:|---:|---:|")
    for row in report["veto_by_week"]:
        lines.append(
            f"| {row['week']} | {row['n_picks']} | {row['n_vetoed']} | "
            f"{row['n_total']} | {_fmt_ratio_pct(row['veto_pct'])} |"
        )
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    for caveat in report["caveats"]:
        lines.append(f"- {caveat}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_output_paths(backtest_path: Path) -> tuple[Path, Path]:
    """Markdown defaults to backtest_signal_report.md next to the backtest
    JSON; JSON output defaults to the same stem with a .json extension."""
    md_default = backtest_path.parent / "backtest_signal_report.md"
    json_default = backtest_path.parent / "backtest_signal_report.json"
    return md_default, json_default


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", type=Path, required=True, help="Realized backtest JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Read-only SQLite catalog")
    parser.add_argument("--output", type=Path, default=None, help="Markdown report output path")
    parser.add_argument(
        "--json-output", type=Path, default=None, help="JSON report output path"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    backtest_path = args.backtest
    md_default, json_default = _default_output_paths(backtest_path)
    output_path = args.output or md_default
    json_output_path = args.json_output or json_default

    report = build_report(backtest_path, args.db)
    markdown = render_markdown(report)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    json_output_path.parent.mkdir(parents=True, exist_ok=True)
    json_output_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Wrote {json_output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
