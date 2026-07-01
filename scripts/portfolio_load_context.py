"""Load and join portfolio positions with the latest matrix run output.

This is the canonical context loader for the tradingagents-portfolio-advisor
skill. Run it on the first turn of any portfolio-shaped conversation to pin
the agent's working set. It is intentionally read-only and deterministic
(no network calls — it reuses the matrix run's already-fetched current_prices.json).

Usage::

    .venv/bin/python scripts/portfolio_load_context.py [--json] [--matrix-run RUN]
    .venv/bin/python scripts/portfolio_load_context.py --validate

Outputs a structured "context blob" combining:
- The user's positions (runs/portfolio/positions.json)
- Their policy (runs/portfolio/policy.json)
- The most recent matrix run's verdict ledger + options overlay + prices
- Cross-run tier history per held ticker (from runs/index.db)
- Computed flags (stop-loss breaches, DTE imminent, tier demoted, etc.)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tradingagents.tiers import tier_for_row, tier_rank

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_DIR = REPO_ROOT / "runs" / "portfolio"
RUNS_DIR = REPO_ROOT / "runs"
INDEX_DB = REPO_ROOT / "runs" / "index.db"

DEFAULT_POSITIONS = PORTFOLIO_DIR / "positions.json"
DEFAULT_POLICY = PORTFOLIO_DIR / "policy.json"
DEFAULT_TRADES_LOG = PORTFOLIO_DIR / "trades_log.jsonl"

VALID_INSTRUMENTS = {
    "equity",
    "long_call",
    "long_put",
    "short_call",
    "short_put",
    "vertical_call_spread",
    "vertical_put_spread",
    "covered_call",
    "cash_secured_put",
}


# ---------------------------------------------------------------------------
# Data loaders


def _strip_help_keys(obj: Any) -> Any:
    """Recursively drop keys starting with underscore (schema/help annotations)."""
    if isinstance(obj, dict):
        return {k: _strip_help_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_help_keys(v) for v in obj]
    return obj


def load_positions(path: Path = DEFAULT_POSITIONS) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"positions file not found: {path}")
    raw = json.loads(path.read_text())
    return _strip_help_keys(raw)


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"policy file not found: {path}")
    raw = json.loads(path.read_text())
    return _strip_help_keys(raw)


def load_trades_log(path: Path = DEFAULT_TRADES_LOG) -> list[dict[str, Any]]:
    """Load the append-only trades log. Returns [] if the file is missing or empty.

    Each line is a JSON object with shape::

        {
          "ts": "2026-05-09T17:00:00Z",
          "action": "TRIM",            # OPEN / CLOSE / TRIM / ROLL / ADD / NOTE
          "ticker": "VIRT",
          "id": "VIRT-2026-04-25-c35", # position id, may be null for OPEN
          "qty": 2,
          "premium_per_share": 5.49,
          "underlying": 51.31,
          "notes": "Took half profits"
        }
    """
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def recent_actions_for_position(
    log_entries: list[dict[str, Any]],
    ticker: str,
    position_id: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return the most-recent matching log entries (newest first)."""
    matches = []
    for e in log_entries:
        if e.get("ticker") != ticker:
            continue
        if position_id and e.get("id") and e["id"] != position_id:
            continue
        matches.append(e)
    matches.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return matches[:limit]


def find_latest_matrix_run(runs_dir: Path = RUNS_DIR) -> Path | None:
    """Return the path to the most recent matrix run that has a verdict_ledger.json.

    Detection mirrors scripts/index_runs.py — we check for verdict_ledger.json
    rather than directory naming, so custom run-ids work.

    NOTE: this is mtime-based and kept only for back-compat / single-run
    callers. Prefer :func:`find_matrix_runs_for_latest_trade_date`, which is
    content-based (immune to a Saturday overlay re-run reshuffling "latest",
    and correctly unions the mid/large/mega tier runs that now all land the
    same Friday).
    """
    if not runs_dir.exists():
        return None
    candidates = [
        p for p in runs_dir.iterdir()
        if p.is_dir() and (p / "verdict_ledger.json").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _matrix_run_trade_date(run_path: Path) -> str | None:
    """Best-effort content-based trade date for a matrix run directory.

    Prefers manifest.json::trade_date (explicit, set at launch time), falls
    back to verdict_ledger.json::snapshot_date. Returns None if neither is
    present/parseable — such runs are excluded from trade-date grouping.
    """
    manifest_path = run_path / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}
        trade_date = manifest.get("trade_date")
        if trade_date:
            return str(trade_date)

    verdict_path = run_path / "verdict_ledger.json"
    if verdict_path.exists():
        try:
            verdict = json.loads(verdict_path.read_text())
        except json.JSONDecodeError:
            verdict = {}
        snapshot_date = verdict.get("snapshot_date")
        if snapshot_date:
            return str(snapshot_date)

    return None


def find_matrix_runs_for_latest_trade_date(runs_dir: Path | None = None) -> list[Path]:
    """Discover ALL matrix runs sharing the most recent trade date.

    Trade date is determined from run *content* (manifest.json::trade_date,
    falling back to verdict_ledger.json::snapshot_date) — NOT directory mtime.
    This is what makes multi-tier Fridays (mid/large/mega matrix runs, all
    dated the same trade_date) join correctly, and keeps a same-day overlay
    re-run (which bumps mtime but not trade_date) from reshuffling "latest".

    Runs without a resolvable trade date are excluded. Returns paths sorted
    by run_id for determinism (not by mtime).

    ``runs_dir`` defaults to the module-level :data:`RUNS_DIR`, resolved at
    call time (not def-time) so tests can monkeypatch it.
    """
    if runs_dir is None:
        runs_dir = RUNS_DIR
    if not runs_dir.exists():
        return []
    dated: list[tuple[str, Path]] = []
    for p in runs_dir.iterdir():
        if not p.is_dir() or not (p / "verdict_ledger.json").exists():
            continue
        trade_date = _matrix_run_trade_date(p)
        if trade_date is None:
            continue
        dated.append((trade_date, p))
    if not dated:
        return []
    latest_date = max(td for td, _ in dated)
    return sorted((p for td, p in dated if td == latest_date), key=lambda p: p.name)


def load_matrix_run(run_path: Path) -> dict[str, Any]:
    verdict_path = run_path / "verdict_ledger.json"
    if not verdict_path.exists():
        raise FileNotFoundError(f"verdict_ledger.json missing in {run_path}")
    verdict = json.loads(verdict_path.read_text())

    overlays = []
    overlay_path = run_path / "options_overlay.json"
    if overlay_path.exists():
        overlay_doc = json.loads(overlay_path.read_text())
        overlays = overlay_doc.get("overlays", [])

    prices = {}
    prices_path = run_path / "current_prices.json"
    if prices_path.exists():
        prices_doc = json.loads(prices_path.read_text())
        prices = prices_doc.get("prices_usd", {})

    return {
        "run_id": verdict.get("run_id", run_path.name),
        "run_path": str(run_path),
        "snapshot_date": verdict.get("snapshot_date"),
        "generated_at": verdict.get("generated_at"),
        "rows_by_ticker": {row["ticker"]: row for row in verdict.get("rows", [])},
        "overlays_by_ticker": {ov["ticker"]: ov for ov in overlays},
        "prices": prices,
    }


def _row_tier(row: dict[str, Any] | None) -> str | None:
    """Derive tier from a verdict_ledger row. Delegates to
    tradingagents.tiers.tier_for_row (canonical source of truth)."""
    if row is None:
        return None
    return tier_for_row(row, suspect_caps_a=False)


def load_matrix_runs(run_paths: list[Path]) -> dict[str, Any]:
    """Union multiple matrix runs into one matrix context, per-ticker deduped.

    On a ticker collision (the same ticker appears in more than one tier
    run — e.g. a name that straddled the mid/large mcap boundary and got
    screened twice), the row/overlay from the run whose *tier is higher*
    wins (rank ladder VETO=0, "—"/None=1, C=2, B=3, A=4 — mirrors
    ``_tier_rank``). Tier is read from the overlay's ``tier`` field when
    present (authoritative, written by build_options_overlay.py), else
    derived from the verdict_ledger row via ``_row_tier``.

    Prices are merged (later runs don't overwrite earlier ones unless the
    ticker collides and wins on tier, in which case its run's price wins
    too, keeping price/row/overlay consistent for that ticker).
    """
    if not run_paths:
        return {
            "run_id": None,
            "run_ids": [],
            "run_paths": [],
            "snapshot_date": None,
            "generated_at": None,
            "rows_by_ticker": {},
            "overlays_by_ticker": {},
            "prices": {},
        }

    loaded = [load_matrix_run(p) for p in run_paths]

    rows_by_ticker: dict[str, Any] = {}
    overlays_by_ticker: dict[str, Any] = {}
    prices: dict[str, Any] = {}
    winning_run_by_ticker: dict[str, str] = {}

    for run in loaded:
        run_id = run["run_id"]
        all_tickers = set(run["rows_by_ticker"]) | set(run["overlays_by_ticker"])
        for ticker in all_tickers:
            row = run["rows_by_ticker"].get(ticker)
            overlay = run["overlays_by_ticker"].get(ticker)

            candidate_tier = (overlay.get("tier") if overlay else None) or _row_tier(row)

            if ticker not in rows_by_ticker and ticker not in overlays_by_ticker:
                incumbent_tier = None
            else:
                incumbent_row = rows_by_ticker.get(ticker)
                incumbent_overlay = overlays_by_ticker.get(ticker)
                incumbent_tier = (
                    incumbent_overlay.get("tier") if incumbent_overlay else None
                ) or _row_tier(incumbent_row)

            if ticker not in winning_run_by_ticker or _tier_rank(candidate_tier) >= _tier_rank(
                incumbent_tier
            ):
                if row is not None:
                    rows_by_ticker[ticker] = row
                if overlay is not None:
                    overlays_by_ticker[ticker] = overlay
                if ticker in run["prices"]:
                    prices[ticker] = run["prices"][ticker]
                winning_run_by_ticker[ticker] = run_id
            else:
                # Losing run still fills price gaps if the winner lacks one.
                if ticker not in prices and ticker in run["prices"]:
                    prices[ticker] = run["prices"][ticker]

        # current_prices.json commonly carries prices for tickers with no
        # ledger row/overlay at all (e.g. a held position that isn't a pick
        # this week but whose price got fetched anyway). Backfill those so
        # a held ticker's price isn't silently dropped just because it
        # never appears in rows_by_ticker/overlays_by_ticker.
        for ticker, price in run["prices"].items():
            if ticker not in prices:
                prices[ticker] = price

    # snapshot_date / generated_at: prefer the newest across contributing runs.
    snapshot_dates = [r["snapshot_date"] for r in loaded if r.get("snapshot_date")]
    generated_ats = [r["generated_at"] for r in loaded if r.get("generated_at")]

    return {
        "run_id": loaded[0]["run_id"] if len(loaded) == 1 else None,
        "run_ids": [r["run_id"] for r in loaded],
        "run_paths": [r["run_path"] for r in loaded],
        "snapshot_date": max(snapshot_dates) if snapshot_dates else None,
        "generated_at": max(generated_ats) if generated_ats else None,
        "rows_by_ticker": rows_by_ticker,
        "overlays_by_ticker": overlays_by_ticker,
        "prices": prices,
        "winning_run_by_ticker": winning_run_by_ticker,
    }


def load_tier_history(ticker: str, db_path: Path = INDEX_DB, limit: int = 6) -> list[dict[str, Any]]:
    """Return last N matrix appearances for ticker, newest first."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT mp.run_id, r.snapshot_date, mp.tier, mp.classification,
                   mp.aggressive_pt, mp.conservative_pt, mp.pt_compression_pct
            FROM matrix_picks mp
            JOIN runs r ON r.run_id = mp.run_id
            WHERE mp.ticker = ?
            ORDER BY r.snapshot_date DESC, mp.run_id DESC
            LIMIT ?
            """,
            (ticker, limit),
        )
        return [
            {
                "run_id": row[0],
                "snapshot_date": row[1],
                "tier": row[2],
                "classification": row[3],
                "aggressive_pt": row[4],
                "conservative_pt": row[5],
                "pt_compression_pct": row[6],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_positions(book: dict[str, Any], strict: bool = False) -> ValidationResult:
    res = ValidationResult()
    positions = book.get("positions", [])
    if not isinstance(positions, list):
        res.errors.append("`positions` must be a list")
        return res

    seen_ids: set[str] = set()
    today = date.today()
    for i, pos in enumerate(positions):
        prefix = f"positions[{i}]"
        for required in ("id", "ticker", "instrument", "qty", "entry_date"):
            if pos.get(required) in (None, ""):
                res.errors.append(f"{prefix}: missing required field `{required}`")

        pid = pos.get("id")
        if pid:
            if pid in seen_ids:
                res.errors.append(f"{prefix}: duplicate id '{pid}'")
            seen_ids.add(pid)

        instrument = pos.get("instrument")
        if instrument and instrument not in VALID_INSTRUMENTS:
            res.errors.append(
                f"{prefix}: instrument '{instrument}' not in {sorted(VALID_INSTRUMENTS)}"
            )

        if instrument and instrument != "equity":
            if not pos.get("option"):
                res.errors.append(f"{prefix}: option positions require an `option` object")
            else:
                opt = pos["option"]
                # Catch legacy field names — surface as actionable errors.
                if "premium_paid_per_contract" in opt and "premium_paid_per_share" not in opt:
                    res.errors.append(
                        f"{prefix}.option: legacy field `premium_paid_per_contract` — "
                        "rename to `premium_paid_per_share` (it was always quoted per-share)."
                    )
                if "current_mark_per_contract" in opt and "current_mark_per_share" not in opt:
                    res.errors.append(
                        f"{prefix}.option: legacy field `current_mark_per_contract` — "
                        "rename to `current_mark_per_share`."
                    )
                for required in ("strike", "expiry", "premium_paid_per_share"):
                    if opt.get(required) in (None, ""):
                        res.errors.append(f"{prefix}.option: missing required field `{required}`")
                expiry_s = opt.get("expiry")
                if expiry_s:
                    try:
                        expiry_d = datetime.fromisoformat(expiry_s).date()
                        if expiry_d < today:
                            msg = f"{prefix}.option.expiry {expiry_s} is in the past"
                            (res.errors if strict else res.warnings).append(msg)
                    except ValueError:
                        res.errors.append(f"{prefix}.option.expiry not ISO date: {expiry_s}")

        if instrument in {"vertical_call_spread", "vertical_put_spread"}:
            if not pos.get("option_short"):
                res.errors.append(f"{prefix}: spread requires `option_short`")
    return res


# ---------------------------------------------------------------------------
# Joining + flag computation


def _tier_rank(tier: str | None) -> int:
    """Mirror compare_insider_ablation.py ladder: VETO < — < C < B < A.
    Delegates to tradingagents.tiers.tier_rank."""
    return tier_rank(tier)


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _option_dte(expiry: str, asof: date) -> int | None:
    try:
        d = datetime.fromisoformat(expiry).date()
        return (d - asof).days
    except (ValueError, TypeError):
        return None


def enrich_position(
    pos: dict[str, Any],
    matrix: dict[str, Any],
    policy: dict[str, Any],
    asof: date,
) -> dict[str, Any]:
    ticker = pos["ticker"]
    out: dict[str, Any] = dict(pos)
    live: dict[str, Any] = {}

    current_price = matrix["prices"].get(ticker)
    live["current_underlying_price"] = current_price

    cost_basis = pos.get("underlying_cost_basis_at_entry")
    if current_price is not None and cost_basis:
        live["underlying_pnl_pct"] = round((current_price - cost_basis) / cost_basis * 100, 2)
    else:
        live["underlying_pnl_pct"] = None

    row = matrix["rows_by_ticker"].get(ticker)
    overlay = matrix["overlays_by_ticker"].get(ticker)

    live["in_latest_matrix"] = bool(row)
    live["matrix_classification"] = row.get("classification") if row else None
    live["matrix_tier_now"] = overlay.get("tier") if overlay else None
    live["matrix_tier_at_entry"] = pos.get("thesis_tier_at_entry")
    live["matrix_aggressive_pt"] = row.get("aggressive_pt") if row else None
    live["matrix_conservative_pt"] = row.get("conservative_pt") if row else None
    live["matrix_compression_pct"] = row.get("pt_compression_pct") if row else None
    live["matrix_aggressive_rating"] = row.get("aggressive_rating") if row else None
    live["matrix_conservative_rating"] = row.get("conservative_rating") if row else None
    live["matrix_aggressive_summary"] = row.get("aggressive_executive_summary") if row else None

    tier_now = live["matrix_tier_now"]
    tier_entry = live["matrix_tier_at_entry"]
    delta_rank = _tier_rank(tier_now) - _tier_rank(tier_entry)
    if tier_entry is None and tier_now is None:
        live["tier_delta"] = "n/a"
    elif tier_now is None and tier_entry is not None:
        # Absent from latest matrix run — thesis unverified this week, not a demotion.
        live["tier_delta"] = "stale"
    elif tier_entry is None and tier_now is not None:
        # No tier at entry (non-matrix-driven hold) but ticker now appears — informational.
        live["tier_delta"] = "newly_rated"
    elif delta_rank > 0:
        live["tier_delta"] = "promoted"
    elif delta_rank < 0:
        live["tier_delta"] = "demoted"
    else:
        live["tier_delta"] = "stable"
    live["tier_delta_rank"] = delta_rank

    live["matrix_long_call_recommendation"] = overlay if overlay else None

    live["tier_history_last_runs"] = load_tier_history(ticker, limit=6)

    opt = pos.get("option") or {}
    if opt and opt.get("expiry"):
        dte = _option_dte(opt["expiry"], asof)
        live["dte"] = dte
        roll_threshold = pos.get("roll_trigger_dte") or policy.get("roll_defaults", {}).get(
            "dte_threshold", 60
        )
        live["roll_trigger_armed"] = dte is not None and dte <= roll_threshold

        prem_paid = opt.get("premium_paid_per_share")
        prem_now = opt.get("current_mark_per_share")
        live["option_pnl_pct"] = (
            round((prem_now - prem_paid) / prem_paid * 100, 2)
            if prem_paid and prem_now
            else None
        )

    stop = pos.get("stop_loss_underlying")
    take = pos.get("take_profit_underlying")
    if current_price is not None:
        live["underlying_below_stop_loss"] = stop is not None and current_price < stop
        live["underlying_above_take_profit"] = take is not None and current_price > take

    out["live"] = live
    return out


def compute_flags(positions: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for pos in positions:
        live = pos.get("live", {})
        ticker = pos["ticker"]
        pid = pos.get("id", ticker)

        if live.get("matrix_classification") == "VETOED":
            flags.append(
                {
                    "severity": "high",
                    "id": pid,
                    "ticker": ticker,
                    "code": "matrix_veto",
                    "message": (
                        f"{ticker} ({pid}) is VETOED in the latest matrix run "
                        f"(was tier {live.get('matrix_tier_at_entry') or 'unknown'} at entry). "
                        f"Policy default action: {policy.get('exit_defaults', {}).get('tier_demoted_to_veto_action', 'review')}"
                    ),
                }
            )
        elif live.get("tier_delta") == "demoted":
            flags.append(
                {
                    "severity": "med",
                    "id": pid,
                    "ticker": ticker,
                    "code": "tier_demoted",
                    "message": (
                        f"{ticker} ({pid}) demoted from tier "
                        f"{live.get('matrix_tier_at_entry')} → {live.get('matrix_tier_now')}. "
                        f"Policy default action: {policy.get('exit_defaults', {}).get('tier_demoted_one_step_action', 'review')}"
                    ),
                }
            )
        elif live.get("tier_delta") == "promoted":
            flags.append(
                {
                    "severity": "info",
                    "id": pid,
                    "ticker": ticker,
                    "code": "tier_promoted",
                    "message": (
                        f"{ticker} ({pid}) promoted "
                        f"{live.get('matrix_tier_at_entry')} → {live.get('matrix_tier_now')}. "
                        "Consider scaling up toward tier_sizing.max_pct."
                    ),
                }
            )

        if live.get("underlying_below_stop_loss"):
            flags.append(
                {
                    "severity": "high",
                    "id": pid,
                    "ticker": ticker,
                    "code": "stop_loss_breach",
                    "message": (
                        f"{ticker} underlying ${live['current_underlying_price']:.2f} "
                        f"below stop_loss_underlying ${pos.get('stop_loss_underlying'):.2f}"
                    ),
                }
            )
        if live.get("underlying_above_take_profit"):
            flags.append(
                {
                    "severity": "med",
                    "id": pid,
                    "ticker": ticker,
                    "code": "take_profit_hit",
                    "message": (
                        f"{ticker} underlying ${live['current_underlying_price']:.2f} "
                        f"above take_profit_underlying ${pos.get('take_profit_underlying'):.2f} "
                        "— consider trimming"
                    ),
                }
            )
        if live.get("roll_trigger_armed"):
            flags.append(
                {
                    "severity": "med",
                    "id": pid,
                    "ticker": ticker,
                    "code": "roll_trigger_dte",
                    "message": (
                        f"{ticker} has {live.get('dte')} days to expiry "
                        f"(threshold {pos.get('roll_trigger_dte') or policy.get('roll_defaults', {}).get('dte_threshold', 60)}). "
                        "Roll candidate evaluation recommended."
                    ),
                }
            )
        if not live.get("in_latest_matrix") and pos.get("thesis_run_id"):
            flags.append(
                {
                    "severity": "info",
                    "id": pid,
                    "ticker": ticker,
                    "code": "not_in_latest_matrix",
                    "message": (
                        f"{ticker} not in latest matrix run "
                        "— matrix-derived signals are stale for this position."
                    ),
                }
            )

    return flags


# ---------------------------------------------------------------------------
# Main


def _resolve_matrix_run_paths(
    matrix_run_path: Path | list[Path] | None,
) -> list[Path]:
    """Normalize the ``matrix_run_path`` override into a list of run dirs.

    - ``None`` → content-based discovery of every run sharing the most
      recent trade date (:func:`find_matrix_runs_for_latest_trade_date`).
    - a single ``Path`` → back-compat: exactly that one run (mirrors the
      old single-run behavior/signature used by existing callers/tests).
    - a ``list[Path]`` → explicit override (mirrors export_lean_signals.py's
      repeatable ``--matrix-run``): use exactly those runs, in the order
      given.
    """
    if matrix_run_path is None:
        return find_matrix_runs_for_latest_trade_date()
    if isinstance(matrix_run_path, (list, tuple)):
        return list(matrix_run_path)
    return [matrix_run_path]


def build_context(
    positions_path: Path = DEFAULT_POSITIONS,
    policy_path: Path = DEFAULT_POLICY,
    matrix_run_path: Path | list[Path] | None = None,
    trades_log_path: Path = DEFAULT_TRADES_LOG,
) -> dict[str, Any]:
    book = load_positions(positions_path)
    policy = load_policy(policy_path)
    trades_log = load_trades_log(trades_log_path)

    run_paths = _resolve_matrix_run_paths(matrix_run_path)
    matrix = load_matrix_runs(run_paths)

    # Back-compat single-path handle: consumers (portfolio_trade_tickets.py,
    # portfolio_allocation.py) re-read <path>/options_overlay.json directly
    # off ctx["latest_matrix_run"]. With >1 contributing run we still expose
    # one representative path (the run with the most ledger rows — usually
    # the most informative single tier) so those direct re-reads keep
    # working; ctx["contributing_matrix_runs"] carries the full list for
    # anything that wants the union.
    if not run_paths:
        primary_run_path = None
    elif len(run_paths) == 1:
        primary_run_path = run_paths[0]
    else:
        primary_run_path = max(
            run_paths,
            key=lambda p: len(json.loads((p / "verdict_ledger.json").read_text()).get("rows", []))
            if (p / "verdict_ledger.json").exists()
            else 0,
        )

    asof_str = book.get("as_of") or matrix.get("snapshot_date") or date.today().isoformat()
    asof = datetime.fromisoformat(asof_str).date()

    enriched = [enrich_position(pos, matrix, policy, asof) for pos in book.get("positions", [])]
    for pos in enriched:
        pos["live"]["recent_actions"] = recent_actions_for_position(
            trades_log, pos["ticker"], pos.get("id")
        )
    flags = compute_flags(enriched, policy)

    summary = {
        "n_positions": len(enriched),
        "n_in_matrix": sum(1 for p in enriched if p["live"].get("in_latest_matrix")),
        "n_demoted": sum(1 for p in enriched if p["live"].get("tier_delta") == "demoted"),
        "n_promoted": sum(1 for p in enriched if p["live"].get("tier_delta") == "promoted"),
        "n_vetoed_now": sum(1 for p in enriched if p["live"].get("matrix_classification") == "VETOED"),
        "n_dte_under_threshold": sum(1 for p in enriched if p["live"].get("roll_trigger_armed")),
        "n_high_severity_flags": sum(1 for f in flags if f["severity"] == "high"),
    }

    primary_run_id = None
    if primary_run_path is not None:
        primary_run_id = next(
            (rid for rid, p in zip(matrix.get("run_ids", []), run_paths) if p == primary_run_path),
            matrix.get("run_id"),
        )

    return {
        "as_of": asof.isoformat(),
        "positions_file": str(positions_path),
        "policy_file": str(policy_path),
        # Back-compat single-path handle (see _resolve_matrix_run_paths note
        # above) — one representative contributing run.
        "latest_matrix_run": str(primary_run_path) if primary_run_path else None,
        "matrix_run_id": primary_run_id,
        "matrix_snapshot_date": matrix.get("snapshot_date"),
        # Full union metadata — every run that fed positions/flags below.
        "contributing_matrix_runs": [str(p) for p in run_paths],
        "contributing_matrix_run_ids": matrix.get("run_ids", []),
        "winning_run_by_ticker": matrix.get("winning_run_by_ticker", {}),
        "policy": policy,
        "positions": enriched,
        "flags": flags,
        "summary": summary,
        "trades_log_path": str(trades_log_path),
        "trades_log_entries": len(trades_log),
    }


def render_markdown(ctx: dict[str, Any]) -> str:
    lines = []
    lines.append(f"# Portfolio context — as of {ctx['as_of']}")
    lines.append("")
    lines.append(f"- **Latest matrix run:** `{ctx.get('matrix_run_id') or '(none)'}`")
    contributing_ids = ctx.get("contributing_matrix_run_ids") or []
    if len(contributing_ids) > 1:
        lines.append(f"- **Contributing matrix runs ({len(contributing_ids)}):** " +
                     ", ".join(f"`{rid}`" for rid in contributing_ids))
    lines.append(f"- **Matrix snapshot date:** {ctx.get('matrix_snapshot_date') or '(none)'}")
    lines.append(f"- **Positions file:** `{ctx['positions_file']}`")
    lines.append("")
    s = ctx["summary"]
    lines.append("## Summary")
    lines.append(
        f"{s['n_positions']} positions, {s['n_in_matrix']} in latest matrix, "
        f"{s['n_promoted']} promoted, {s['n_demoted']} demoted, "
        f"{s['n_vetoed_now']} vetoed, {s['n_dte_under_threshold']} near roll trigger, "
        f"{s['n_high_severity_flags']} high-severity flags."
    )
    lines.append("")

    if ctx["flags"]:
        lines.append("## Flags (sorted by severity)")
        order = {"high": 0, "med": 1, "info": 2}
        for f in sorted(ctx["flags"], key=lambda x: order.get(x["severity"], 9)):
            lines.append(f"- **[{f['severity'].upper()}] {f['code']}** — {f['message']}")
        lines.append("")

    lines.append("## Positions")
    lines.append("")
    if not ctx["positions"]:
        lines.append("_No positions in `positions.json`. Edit `runs/portfolio/positions.json`"
                     " (see `positions.schema.md` and `positions.example.json`)._")
        return "\n".join(lines)

    lines.append("| ID | Ticker | Instrument | Qty | Tier @ entry → now | Δ | DTE | UnderlyingP&L% | OptionP&L% |")
    lines.append("|---|---|---|---:|---|---|---:|---:|---:|")
    for p in ctx["positions"]:
        live = p["live"]
        tier_str = f"{live.get('matrix_tier_at_entry') or '—'} → {live.get('matrix_tier_now') or '—'}"
        delta_emoji = {"promoted": "📈", "demoted": "📉", "stable": "—",
                       "stale": "⏸", "newly_rated": "✨", "n/a": "·"}.get(
            live.get("tier_delta", "n/a"), "·"
        )
        dte = live.get("dte")
        upnl = live.get("underlying_pnl_pct")
        opnl = live.get("option_pnl_pct")
        lines.append(
            f"| `{p.get('id', '')}` | {p['ticker']} | {p['instrument']} | "
            f"{p.get('qty', '')} | {tier_str} | {delta_emoji} | "
            f"{dte if dte is not None else '—'} | "
            f"{upnl if upnl is not None else '—'} | "
            f"{opnl if opnl is not None else '—'} |"
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--matrix-run", type=Path, action="append", default=None,
                        help="Matrix run directory to include; repeat to merge multiple tier "
                             "runs (mirrors export_lean_signals.py). Default: content-based "
                             "discovery of every matrix run sharing the most recent trade date.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    parser.add_argument("--validate", action="store_true",
                        help="Validate positions.json and exit (non-zero on errors)")
    parser.add_argument("--strict", action="store_true",
                        help="With --validate, treat past-expiry options as errors")
    args = parser.parse_args(argv)

    if args.validate:
        try:
            book = load_positions(args.positions_file)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        result = validate_positions(book, strict=args.strict)
        for w in result.warnings:
            print(f"WARN: {w}", file=sys.stderr)
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        if result.ok:
            print(f"OK: {len(book.get('positions', []))} positions validated.")
        return 0 if result.ok else 1

    ctx = build_context(args.positions_file, args.policy_file, args.matrix_run)

    if args.json:
        print(json.dumps(ctx, indent=2, default=str))
    else:
        print(render_markdown(ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
