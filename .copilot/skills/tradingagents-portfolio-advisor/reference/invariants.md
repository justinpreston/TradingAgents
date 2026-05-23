# Invariants

Operational rules that must hold for every portfolio recommendation.

## 1. Validate prices via Polygon before quoting specifics

Per the stored memory:

> User input: *"always validate any potential trades with existing data from Polygon"*

The matrix run's `current_prices.json` already contains Polygon-validated
prices (sourced from `/v2/aggs/ticker/{T}/prev`). When the held ticker is in
the matrix, use those prices.

For tickers **not** in the latest matrix:

- Do NOT infer the underlying from option chain delta (high-IV names like
  INTC have broken this heuristic before — see the corrected INTC roll math
  in session 5/5/2026).
- Do NOT guess prices from chat history.
- Refuse to quote a specific entry/exit until the user runs the screener or
  matrix on that ticker.

## 2. Long calls preferred, slightly ITM (Δ 0.55)

Per `/CLAUDE.md` and the stored cadence memory:

> User input: *"I generally prefer long calls for ease of trading."*

- Default: `--strategy-mode long-call --long-call-delta 0.55`
- Tenor: 6–18 months (multi-month, captures earnings + multi-quarter catalysts)
- Slightly ITM Δ 0.55 has less time decay than ATM, retains leverage, single fill.

The matrix's options overlay is configured to this default. When emitting a
suggested ticket, prefer the matrix-recommended structure over inventing a
different one. If the user asks for a different structure, surface the
trade-off (theta, breakeven, max loss) but quote the requested structure.

## 3. Always check current_datetime before quoting prices

Per the stored memory:

> User input: *"ALways check the current date and time too"*

Polygon's `/prev` finalises the prior session ~30–60 min after close but can
lag. Near market hours, cross-check against the current time and note
staleness explicitly. The matrix run's `current_prices.json::snapshot_taken`
field gives you the validated capture time.

## 4. Signal vs. noise drift framework (5/8 ablation findings)

When a position's tier changes week-over-week, classify before acting:

| Drift type | Signal | Action |
|---|---|---|
| **Noise drift** | Vague, hand-wavy cons rationale; LLM stochasticity | Re-run; don't act on a single sample |
| **Code drift** | Cons rationale references broken/missing tools, retry artifacts | Fix the tool first; re-run |
| **Signal drift** | Cons cites specific multiples, technicals, dollar amounts, news | **Real signal — act on it** |

The 5/8 ablation found 43% code drift, 57% signal drift, 0% noise drift in
that cohort. The cons frame's `summary.md` text is the diagnostic — read
the actual rationale before treating a tier change as actionable.

## 5. Tier A/B/C is derived per row, not stored

The `classification` column only takes `PICK` or `VETOED`. Tier comes from
the rules in `reference/tier_economics.md`. **Never** guess a tier — read it
from `verdict_ledger.json` (or via `portfolio_load_context.py`, which does
the join correctly).

## 6. Read-only operation

This skill never:

- Writes `positions.json` — user controls their own ledger
- Places trades or calls broker APIs
- Writes to `runs/index.db` — only `scripts/index_runs.py` does
- Re-runs matrix or screener — those are explicit user-driven commands
- Modifies `runs/portfolio/policy.json` without explicit user confirmation

## 7. Specific over narrative

The user explicitly asked for granularity:

> User input: *"we want it to be pretty granular. I'm looking for specific
> trades, strikes, exits, etc."*

Every actionable recommendation must include:

- Specific contract: strike, expiry
- Quantity: number of contracts to buy / sell / roll
- Premium: per-share quote AND per-contract dollar cost
- Triggers: stop-loss underlying, take-profit underlying, roll DTE
- Greeks at point of trade: Δ, IV, OI

Narrative-only suggestions ("consider trimming TRGP") are insufficient.
Trade-ticket-grade output is the bar.
