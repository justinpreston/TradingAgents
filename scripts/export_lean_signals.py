#!/usr/bin/env python3
"""Export TradingAgents matrix options overlays as LEAN-ready signals.

This is a pure file transform: it reads one or more completed matrix run
``options_overlay.json`` files plus portfolio sizing policy, then writes a
``signals.json`` manifest for manual review before LEAN acts.

Example::

    .venv/bin/python scripts/export_lean_signals.py \
        --matrix-run runs/matrix_mid_weekly_2026-06-26_2103_chain \
        --matrix-run runs/matrix_large_weekly_2026-06-26_2126_chain \
        --output lean/signals.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_POLICY = REPO_ROOT / "runs" / "portfolio" / "policy.json"
DEFAULT_OUTPUT = REPO_ROOT / "lean" / "signals.json"
PICK_TIERS = {"A", "B", "C"}
EXIT_RULE_BY_TIER = {
    "A": "tier_a_take_profit",
    "B": "tier_b_run",
    "C": "tier_c_trim",
}
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_STOP_LOSS_PREMIUM_PCT = -0.40
DEFAULT_TIME_STOP_DTE = 21


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _clean_notes(*parts: Any) -> str:
    chunks: list[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, list):
            chunks.extend(str(item) for item in part if item)
        else:
            chunks.append(str(part))
    return "; ".join(chunks)


def _policy_exit_default(policy: dict[str, Any], key: str, fallback: Any) -> Any:
    exit_defaults = policy.get("exit_defaults")
    if isinstance(exit_defaults, dict) and key in exit_defaults:
        return exit_defaults[key]
    return fallback


def _tier_sizing(policy: dict[str, Any], tier: str) -> dict[str, float]:
    sizing = policy.get("tier_sizing")
    if not isinstance(sizing, dict) or not isinstance(sizing.get(tier), dict):
        raise ValueError(f"policy tier_sizing.{tier} is required")
    row = sizing[tier]
    try:
        return {
            "starter_pct": float(row["starter_pct"]),
            "max_pct": float(row["max_pct"]),
        }
    except KeyError as exc:
        raise ValueError(f"policy tier_sizing.{tier}.{exc.args[0]} is required") from exc


def _target_delta(overlay_doc: dict[str, Any], policy: dict[str, Any]) -> float:
    if overlay_doc.get("long_call_delta_target") is not None:
        return float(overlay_doc["long_call_delta_target"])
    options_defaults = policy.get("options_defaults")
    if isinstance(options_defaults, dict) and options_defaults.get("delta_target") is not None:
        return float(options_defaults["delta_target"])
    return 0.55


def _signal_from_overlay(
    overlay: dict[str, Any],
    *,
    measure_date: str,
    target_delta: float,
    policy: dict[str, Any],
    slippage_pct: float,
    stop_loss_premium_pct: float,
    time_stop_dte: int,
    approved: bool,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    tier = overlay.get("tier")
    if tier not in PICK_TIERS:
        return None

    ticker = str(overlay.get("ticker", "<unknown>")).upper()

    legs = overlay.get("legs") or []
    if not legs:
        if warnings is not None:
            warnings.append(f"{ticker} tier {tier} has no option legs — skipped")
        return None
    leg = legs[0]

    ref_premium = leg.get("price", overlay.get("net_debit_per_share"))
    if ref_premium is None:
        if warnings is not None:
            warnings.append(
                f"{ticker} tier {tier} is missing legs[0].price / net_debit_per_share — skipped"
            )
        return None

    sizing = _tier_sizing(policy, tier)
    cons_pt = None if tier == "C" else overlay.get("conservative_pt")

    return {
        "id": f"{ticker}-{measure_date}",
        "ticker": ticker,
        "tier": tier,
        "classification": "PICK",
        "underlying_ref_price": overlay.get("current_price_usd"),
        "option": {
            "right": "call",
            "strike": leg.get("strike"),
            "expiry": leg.get("expiration", overlay.get("expiration")),
            "target_delta": target_delta,
            "occ_symbol": leg.get("symbol"),
            "ref_premium_per_share": ref_premium,
        },
        "entry": {
            "max_premium_per_share": round(float(ref_premium) * (1.0 + slippage_pct), 4),
            "size_pct": sizing["starter_pct"],
            "max_size_pct": sizing["max_pct"],
        },
        "exits": {
            "cons_pt": cons_pt,
            "aggr_pt": overlay.get("aggressive_pt"),
            "rule": EXIT_RULE_BY_TIER[tier],
            "stop_loss_premium_pct": stop_loss_premium_pct,
            "time_stop_dte": time_stop_dte,
        },
        "approved": approved,
        "notes": _clean_notes(overlay.get("liquidity_warnings"), overlay.get("earnings_note")),
    }


def build_manifest(
    matrix_runs: list[Path],
    policy: dict[str, Any],
    *,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    stop_loss_premium_pct: float | None = None,
    time_stop_dte: int | None = None,
    approve_all: bool = False,
    generated_at: str | None = None,
    strict: bool = False,
    existing_signals: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the signals manifest from one or more matrix runs' overlays.

    ``strict=True`` restores the old hard-fail behavior when merged runs
    carry differing ``snapshot_date`` values; by default (``strict=False``)
    a mismatch is only a warning (returned in ``manifest["warnings"]``) and
    the manifest's top-level ``measure_date`` becomes the most recent of the
    contributing dates. Overlay rows missing legs / a usable premium are
    skipped with a warning rather than raising, so one bad row in one tier
    run no longer blocks export for the healthy rows.

    ``existing_signals`` (id -> prior signal dict), when supplied, carries
    forward ``approved`` and any ``notes_user`` field for ids that still
    exist in this export — this is the human approval-preservation contract
    for re-running export on a Friday refresh (see approve_lean_signal.py).
    """
    if not matrix_runs:
        raise ValueError("at least one --matrix-run is required")

    stop_loss = float(
        DEFAULT_STOP_LOSS_PREMIUM_PCT
        if stop_loss_premium_pct is None
        else stop_loss_premium_pct
    )
    time_stop = int(DEFAULT_TIME_STOP_DTE if time_stop_dte is None else time_stop_dte)

    signals: list[dict[str, Any]] = []
    measure_dates: set[str] = set()
    source_runs: list[str] = []
    warnings: list[str] = []

    for run_dir in matrix_runs:
        overlay_path = run_dir / "options_overlay.json"
        doc = _read_json(overlay_path)
        measure_date = doc.get("snapshot_date")
        if not measure_date:
            raise ValueError(f"{overlay_path} is missing snapshot_date")
        measure_dates.add(str(measure_date))
        source_runs.append(_repo_relative(run_dir))

        target_delta = _target_delta(doc, policy)
        for overlay in doc.get("overlays", []) or []:
            signal = _signal_from_overlay(
                overlay,
                measure_date=str(measure_date),
                target_delta=target_delta,
                policy=policy,
                slippage_pct=slippage_pct,
                stop_loss_premium_pct=stop_loss,
                time_stop_dte=time_stop,
                approved=approve_all,
                warnings=warnings,
            )
            if signal is not None:
                signals.append(signal)

    if len(measure_dates) != 1:
        dates = ", ".join(sorted(measure_dates))
        msg = f"merged matrix runs have differing snapshot_date: {dates}"
        if strict:
            raise ValueError(f"all merged matrix runs must share snapshot_date; got {dates}")
        warnings.append(msg)

    measure_date = max(measure_dates) if measure_dates else None

    carried_forward = 0
    new_count = 0
    approval_reset = 0
    if existing_signals:
        for signal in signals:
            prior = existing_signals.get(signal["id"])
            if prior is None:
                new_count += 1
                continue
            if not approve_all and prior.get("approved"):
                # Only carry an approval forward if the underlying contract is
                # unchanged. `id` is keyed on ticker+measure_date, so a same-day
                # overlay refresh that rolls the Δ0.55 strike/expiry (or otherwise
                # re-selects a different contract) reuses the same id — carrying
                # the approval blindly would transplant the human gate onto a
                # contract the user never reviewed. occ_symbol uniquely encodes
                # strike+expiry+right, so require it (and both being present).
                prior_occ = (prior.get("option") or {}).get("occ_symbol")
                new_occ = (signal.get("option") or {}).get("occ_symbol")
                if prior_occ and new_occ and prior_occ == new_occ:
                    signal["approved"] = True
                    carried_forward += 1
                else:
                    approval_reset += 1
                    warnings.append(
                        f"{signal['id']} contract changed since approval "
                        f"({prior_occ or 'None'} -> {new_occ or 'None'}); "
                        "approval reset to false — re-approve required"
                    )
            if prior.get("notes_user"):
                signal["notes_user"] = prior["notes_user"]
    else:
        new_count = len(signals)

    signals.sort(key=lambda s: ({"A": 0, "B": 1, "C": 2}[s["tier"]], s["ticker"]))
    return {
        "schema_version": "1.0",
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_run": ", ".join(source_runs),
        "measure_date": measure_date,
        "signals": signals,
        "warnings": warnings,
        "n_new_signals": new_count,
        "n_carried_forward_approvals": carried_forward,
    }


_TRANSIENT_MANIFEST_KEYS = ("warnings", "n_new_signals", "n_carried_forward_approvals")


def write_manifest(path: Path, manifest: dict[str, Any], *, pretty: bool = True) -> None:
    """Persist the manifest, dropping transient run-summary keys.

    ``warnings`` / ``n_new_signals`` / ``n_carried_forward_approvals`` are
    build_manifest()-only diagnostics for the CLI summary print — they are
    not part of the lean/signals.schema.md contract, so they're stripped
    before writing to keep the on-disk schema stable.
    """
    persisted = {k: v for k, v in manifest.items() if k not in _TRANSIENT_MANIFEST_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(persisted, indent=2 if pretty else None, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def load_existing_signals(path: Path) -> dict[str, dict[str, Any]]:
    """Load id -> signal dict from an existing signals.json, or {} if absent/malformed."""
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(doc, dict):
        return {}
    return {s["id"]: s for s in doc.get("signals", []) if isinstance(s, dict) and s.get("id")}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-run", type=Path, action="append", required=True,
                        help="Completed matrix run directory; repeat to merge tier runs")
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_SLIPPAGE_PCT)
    parser.add_argument("--stop-loss-pct", type=float, default=None,
                        help="Premium stop-loss pct, e.g. -0.40 for -40%%")
    parser.add_argument("--time-stop-dte", type=int, default=None)
    parser.add_argument("--approve-all", action="store_true",
                        help="Testing only: mark every signal approved. Real workflow is manual review.")
    parser.add_argument("--strict", action="store_true",
                        help="Hard-fail (old behavior) if merged matrix runs have differing "
                             "snapshot_date, instead of warning and using the most recent.")
    parser.add_argument("--pretty", action="store_true", default=True,
                        help="Pretty-print JSON output (default on)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    policy = _read_json(args.policy_file)
    stop_loss = args.stop_loss_pct
    if stop_loss is None:
        stop_loss = _policy_exit_default(policy, "stop_loss_premium_pct", DEFAULT_STOP_LOSS_PREMIUM_PCT)
    time_stop = args.time_stop_dte
    if time_stop is None:
        time_stop = _policy_exit_default(policy, "time_stop_dte", DEFAULT_TIME_STOP_DTE)

    existing_signals = load_existing_signals(args.output)

    manifest = build_manifest(
        args.matrix_run,
        policy,
        slippage_pct=args.slippage_pct,
        stop_loss_premium_pct=float(stop_loss),
        time_stop_dte=int(time_stop),
        approve_all=args.approve_all,
        strict=args.strict,
        existing_signals=existing_signals,
    )
    write_manifest(args.output, manifest, pretty=args.pretty)

    for w in manifest.get("warnings", []):
        print(f"⚠️  {w}")

    counts = Counter(signal["tier"] for signal in manifest["signals"])
    summary = ", ".join(f"{tier}:{counts.get(tier, 0)}" for tier in ["A", "B", "C"])
    print(f"✅ Wrote {len(manifest['signals'])} LEAN signals to {_repo_relative(args.output)} ({summary})")
    print(
        f"   {manifest['n_new_signals']} new, "
        f"{manifest['n_carried_forward_approvals']} carried-forward approvals"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
