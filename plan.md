# LEAN integration — weekly run → approved signals → automated trading

## Goal
Turn the weekly TradingAgents output into a LEAN algorithm that auto-executes
long calls and manages the *exit discipline* proven in the backtest
(`runs/backtest_exits_2026-06-26.json`): take profit at PT, tier-specific.

## Architecture
Brain (weekly run) → `lean/signals.json` (user approves) → LEAN algo → broker.

## Shared contract: signals.json v1.0  (ALL agents build to this)
Top-level: schema_version, generated_at, source_run, measure_date, signals[].
Signal: id, ticker, tier(A|B|C), classification, underlying_ref_price,
  option{right,strike,expiry,target_delta,occ_symbol,ref_premium_per_share},
  entry{max_premium_per_share, size_pct, max_size_pct},
  exits{cons_pt, aggr_pt(nullable), rule, stop_loss_premium_pct, time_stop_dte},
  approved(false default), notes.

### Exit rule semantics (canonical — bridge emits, algo implements)
- tier_a_take_profit: underlying high ≥ cons_pt → sell 50%; ≥ aggr_pt → sell rest.
- tier_b_run: ignore cons_pt; underlying high ≥ aggr_pt → sell 100%.
- tier_c_trim: underlying high ≥ aggr_pt → sell 100% (C has no cons_pt).
- Universal overlay (all tiers): premium ≤ entry*(1+stop_loss_premium_pct) → sell 100%;
  DTE ≤ time_stop_dte → sell 100%.

## Workstreams (parallel, disjoint files)
- [ ] A bridge: scripts/export_lean_signals.py + tests/test_export_lean_signals.py
      + lean/signals.example.json + lean/signals.schema.md
- [ ] B lean-algo: lean/algorithm/{TradingAgentsAlgorithm.py, SignalData.py,
      config.json, README.md}
- [ ] C infra-docs: lean/README.md (architecture, staging, broker/infra) +
      correct stale ROI in tier_economics.md (DO NOT touch policy.json sizing)

## Inputs (real)
options_overlay.json fields: ticker, current_price_usd, aggressive_pt,
conservative_pt, tier, expiration, legs[0]{symbol(OCC),strike,delta,price},
net_debit_per_share. policy.json: tier_sizing{A,B,C}{starter_pct,max_pct},
exit_defaults.

## Validation
- Run export script on runs/matrix_*_weekly_2026-06-26_*_chain → signals.json.
- Run pytest tests/test_export_lean_signals.py.
- LEAN algo validated by structure/syntax (no LEAN CLI/data locally).
